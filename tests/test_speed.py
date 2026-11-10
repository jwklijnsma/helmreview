"""Speed-related behaviour: streaming, timeouts that split work, excerpts, cache."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time

import pytest

from helmreview.config import Config
from helmreview.fix import Fixer, _excerpt
from helmreview.models import Finding, Severity
from helmreview.ollama import OllamaClient, OllamaTimeout


class _Slow(BaseHTTPRequestHandler):
    """Streams NDJSON; stalls forever when asked about more than `max_findings` at once."""
    max_findings = 1
    calls: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = req["messages"][-1]["content"]
        ids = [int(l[1:l.index("]")]) for l in prompt.splitlines() if l.startswith("[")]
        _Slow.calls.append((ids, prompt))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        if len(ids) > _Slow.max_findings:
            time.sleep(3)      # longer than the client's idle timeout
            return
        body = json.dumps({"results": [{"id": i, "action": "skip", "reason": "fp", "edits": []}
                                       for i in ids]})
        for piece in (body[: len(body) // 2], body[len(body) // 2:]):
            self.wfile.write(json.dumps({"message": {"content": piece}, "done": False}).encode() + b"\n")
            self.wfile.flush()
        self.wfile.write(json.dumps({"message": {"content": ""}, "done": True,
                                     "done_reason": "stop", "eval_count": 5}).encode() + b"\n")


@pytest.fixture()
def slow():
    _Slow.calls = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _f(line, rule="llm:x"):
    return Finding(rule=rule, severity=Severity.HIGH, file="templates/cm.yaml", line=line, message=rule,
                   source="llm", category="reliability", suggestion="", confidence=0.9)


def test_timeout_is_not_retried(slow):
    c = OllamaClient(slow, "m", timeout=1, retries=2)
    with pytest.raises(OllamaTimeout):
        c.chat_json("s", "[0] a\n[1] b", {})
    assert len(_Slow.calls) == 1          # old client sent the same prompt 3 times


def test_fix_splits_batch_on_timeout_and_caches(slow, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "templates").mkdir(exist_ok=True)
    (root / "templates" / "cm.yaml").write_text("".join(
        f"---\nkind: ConfigMap\nmetadata:\n  name: c{i}\n" for i in range(10)))
    cfg = Config(fix_batch_size=4)
    findings = [_f(3), _f(5), _f(7), _f(9)]

    run = Fixer(root, cfg, client=OllamaClient(slow, "m", timeout=1), static_fix=False).run(findings)
    assert run.count("declined", "llm") == 4
    sizes = [len(ids) for ids, _ in _Slow.calls]
    assert sizes[0] == 4 and sizes.count(1) == 4       # 4 -> 2+2 -> 1+1+1+1

    _Slow.calls = []
    run = Fixer(root, cfg, client=OllamaClient(slow, "m", timeout=1), static_fix=False).run(findings)
    assert run.count("declined", "llm") == 4 and _Slow.calls == []   # all from cache


def test_excerpt_only_sends_windows_for_large_files():
    text = "\n".join(f"line{i}" for i in range(1, 1001))
    out = _excerpt(text, [_f(500)], context=5, full_below=120)
    assert " 500 | line500" in out and "line800" not in out and "..." in out
    assert len(out.splitlines()) < 40
    assert _excerpt("a\nb", [_f(1)], 5, 120).count("|") == 2


def test_scan_splits_on_timeout_and_caches(slow, tmp_path):
    from helmreview.models import Unit
    from helmreview.review import Reviewer

    class Stall(_Slow):
        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            files = [l for l in req["messages"][-1]["content"].splitlines()
                     if l.startswith("===== FILE")]
            _Slow.calls.append((files, ""))
            self.send_response(200)
            self.end_headers()
            if len(files) > 1:
                time.sleep(3)
                return
            body = json.dumps({"summary": "ok", "findings": []})
            self.wfile.write(json.dumps({"message": {"content": body}, "done": True}).encode())

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stall)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        for n in range(4):
            (tmp_path / f"f{n}.yml").write_text("kind: ConfigMap\n" * 50)
        unit = Unit(kind="manifests", name="x", files=[f"f{n}.yml" for n in range(4)])
        cfg = Config(max_chars_per_unit=24000)
        Reviewer(tmp_path, cfg, OllamaClient(url, "m", timeout=1)).review([unit], [])
        assert sorted(len(f) for f, _ in _Slow.calls).count(1) == 4

        _Slow.calls = []
        r = Reviewer(tmp_path, cfg, OllamaClient(url, "m", timeout=1))
        r.review([unit], [])
        assert all(len(f) == 1 for f, _ in _Slow.calls) is True and not r.errors
        assert len(_Slow.calls) == 0       # answers + known timeouts both cached
    finally:
        srv.shutdown()


def test_doctor_speed_check(capsys):
    import mock_ollama
    from helmreview.cli import main
    srv, url = mock_ollama.start()
    try:
        main(["doctor", str(Path(__file__).parent.parent / "examples" / "sample-repo"),
              "--host", url])
    finally:
        srv.shutdown()
    out = capsys.readouterr().out
    assert "tok/s" in out and "fully on the GPU" in out


def test_one_request_per_file_and_queue(tmp_path):
    import shutil
    import mock_ollama
    from helmreview.cli import main
    repo = tmp_path / "repo"
    shutil.copytree(Path(__file__).parent.parent / "examples" / "sample-repo", repo)
    srv, url = mock_ollama.start()
    try:
        main(["scan", str(repo), "--host", url, "-q", "-f", "json", "--no-lint"])
    finally:
        srv.shutdown()
    prompts = [c["messages"][-1]["content"] for c in mock_ollama._Handler.calls]
    assert prompts and all(p.count("===== FILE:") == 1 for p in prompts)
    files = [p.split("===== FILE: ")[1].split(" ")[0] for p in prompts]
    assert len(files) == len(set(files))                       # each file once

    q = json.loads((repo / ".helmreview" / "queue.json").read_text())
    assert q["stage"] == "scan" and q["total"] == len(files)
    assert {f["status"] for f in q["files"]} == {"done"}
