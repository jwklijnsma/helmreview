import json
from pathlib import Path
import shutil
import subprocess

import pytest

from helmreview import lint
from helmreview.cli import main
from helmreview.config import load_config
from helmreview.discover import discover, looks_like_project
from helmreview.models import Severity
from helmreview.ollama import parse_json_lenient
from helmreview.static import run_static

import mock_ollama

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sample-repo"
needs_helm = pytest.mark.skipif(not shutil.which("helm"), reason="helm not installed")


@pytest.fixture()
def repo(tmp_path):
    dst = tmp_path / "repo"
    shutil.copytree(SAMPLE, dst)
    return dst


@pytest.fixture()
def ollama():
    srv, url = mock_ollama.start()
    yield url
    srv.shutdown()


def rules_of(findings):
    return {f.rule for f in findings}


def write(root: Path, rel: str, text: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_discover_units(repo):
    units = discover(repo, [])
    by_kind = {(u.kind, u.name): u for u in units}
    chart = by_kind[("chart", "charts/webapp")]
    assert chart.path == "charts/webapp"
    assert "charts/webapp/templates/_helpers.tpl" in chart.files
    assert "charts/webapp/templates/NOTES.txt" in chart.files
    assert "charts/webapp/README.md" not in chart.files
    assert by_kind[("manifests", "deploy")].files == ["deploy/rbac.yaml"]
    assert looks_like_project(repo) and looks_like_project(repo / "charts" / "webapp")


def test_discover_subcharts_root_chart_and_skips(tmp_path):
    write(tmp_path, "Chart.yaml", "apiVersion: v2\nname: top\nversion: 1.0.0\n")
    write(tmp_path, "values.yaml", "a: 1\n")
    write(tmp_path, "templates/cm.yaml", "kind: ConfigMap\n")
    write(tmp_path, "charts/sub/Chart.yaml", "apiVersion: v2\nname: sub\nversion: 1.0.0\n")
    write(tmp_path, "charts/sub/templates/svc.yaml", "kind: Service\n")
    write(tmp_path, "templates/files/Chart.yaml", "not a chart inside templates\n")
    write(tmp_path, ".github/workflows/ci.yaml", "apiVersion: x\nkind: y\n")
    units = discover(tmp_path, [".github/**"])
    names = {u.name: u for u in units}
    assert set(names) == {"top", "charts/sub"}
    assert "charts/sub/templates/svc.yaml" not in names["top"].files
    assert names["charts/sub"].files == ["charts/sub/Chart.yaml", "charts/sub/templates/svc.yaml"]


def test_not_a_project(tmp_path):
    write(tmp_path, "notes/readme.yaml", "just: data\n")
    assert not looks_like_project(tmp_path)
    write(tmp_path, "k8s/app.yaml", "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: x}\n")
    assert looks_like_project(tmp_path)


def test_static_values_chart_and_manifests_without_helm(repo, monkeypatch):
    monkeypatch.setenv("HELMREVIEW_HELM", "helm-that-does-not-exist")
    units = discover(repo, [])
    findings, errors, _ = run_static(repo, units)
    assert {"HR001", "HR011", "HR017", "HR018", "HR019"} <= rules_of(findings)
    assert any("helm not found" in e for e in errors)
    hr001 = [f for f in findings if f.rule == "HR001"]
    assert [(f.file, f.line) for f in hr001] == [("charts/webapp/values.yaml", 15)]
    # secret values are masked, placeholders and secret names are not secrets
    assert not any("Sup3rS3cret" in f.message for f in findings)
    assert not any("apiToken" in f.message or "existingSecret" in f.message for f in hr001)
    rbac = next(f for f in findings if f.rule == "HR011")
    assert (rbac.file, rbac.line) == ("deploy/rbac.yaml", 14)


def test_static_manifest_pod_checks_exact_lines(tmp_path):
    write(tmp_path, "k8s/app.yaml", """apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: agent
spec:
  template:
    spec:
      hostNetwork: true
      volumes:
        - name: root
          hostPath:
            path: /
      containers:
        - name: agent
          image: busybox
          securityContext:
            runAsUser: 0
            capabilities:
              add: [SYS_ADMIN]
---
apiVersion: batch/v1beta1
kind: CronJob
metadata:
  name: job
spec:
  jobTemplate:
    spec:
      template:
        spec:
          securityContext:
            runAsNonRoot: true
            seccompProfile: {type: RuntimeDefault}
          containers:
            - name: j
              image: registry.example.com/j@sha256:abc
              securityContext:
                allowPrivilegeEscalation: false
                readOnlyRootFilesystem: true
                capabilities: {drop: [ALL]}
              resources:
                requests: {cpu: 10m, memory: 16Mi}
                limits: {memory: 32Mi}
""")
    findings, errors, _ = run_static(tmp_path, discover(tmp_path, []))
    assert not errors
    got = {(f.rule, f.line) for f in findings}
    assert {("HR007", 8), ("HR008", 11), ("HR006", 15), ("HR004", 17), ("HR010", 19),
            ("HR012", 21)} <= got
    # the hardened CronJob container gets no hardening/resources/probe findings
    assert not any(f.line and f.line > 22 and f.rule in {"HR003", "HR005", "HR009", "HR006"}
                   for f in findings)


def test_static_disable_rule(repo, monkeypatch):
    monkeypatch.setenv("HELMREVIEW_HELM", "nope")
    findings, _, _ = run_static(repo, discover(repo, []), disabled=["HR001"])
    assert "HR001" not in rules_of(findings)


@needs_helm
def test_rendered_chart_checks_point_at_templates(repo):
    findings, errors, notes = run_static(repo, discover(repo, []))
    assert not errors
    assert any("dependencies are not in charts/" in n for n in notes)
    got = {(f.rule, f.file.rsplit("/", 1)[-1], f.line) for f in findings}
    assert ("HR002", "deployment.yaml", 23) in got           # privileged: true
    assert ("HR006", "deployment.yaml", 20) in got           # image tag latest
    assert ("HR014", "deployment.yaml", 5) in got            # namespace: production
    assert ("HR012", "ingress.yaml", 2) in got               # networking.k8s.io/v1beta1
    assert ("HR013", "service.yaml", 8) in got               # NodePort


@needs_helm
def test_render_error_maps_to_template_line(tmp_path):
    write(tmp_path, "Chart.yaml", "apiVersion: v2\nname: broken\nversion: 0.1.0\n")
    write(tmp_path, "templates/cm.yaml",
          "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\ndata:\n  a: {{ include \"nope\" . }}\n")
    rendered = lint.render(tmp_path, "", "broken")
    assert rendered.error and rendered.error_file == "templates/cm.yaml" and rendered.error_line == 6
    found, errs = lint.lint_chart(tmp_path, "", "broken")
    assert not errs
    err = next(f for f in found if f.rule == "helm-lint:error")
    assert (err.file, err.line, err.severity) == ("templates/cm.yaml", 6, Severity.HIGH)


def test_parse_lint_joins_helm4_continuation_lines():
    out = ("==> Linting .\n[INFO] Chart.yaml: icon is recommended\n"
           "[ERROR] templates/: demo/templates/deployment.yaml:9:15\n"
           "  executing \"demo/templates/deployment.yaml\" at <include \"nope\" .>:\n"
           "    error calling include:\ntemplate: no template \"nope\"\n\n"
           "Error: 1 chart(s) linted, 1 chart(s) failed\n")
    rows = lint.parse_lint(out)
    assert rows[0] == ("INFO", "Chart.yaml", "icon is recommended")
    assert rows[1][0] == "ERROR" and rows[1][2].endswith('template: no template "nope"')
    assert "chart(s) linted" not in rows[1][2]


def test_parse_json_lenient():
    assert parse_json_lenient('```json\n{"findings": []}\n```') == {"findings": []}
    assert parse_json_lenient('Sure! {"summary": "x", "findings": []} hope it helps') \
        == {"summary": "x", "findings": []}
    assert parse_json_lenient('[{"rule": "a"}]') == {"findings": [{"rule": "a"}]}
    with pytest.raises(ValueError):
        parse_json_lenient("no json here")


def test_severity_parse():
    assert Severity.parse("major") == Severity.HIGH
    assert Severity.parse("CRITICAL") == Severity.CRITICAL
    assert Severity.parse("weird") == Severity.MEDIUM


def test_repo_config_cannot_redirect_host(repo, monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.delenv("HELMREVIEW_TRUST_REPO_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(repo / "nohome"))
    (repo / ".helmreview.yml").write_text("ollama_host: http://evil.example:11434\nmodel: m:1\n")
    cfg, warnings = load_config(repo)
    assert cfg.ollama_host == "http://localhost:11434"
    assert cfg.model == "m:1"
    assert any("ollama_host" in w for w in warnings)


@needs_helm
def test_full_scan_with_mock_ollama(repo, ollama, tmp_path):
    out = tmp_path / "out"
    code = main(["scan", str(repo), "--host", ollama, "-q",
                 "-f", "md,json,sarif,gitlab", "-o", str(out)])
    assert code == 1  # high findings present, default fail_on=high
    data = json.loads((out / "report.json").read_text())
    rules = {f["rule"] for f in data["findings"]}
    assert {"llm:selector-label-mismatch", "HR002", "helm-lint:icon"} <= rules
    assert "llm:maybe" not in rules        # filtered: low confidence
    assert "llm:ghost" not in rules        # filtered: file not in the unit
    assert data["model"] == "qwen2.5-coder:14b"
    # a template under review gets values.yaml as context
    prompts = [c["messages"][-1]["content"] for c in mock_ollama._Handler.calls]
    dep = next(p for p in prompts if "FILE: charts/webapp/templates/deployment.yaml" in p)
    assert "Context only" in dep and "replicaCount: 2" in dep
    sarif = json.loads((out / "report.sarif").read_text())
    assert sarif["version"] == "2.1.0" and sarif["runs"][0]["results"]
    gl = json.loads((out / "gl-code-quality-report.json").read_text())
    assert all({"fingerprint", "severity", "location"} <= set(x) for x in gl)
    md = (out / "report.md").read_text()
    assert "## Findings" in md and "1 charts, 1 manifest folders" in md


def test_dutch_report(repo, ollama, tmp_path):
    out = tmp_path / "out"
    main(["scan", str(repo), "--host", ollama, "--no-lint", "--no-static", "-q", "--lang", "nl",
          "-o", str(out)])
    md = (out / "report.md").read_text()
    assert "## Bevindingen" in md and "Helm-review" in md


def test_unreachable_ollama_degrades(repo, tmp_path):
    out = tmp_path / "out"
    code = main(["scan", str(repo), "--host", "http://127.0.0.1:9", "--no-lint", "-q",
                 "-o", str(out), "--timeout", "2"])
    data = json.loads((out / "report.json").read_text())
    assert code == 1 and any("not reachable" in e for e in data["errors"])
    assert main(["scan", str(repo), "--host", "http://127.0.0.1:9", "--no-lint", "-q",
                 "-o", str(out), "--require-llm"]) == 2


def test_fail_on_threshold(repo, tmp_path):
    out = tmp_path / "out"
    assert main(["scan", str(repo), "--no-llm", "--no-lint", "-q", "-o", str(out),
                 "--fail-on", "critical", "--disable", "HR002"]) == 0


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


@pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
def test_diff_mode_only_reports_changed_lines(repo, tmp_path):
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    _git(repo, "branch", "-M", "main")
    _git(repo, "checkout", "-qb", "feature")
    with (repo / "deploy" / "rbac.yaml").open("a") as fh:
        fh.write("---\napiVersion: v1\nkind: Pod\nmetadata:\n  name: debug\nspec:\n"
                 "  hostPID: true\n  containers: []\n")
    _git(repo, "commit", "-qam", "change")
    out = tmp_path / "out"
    main(["scan", str(repo), "--diff", "main", "--no-llm", "--no-lint", "-q", "-o", str(out)])
    data = json.loads((out / "report.json").read_text())
    assert [f["rule"] for f in data["findings"]] == ["HR007"]
