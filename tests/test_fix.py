import json
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from helmreview import lint
from helmreview.cli import main
from helmreview.fix import _apply_edits

import mock_ollama

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sample-repo"
needs_git = pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
needs_helm = pytest.mark.skipif(not shutil.which("helm"), reason="helm not installed")
CHART = "charts/webapp"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                           *args], check=True, capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path):
    dst = tmp_path / "repo"
    shutil.copytree(SAMPLE, dst)
    _git(dst, "init", "-q")
    _git(dst, "add", ".")
    _git(dst, "commit", "-qm", "init")
    return dst


@pytest.fixture()
def ollama():
    srv, url = mock_ollama.start()
    yield url
    srv.shutdown()


def _llm(rule, file, line, conf=0.9, sev="high"):
    return {"rule": f"llm:{rule}", "severity": sev, "file": file, "line": line,
            "message": rule, "source": "llm", "category": "reliability",
            "suggestion": "", "confidence": conf}


def _write_report(repo, findings):
    out = repo / ".helmreview"
    out.mkdir(exist_ok=True)
    (out / "report.json").write_text(json.dumps({"root": str(repo), "findings": findings}))


@needs_git
@needs_helm
def test_fix_end_to_end(repo, ollama):
    _write_report(repo, [
        _llm("selector-label-mismatch", f"{CHART}/templates/service.yaml", 13),
        _llm("secret-in-env", f"{CHART}/templates/deployment.yaml", 28),
        _llm("false-positive", f"{CHART}/templates/ingress.yaml", 2),
        _llm("bad-edit", f"{CHART}/templates/ingress.yaml", 5),
        _llm("breaks-yaml", f"{CHART}/values.yaml", 1),
        _llm("rewrite-everything", "deploy/rbac.yaml", 1),
        _llm("low-confidence", f"{CHART}/values.yaml", 1, conf=0.3),
    ])
    before = {p: (repo / p).read_text() for p in (
        f"{CHART}/templates/deployment.yaml", f"{CHART}/values.yaml", "deploy/rbac.yaml",
        f"{CHART}/templates/ingress.yaml")}

    assert main(["fix", str(repo), "--host", ollama, "-q"]) == 0

    svc = (repo / CHART / "templates/service.yaml").read_text()
    assert 'include "webapp.selectorLabels" . | nindent 4' in svc and "app: webapp" not in svc
    # rejected / failed / render-breaking model edits leave files untouched
    for rel, text in before.items():
        assert (repo / rel).read_text() == text, rel
    # the chart still renders, and the Service now selects the pods
    rendered = lint.render(repo, CHART, "webapp")
    assert not rendered.error
    svc_doc = next(yaml.safe_load(d) for rel, d in rendered.docs if rel.endswith("service.yaml"))
    assert svc_doc["spec"]["selector"] == {"app.kubernetes.io/name": "webapp",
                                           "app.kubernetes.io/instance": "release-name"}

    report = (repo / ".helmreview" / "fix-report.md").read_text()
    assert "Declined by the model" in report and "false-positive" in report
    assert "did not match" in report and "invalid YAML" in report
    assert "stopped rendering" in report
    assert "rewrote too much" in report or "too large" in report
    assert "low-confidence" not in report               # below threshold: never sent
    assert "Needs a human" in report and "HR002" in report
    patch = (repo / ".helmreview" / "fix.patch").read_text()
    assert '+    {{- include "webapp.selectorLabels" . | nindent 4 }}' in patch
    assert "deployment.yaml" not in patch
    assert _git(repo, "diff", "--stat").stdout.strip()   # changes visible to git


@needs_git
def test_fix_dry_run_changes_nothing(repo, ollama):
    _write_report(repo, [_llm("selector-label-mismatch", f"{CHART}/templates/service.yaml", 13)])
    main(["fix", str(repo), "--host", ollama, "--dry-run", "-q"])
    assert _git(repo, "status", "--porcelain", "--", ".", ":(exclude).helmreview").stdout == ""
    assert "webapp.selectorLabels" in (repo / ".helmreview" / "fix.patch").read_text()


@needs_git
def test_fix_refuses_dirty_tree(repo):
    p = repo / CHART / "values.yaml"
    p.write_text(p.read_text() + "\n")
    assert main(["fix", str(repo), "--no-llm", "-q"]) == 2
    assert main(["fix", str(repo), "--no-llm", "-q", "--allow-dirty"]) == 0


def test_apply_edits_tolerates_whitespace_and_indent_shift():
    src = ("spec:\n  template:\n    spec:\n      containers:\n        - name: app\n"
           "          image: x:1   \n          ports:\n            - containerPort: 80\n")
    # model dropped 4 spaces of indentation and the trailing spaces
    edit = {"old": "    - name: app\n      image: x:1\n      ports:\n        - containerPort: 80",
            "new": "    - name: app\n      image: x:1\n      ports:\n        - containerPort: 8080"}
    out = _apply_edits(src, [edit])
    assert out is not None and "            - containerPort: 8080\n" in out
    assert yaml.safe_load(out)
    # ambiguous: same block twice -> refuse
    assert _apply_edits(src + src.split("containers:\n", 1)[1], [edit]) is None
    # excerpt marker copied into "old" -> refuse
    assert _apply_edits(src, [{"old": "- name: app\n...\n", "new": "x"}]) is None
    # CRLF file
    crlf = src.replace("\n", "\r\n")
    out = _apply_edits(crlf, [edit])
    assert out is not None and "8080\r\n" in out and "\n" not in out.replace("\r\n", "")


@needs_git
def test_untracked_non_chart_files_do_not_block_fix(repo):
    from helmreview.cli import _git_dirty
    (repo / "docs").mkdir()
    (repo / "docs" / "index.md").write_text("# docs\n")
    assert _git_dirty(repo) == []
    (repo / "docs" / "extra.yaml").write_text("a: 1\n")
    assert _git_dirty(repo) == ["docs/extra.yaml"]
    (repo / "docs" / "extra.yaml").unlink()
    p = repo / CHART / "templates" / "_helpers.tpl"
    p.write_text(p.read_text() + "\n")
    assert _git_dirty(repo) == [f"{CHART}/templates/_helpers.tpl"]
