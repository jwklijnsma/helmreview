"""Web UI: auth, path safety, option whitelisting and real scan/fix jobs against mock Ollama."""

import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import threading
import time

import pytest
import requests

from helmreview.web.server import ApiError, Job, WebApp, build_argv, make_server

import mock_ollama

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sample-repo"
TOKEN = "s3cret-token"
POST = {"X-Helmreview": "1", "Authorization": f"Bearer {TOKEN}"}
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def repos(tmp_path):
    root = tmp_path / "repos"
    root.mkdir()
    repo = root / "infra"
    shutil.copytree(SAMPLE, repo)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                    "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], check=True)
    (root / "not-k8s").mkdir()
    (root / "not-k8s" / "notes.txt").write_text("hi")
    (tmp_path / "outside.txt").write_text("top secret")
    return root


@pytest.fixture()
def ollama(monkeypatch, tmp_path):
    srv, url = mock_ollama.start()
    monkeypatch.setenv("OLLAMA_HOST", url)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))   # no ~/.helmreview.yml leaks in
    (tmp_path / "home").mkdir()
    yield url
    srv.shutdown()


def start(app):
    srv = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.fixture()
def web(repos, ollama):
    app = WebApp([repos], TOKEN)
    srv, base = start(app)
    yield app, base
    for j in list(app.jobs.values()):
        if j.proc and j.proc.poll() is None:
            app.cancel_job(j.id)
    srv.shutdown()


def wait(base, job_id, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        d = requests.get(f"{base}/api/jobs/{job_id}", headers=AUTH).json()
        if d["status"] not in ("queued", "running"):
            return d
        time.sleep(0.3)
    raise AssertionError("job did not finish")


# ------------------------------------------------------------------ option whitelist

def test_build_argv_uses_equals_form_and_whitelist(tmp_path):
    argv = build_argv("scan", tmp_path, {"model": "qwen3-coder:30b", "only": ["charts/web"],
                                         "diff": "origin/main", "no_llm": True,
                                         "host": "http://evil:11434", "output": "/tmp/x"})
    assert argv[:2] == ["scan", str(tmp_path)]
    assert "--model=qwen3-coder:30b" in argv and "--only=charts/web" in argv
    assert "--diff=origin/main" in argv and "--no-llm" in argv
    assert not any("evil" in a or a.startswith(("--host", "-o", "--output")) for a in argv)


@pytest.mark.parametrize("opts", [
    {"model": "--host=http://evil"}, {"model": "a b"}, {"diff": "-x"}, {"diff": "a;rm"},
    {"only": ["../etc"]}, {"only": ["/etc"]}, {"only": ["-x"]}, {"lang": "--evil"},
    {"min_severity": "huge"}, {"lang": "fr"}, {"diff": "main", "diff_scope": "all"},
])
def test_build_argv_rejects_bad_values(tmp_path, opts):
    with pytest.raises(ApiError):
        build_argv("scan", tmp_path, opts)


def test_build_argv_fix_options(tmp_path):
    argv = build_argv("fix", tmp_path, {"dry_run": True, "min_confidence": "0.9",
                                        "config": "/root/.evil.yml"})
    assert "--dry-run" in argv and "--min-confidence=0.9" in argv
    assert not any("evil" in a for a in argv)
    with pytest.raises(ApiError):
        build_argv("fix", tmp_path, {"min_confidence": "7"})
    with pytest.raises(ApiError):
        build_argv("doctor", tmp_path, {})


# ------------------------------------------------------------------ auth & safety

def test_static_page_and_security_headers(web):
    _, base = web
    r = requests.get(base + "/")
    assert r.status_code == 200 and "helmreview" in r.text
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY"
    assert requests.get(base + "/app.js").status_code == 200
    assert requests.get(base + "/../setup.py").status_code == 404


def test_api_requires_token(web):
    _, base = web
    assert requests.get(base + "/api/repos").status_code == 401
    assert requests.get(base + "/api/repos", headers={"Authorization": "Bearer nope"}).status_code == 401
    r = requests.get(base + "/api/repos", headers=AUTH)
    assert r.status_code == 200
    names = [x["name"] for x in r.json()["repos"]]
    assert names == ["infra"]          # folders without charts or manifests are not listed


def test_token_link_sets_cookie(web):
    _, base = web
    s = requests.Session()
    r = s.get(base + f"/?token={TOKEN}", allow_redirects=False)
    assert r.status_code == 303 and r.headers["Location"] == "/"
    assert "HttpOnly" in r.headers["Set-Cookie"] and "SameSite=Strict" in r.headers["Set-Cookie"]
    assert s.get(base + "/api/repos").status_code == 200
    assert requests.get(base + "/?token=wrong", allow_redirects=False).status_code == 401


def test_login_endpoint(web):
    _, base = web
    s = requests.Session()
    r = s.post(base + "/api/login", json={"token": "nope"}, headers={"X-Helmreview": "1"})
    assert r.status_code == 401
    r = s.post(base + "/api/login", json={"token": TOKEN}, headers={"X-Helmreview": "1"})
    assert r.status_code == 200
    assert s.get(base + "/api/repos").status_code == 200


def test_post_needs_csrf_header_and_same_origin(web):
    _, base = web
    r = requests.post(base + "/api/repos/0:infra/scan", json={}, headers=AUTH)
    assert r.status_code == 403
    r = requests.post(base + "/api/repos/0:infra/scan", json={},
                      headers={**POST, "Origin": "http://evil.example"})
    assert r.status_code == 403


def test_repo_ids_cannot_escape_roots(web):
    _, base = web
    for rid in ("0:..", "0:.", "1:infra", "0:not-k8s", "0:infra%2F..", "x", "0:.git"):
        assert requests.get(f"{base}/api/repos/{rid}", headers=AUTH).status_code == 404, rid


def test_file_endpoint_stays_inside_repo(web, repos):
    _, base = web
    ok = requests.get(f"{base}/api/repos/0:infra/file", params={"path": "charts/webapp/values.yaml"}, headers=AUTH)
    assert ok.status_code == 200 and "replicaCount" in ok.json()["content"]
    for bad in ("../../outside.txt", "/etc/passwd", ".git/config", "charts/../../../outside.txt"):
        r = requests.get(f"{base}/api/repos/0:infra/file", params={"path": bad}, headers=AUTH)
        assert r.status_code in (400, 403), bad
    os.symlink(repos.parent / "outside.txt", repos / "infra" / "link.txt")
    r = requests.get(f"{base}/api/repos/0:infra/file", params={"path": "link.txt"}, headers=AUTH)
    assert r.status_code == 403


# ------------------------------------------------------------------ jobs

def test_scan_then_preview_fix(web, repos):
    app, base = web
    r = requests.post(f"{base}/api/repos/0:infra/scan", json={"no_lint": True}, headers=POST)
    assert r.status_code == 202, r.text
    job = wait(base, r.json()["id"])
    assert job["status"] == "done", job
    detail = requests.get(f"{base}/api/jobs/{job['id']}", headers=AUTH).json()
    assert any("helmreview" in ln for ln in detail["lines"])

    rep = requests.get(f"{base}/api/repos/0:infra/report", headers=AUTH).json()["report"]
    rules = {f["rule"] for f in rep["findings"]}
    assert {"HR001", "HR011"} <= rules
    assert any(f["source"] == "llm" for f in rep["findings"])
    assert (repos / "infra" / ".helmreview" / "report.sarif").is_file()

    listing = requests.get(f"{base}/api/repos", headers=AUTH).json()["repos"][0]
    assert listing["counts"] and listing["report_time"]

    before = (repos / "infra" / "charts" / "webapp" / "templates" / "service.yaml").read_text()
    r = requests.post(f"{base}/api/repos/0:infra/fix",
                      json={"dry_run": True}, headers=POST)
    assert r.status_code == 202
    job = wait(base, r.json()["id"])
    assert job["status"] == "done", job
    fx = requests.get(f"{base}/api/repos/0:infra/fix", headers=AUTH).json()
    assert fx["patch"] and "webapp.selectorLabels" in fx["patch"]
    assert "Dry run" in fx["report_md"]
    after = (repos / "infra" / "charts" / "webapp" / "templates" / "service.yaml").read_text()
    assert before == after            # preview never touches the working tree


def slow_python(tmp_path, seconds=60):
    fake = tmp_path / "fake-python"
    fake.write_text(f"#!{sys.executable}\nimport sys, time\n"
                    f"print('working', sys.argv[3], flush=True)\ntime.sleep({seconds})\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return str(fake)


def test_jobs_queue_in_order_and_queued_ones_can_be_cancelled(repos, ollama, tmp_path):
    app = WebApp([repos], TOKEN, python=slow_python(tmp_path, 3))
    srv, base = start(app)
    try:
        a = requests.post(f"{base}/api/repos/0:infra/scan", json={}, headers=POST).json()
        b = requests.post(f"{base}/api/repos/0:infra/fix", json={"dry_run": True}, headers=POST).json()
        c = requests.post(f"{base}/api/repos/0:infra/scan", json={"no_llm": True}, headers=POST).json()
        # the same job twice is refused, a different one is queued
        dup = requests.post(f"{base}/api/repos/0:infra/fix", json={"dry_run": True}, headers=POST)
        assert dup.status_code == 409 and "already" in dup.json()["error"]
        time.sleep(0.5)
        jobs = {j["id"]: j for j in requests.get(f"{base}/api/jobs", headers=AUTH).json()["jobs"]}
        assert jobs[a["id"]]["status"] == "running"
        assert jobs[b["id"]]["status"] == "queued" and jobs[b["id"]]["position"] == 1
        assert jobs[c["id"]]["position"] == 2
        assert requests.post(f"{base}/api/jobs/{c['id']}/cancel", headers=POST).json()["status"] == "cancelled"
        assert wait(base, a["id"])["status"] == "done"
        assert wait(base, b["id"])["status"] == "done"
        cj = requests.get(f"{base}/api/jobs/{c['id']}", headers=AUTH).json()
        assert cj["status"] == "cancelled" and cj["started"] is None   # never ran
        ran = [ln for j in (a, b) for ln in requests.get(f"{base}/api/jobs/{j['id']}", headers=AUTH).json()["lines"] if ln.startswith("working")]
        assert ran == ["working scan", "working fix"]
    finally:
        srv.shutdown()


def test_queue_limit(repos, ollama, tmp_path):
    from helmreview.web import server as srvmod
    app = WebApp([repos], TOKEN, python=slow_python(tmp_path))
    try:
        app.start_job("0:infra", "scan", {})
        time.sleep(0.3)
        for i in range(srvmod.MAX_PENDING):
            app.start_job("0:infra", "scan", {"only": [f"charts/c{i}"]})
        with pytest.raises(ApiError) as e:
            app.start_job("0:infra", "scan", {"only": ["charts/one-too-many"]})
        assert e.value.status == 429
    finally:
        for j in list(app.jobs.values()):
            app.cancel_job(j.id)


def test_finished_jobs_offer_downloads(web):
    _, base = web
    job = requests.post(f"{base}/api/repos/0:infra/scan", json={"no_lint": True}, headers=POST).json()
    done = wait(base, job["id"])
    names = [d["name"] for d in done["downloads"]]
    assert names == ["report.md", "report.json", "report.sarif"]
    r = requests.get(base + done["downloads"][0]["url"], headers=AUTH)
    assert r.status_code == 200 and "infra-report.md" in r.headers["Content-Disposition"]
    assert "HR001" in r.text
    assert requests.get(f"{base}/api/repos/0:infra/artifacts/fix.patch", headers=AUTH).status_code == 404
    assert requests.get(f"{base}/api/repos/0:infra/artifacts/..%2Fcharts", headers=AUTH).status_code == 404
    job = requests.post(f"{base}/api/repos/0:infra/fix", json={"dry_run": True}, headers=POST).json()
    done = wait(base, job["id"])
    assert [d["name"] for d in done["downloads"]] == ["fix.patch", "fix-report.md"]
    assert done["downloads"][0].get("primary")
    assert "webapp.selectorLabels" in requests.get(base + done["downloads"][0]["url"], headers=AUTH).text


def test_cancel_running_job(repos, ollama, tmp_path):
    fake = tmp_path / "fake-python"
    fake.write_text(f"#!{sys.executable}\nimport time\nprint('working', flush=True)\ntime.sleep(60)\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    app = WebApp([repos], TOKEN, python=str(fake))
    srv, base = start(app)
    try:
        r = requests.post(f"{base}/api/repos/0:infra/scan", json={}, headers=POST)
        jid = r.json()["id"]
        for _ in range(50):
            if requests.get(f"{base}/api/jobs/{jid}", headers=AUTH).json()["status"] == "running":
                break
            time.sleep(0.1)
        assert requests.post(f"{base}/api/jobs/{jid}/cancel", headers=POST).status_code == 200
        assert wait(base, jid, timeout=15)["status"] == "cancelled"
    finally:
        srv.shutdown()


def test_job_does_not_run_from_repo_dir(web, repos):
    """A planted helmreview.py in the scanned repo must not replace the tool."""
    app, base = web
    (repos / "infra" / "helmreview.py").write_text("raise SystemExit('HIJACKED')\n")
    r = requests.post(f"{base}/api/repos/0:infra/scan",
                      json={"no_lint": True, "no_llm": True}, headers=POST)
    job = wait(base, r.json()["id"])
    lines = requests.get(f"{base}/api/jobs/{job['id']}", headers=AUTH).json()["lines"]
    assert job["status"] == "done" and not any("HIJACKED" in ln for ln in lines)


def test_no_auth_refused_on_public_bind(repos, capsys):
    from helmreview.web.server import serve
    assert serve([repos], bind="0.0.0.0", port=0, no_auth=True) == 2
    assert "--no-auth" in capsys.readouterr().err


def test_placeholder_token_refused(repos, capsys, monkeypatch):
    from helmreview.web.server import serve
    monkeypatch.setenv("HELMREVIEW_WEB_TOKEN", "change-me")
    assert serve([repos], port=0, no_upload=True) == 2
    assert "example value" in capsys.readouterr().err


def test_model_picked_in_the_ui(repos, ollama, tmp_path):
    state = tmp_path / "state"
    app = WebApp([repos], TOKEN, state_dir=state)
    srv, base = start(app)
    try:
        st = requests.get(f"{base}/api/status", headers=AUTH).json()
        default = st["model"]
        assert st["model_source"] == "config" and st["default_model"] == default
        r = requests.post(f"{base}/api/settings", json={"model": "qwen3-coder:30b"}, headers=POST)
        assert r.status_code == 200 and r.json() == {"model": "qwen3-coder:30b", "model_source": "ui", "persisted": True}
        st = requests.get(f"{base}/api/status", headers=AUTH).json()
        assert st["model"] == "qwen3-coder:30b" and st["model_source"] == "ui"
        assert requests.get(f"{base}/api/repos/0:infra", headers=AUTH).json()["config"]["model"] == "qwen3-coder:30b"
        # jobs get it, unless the repo's options name another model
        job = app.start_job("0:infra", "scan", {"no_llm": True})
        assert "--model=qwen3-coder:30b" in job.argv
        job2 = app.start_job("0:infra", "scan", {"no_llm": True, "model": "llama3.1:8b"})
        assert "--model=llama3.1:8b" in job2.argv and "--model=qwen3-coder:30b" not in job2.argv
        # validated, and needs the CSRF header like every POST
        assert requests.post(f"{base}/api/settings", json={"model": "--host=http://evil"}, headers=POST).status_code == 400
        assert requests.post(f"{base}/api/settings", json={"model": "x"}, headers=AUTH).status_code == 403
    finally:
        for j in list(app.jobs.values()):
            app.cancel_job(j.id)
        srv.shutdown()
    # survives a restart
    again = WebApp([repos], TOKEN, state_dir=state)
    assert again.effective_model() == "qwen3-coder:30b"
    # empty = back to the configured default
    assert again.set_model("")["model_source"] == "config"
    assert WebApp([repos], TOKEN, state_dir=state).effective_model() == default


def test_broken_settings_file_is_ignored(repos, ollama, tmp_path):
    (tmp_path / "settings.json").write_text("{not json")
    assert WebApp([repos], TOKEN, state_dir=tmp_path).settings == {}
    (tmp_path / "settings.json").write_text('{"model": "bad name; rm -rf /"}')
    assert WebApp([repos], TOKEN, state_dir=tmp_path).settings == {}


def test_cancel_just_before_the_process_starts(repos, ollama, tmp_path, monkeypatch):
    """Cancel between "running" and the subprocess existing used to be lost."""
    import subprocess as sp
    from helmreview.web import server as srvmod
    fake = tmp_path / "fake-python"
    fake.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    app = WebApp([repos], TOKEN, python=str(fake))
    real_popen = sp.Popen

    def slow_popen(*a, **kw):          # cancel inside the window
        for j in list(app.jobs.values()):
            app.cancel_job(j.id)
        return real_popen(*a, **kw)

    monkeypatch.setattr(srvmod.subprocess, "Popen", slow_popen)
    job = app.start_job("0:infra", "scan", {})
    t0 = time.time()
    while job.active and time.time() - t0 < 15:
        time.sleep(0.1)
    assert job.status == "cancelled"
