"""lint.py against a fake `helm` (helm lint / helm template parsing, path mapping,
missing-dependency retry), and how the Reviewer treats model output."""
import json
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from helmreview import lint
from helmreview.config import Config
from helmreview.models import Finding, Severity, Unit
from helmreview.ollama import OllamaError, OllamaTimeout
from helmreview.review import Reviewer, _prior_summary


# --------------------------------------------------------------------------- pure helpers

def test_parse_lint_joins_helm4_continuation_lines():
    out = textwrap.dedent("""\
        ==> Linting .
        [INFO] Chart.yaml: icon is recommended
        [WARNING] templates/deployment.yaml: object name does not conform
          to Kubernetes naming requirements
        [ERROR] templates/: template: web/templates/svc.yaml:12:5: executing "x"
        Error: 1 chart(s) linted, 1 chart(s) failed
        1 chart(s) linted, 0 chart(s) failed
        """)
    assert lint.parse_lint(out) == [
        ("INFO", "Chart.yaml", "icon is recommended"),
        ("WARNING", "templates/deployment.yaml", "object name does not conform to Kubernetes naming requirements"),
        ("ERROR", "templates/", 'template: web/templates/svc.yaml:12:5: executing "x"'),
    ]


@pytest.mark.parametrize("chart_rel,helm_path,expected", [
    ("charts/web", "web/templates/a.yaml", "charts/web/templates/a.yaml"),
    ("", "web/templates/a.yaml", "templates/a.yaml"),
    ("charts/web", "web/charts/sub/templates/b.yaml", "charts/web/charts/sub/templates/b.yaml"),
    ("charts/web", "other/templates/a.yaml", None),
])
def test_chart_file_maps_helm_paths(chart_rel, helm_path, expected):
    assert lint._chart_file(chart_rel, "web", helm_path) == expected


def test_one_line_flattens_helm4_errors():
    text = "Error: INSTALLATION FAILED:\n   template: web/templates/a.yaml:3:1:\n  bad\nUse --debug flag"
    assert lint._one_line(text) == "INSTALLATION FAILED: template: web/templates/a.yaml:3:1: bad"
    assert len(lint._one_line("x" * 2000)) == 600


def test_helm_env_never_reaches_a_cluster(monkeypatch):
    monkeypatch.setenv("KUBECONFIG", "/home/me/.kube/prod")
    env = lint._helm_env()
    assert env["KUBECONFIG"] == "/dev/null" and env["HELM_NO_PLUGINS"] == "1"
    assert "HELM_CACHE_HOME" in env


# --------------------------------------------------------------------------- fake helm

@pytest.fixture
def helm(tmp_path, monkeypatch):
    """A `helm` whose replies are scripted per sub-command in <tmp>/script.json:
    {"lint": [{"rc": 1, "out": "...", "err": "..."}, ...], "template": [...]}"""
    state = tmp_path / "helm-state"
    state.mkdir()
    exe = state / "helm"
    exe.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, os, pathlib, sys, time
        d = pathlib.Path({str(state)!r})
        calls = json.loads((d / "calls.json").read_text()) if (d / "calls.json").exists() else []
        calls.append({{"argv": sys.argv[1:], "cwd": os.getcwd(), "kubeconfig": os.environ.get("KUBECONFIG"),
                      "chart": (pathlib.Path("Chart.yaml").read_text() if pathlib.Path("Chart.yaml").exists() else "")}})
        (d / "calls.json").write_text(json.dumps(calls))
        if sys.argv[1] == "version":
            print("v3.17.0+gabc"); sys.exit(0)
        script = json.loads((d / "script.json").read_text())
        step = script[sys.argv[1]].pop(0)
        (d / "script.json").write_text(json.dumps(script))
        if step.get("sleep"):
            time.sleep(step["sleep"])
        sys.stdout.write(step.get("out", "")); sys.stderr.write(step.get("err", ""))
        sys.exit(step.get("rc", 0))
        """))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("HELMREVIEW_HELM", str(exe))

    class H:
        def script(self, **steps):
            (state / "script.json").write_text(json.dumps(steps))

        @property
        def calls(self):
            return json.loads((state / "calls.json").read_text())
    return H()


@pytest.fixture
def chart(tmp_path):
    root = tmp_path / "repo"
    c = root / "charts" / "web"
    (c / "templates").mkdir(parents=True)
    (c / "Chart.yaml").write_text("apiVersion: v2\nname: web\nversion: 0.1.0\n"
                                  "dependencies:\n  - name: redis\n    version: 1.x\n    repository: https://x\n")
    (c / "Chart.lock").write_text("lock")
    (c / "values.yaml").write_text("replicas: 1\n")
    (c / "templates" / "deployment.yaml").write_text("kind: Deployment\n")
    (c / "templates" / "svc.yaml").write_text("kind: Service\n")
    return root


def test_lint_findings_map_to_repo_files(helm, chart):
    helm.script(lint=[{"rc": 1, "out": (
        "[INFO] Chart.yaml: icon is recommended\n"
        "[WARNING] templates/deployment.yaml: not good\n"
        "[ERROR] templates/: template: web/templates/svc.yaml:12:5: bad thing\n"
        "[WARNING] somewhere/odd: vague\n")}])
    findings, errors = lint.lint_chart(chart, "charts/web", "web", strict=True)
    assert errors == []
    assert [(f.rule, f.severity, f.file, f.line, f.category) for f in findings] == [
        ("helm-lint:icon", Severity.INFO, "charts/web/Chart.yaml", None, "best-practice"),
        ("helm-lint:warning", Severity.LOW, "charts/web/templates/deployment.yaml", None, "best-practice"),
        ("helm-lint:error", Severity.HIGH, "charts/web/templates/svc.yaml", 12, "reliability"),
        ("helm-lint:warning", Severity.LOW, "charts/web/Chart.yaml", None, "best-practice"),   # unknown -> Chart.yaml
    ]
    call = helm.calls[0]
    assert call["argv"] == ["lint", ".", "--strict"] and call["cwd"].endswith("charts/web")
    assert call["kubeconfig"] == "/dev/null"


def test_lint_retries_without_missing_dependencies(helm, chart):
    helm.script(lint=[{"rc": 1, "out": "[ERROR] Chart.yaml: chart metadata is missing these dependencies: redis\n"},
                      {"rc": 0, "out": "[WARNING] templates/svc.yaml: fine otherwise\n"}])
    findings, _ = lint.lint_chart(chart, "charts/web", "web")
    assert [f.message for f in findings] == ["fine otherwise"]
    second = helm.calls[1]
    assert "helmreview-lint-" in second["cwd"] and "dependencies" not in second["chart"]
    assert (chart / "charts/web/Chart.yaml").read_text().count("redis") == 1     # original untouched
    assert (chart / "charts/web/Chart.lock").exists()


def test_lint_without_helm_and_timeouts(helm, chart, monkeypatch):
    helm.script(lint=[{"sleep": 5}])
    assert lint.lint_chart(chart, "charts/web", "web", timeout=1) == ([], ["helm lint charts/web timed out after 1s"])
    monkeypatch.setenv("HELMREVIEW_HELM", "definitely-not-helm")
    findings, errors = lint.lint_chart(chart, "", "web")
    assert findings == [] and errors[0].startswith("helm not found on PATH")
    assert lint.version() == ""
    assert lint.render(chart, "", "web").error == "helm not found on PATH"


def test_version(helm):
    assert lint.version() == "v3.17.0+gabc"


def test_render_splits_documents_by_source(helm, chart):
    out = ("---\n# Source: web/templates/deployment.yaml\nkind: Deployment\n"
           "---\n# Source: web/templates/svc.yaml\nkind: Service\n"
           "---\n# Source: web/charts/redis/templates/sts.yaml\nkind: StatefulSet\n"   # vendored .tgz: no file
           "---\n\n")
    helm.script(template=[{"out": out}])
    r = lint.render(chart, "charts/web", "web")
    assert r.error == "" and [src for src, _ in r.docs] == ["charts/web/templates/deployment.yaml",
                                                             "charts/web/templates/svc.yaml"]
    assert helm.calls[0]["argv"] == ["template", "release-name", ".", "--namespace", "default"]


def test_render_error_points_at_the_template(helm, chart):
    helm.script(template=[{"rc": 1, "err": "Error: template: web/templates/svc.yaml:7:3: executing \"x\"\nUse --debug"}])
    r = lint.render(chart, "charts/web", "web")
    assert (r.error_file, r.error_line) == ("charts/web/templates/svc.yaml", 7)
    assert r.error.startswith('template: web/templates/svc.yaml:7:3') and not r.docs
    helm.script(template=[{"rc": 1, "err": "Error: YAML parse error on web/templates/deployment.yaml: bad"}])
    r = lint.render(chart, "charts/web", "web")
    assert (r.error_file, r.error_line) == ("charts/web/templates/deployment.yaml", None)


def test_render_without_dependencies_adds_a_note(helm, chart):
    helm.script(template=[{"rc": 1, "err": "Error: found in Chart.yaml, but missing in charts/ directory: redis"},
                          {"out": "---\n# Source: web/templates/svc.yaml\nkind: Service\n"}])
    r = lint.render(chart, "charts/web", "web")
    assert r.note.startswith("dependencies are not in charts/") and len(r.docs) == 1


# --------------------------------------------------------------------------- review.py

class FakeClient:
    model, host, timeout = "test-model", "http://ollama", 600

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def chat_json(self, system, user, schema):
        self.calls.append(user)
        r = self.replies.pop(0) if self.replies else {"findings": []}
        if isinstance(r, Exception):
            raise r
        return r(user) if callable(r) else r


def reviewer(root, replies, **cfg):
    c = Config()
    for k, v in cfg.items():
        setattr(c, k, v)
    return Reviewer(root, c, FakeClient(replies), use_cache=False)


def test_model_output_is_sanitised(chart):
    unit = Unit("chart", "web", ["charts/web/templates/deployment.yaml", "charts/web/templates/svc.yaml"],
                path="charts/web")
    reply = {"summary": "ok", "findings": [
        {"file": "templates/svc.yaml", "line": 1, "rule": "Selector Mismatch", "severity": "high",
         "message": "m", "confidence": 0.95},
        {"file": "charts/web/templates/deployment.yaml", "line": 40, "message": "out of range", "confidence": 0.9},
        {"file": "templates/x.yaml", "message": "unknown", "confidence": 0.9},
        {"file": "charts/web/templates/svc.yaml", "message": "unsure", "confidence": 0.1},
        {"file": "charts/web/templates/svc.yaml", "message": ""},
        7,
    ]}
    r = reviewer(chart, [reply], per_file=False)
    out = r._review_chunk(unit, 1, 1, unit.files, "src", [])
    assert [(f.file, f.line, f.rule) for f in out] == [
        ("charts/web/templates/svc.yaml", 1, "llm:selector-mismatch"),
        ("charts/web/templates/deployment.yaml", None, "llm:llm"),
    ]
    assert out[0].severity == Severity.HIGH


def test_timeouts_split_and_are_cached(chart):
    unit = Unit("chart", "web", ["charts/web/templates/deployment.yaml", "charts/web/templates/svc.yaml"],
                path="charts/web")
    r = reviewer(chart, [OllamaTimeout("slow"), {"findings": []}, {"findings": []}], per_file=False)
    assert r._review_split(unit, 1, 1, unit.files, "src", [], 8000) == []
    assert len(r.client.calls) == 3
    r2 = reviewer(chart, [OllamaTimeout("slow")], per_file=False)
    with pytest.raises(OllamaTimeout):
        r2._review_split(unit, 1, 1, unit.files, "src", [], 3000)


def test_review_records_errors_per_job(chart):
    units = [Unit("chart", "web", ["charts/web/templates/deployment.yaml", "charts/web/templates/svc.yaml"],
                  path="charts/web")]

    def by_file(user):
        if "svc.yaml =====" in user:
            raise OllamaError("crash")
        return {"findings": []}

    r = reviewer(chart, [by_file, by_file])
    assert r.review(units, []) == []
    assert r.errors == ["charts/web/templates/svc.yaml: OllamaError: crash"]


def test_prior_summary_caps():
    rows = [Finding("r", Severity.LOW, "a.yaml", i, "msg", "x") for i in range(61)]
    assert _prior_summary(rows, ["a.yaml"]).endswith("- ... and 1 more")
    assert _prior_summary(rows, []) == "(none)"


# --------------------------------------------------------------------------- report formats

from helmreview import report  # noqa: E402
from helmreview.models import ScanResult  # noqa: E402


def sample_result():
    return ScanResult(root="/r", units=[], errors=["helm not found"], findings=[
        Finding("helm-lint:error", Severity.HIGH, "charts/web/templates/svc.yaml", 12, "bad", "helm-lint",
                category="reliability", suggestion="fix it"),
        Finding("llm:selector-mismatch", Severity.MEDIUM, "charts/web/values.yaml", None, "m", "llm",
                category="security", confidence=0.9),
        Finding("helm-lint:icon", Severity.INFO, "charts/web/Chart.yaml", None, "icon", "helm-lint"),
    ])


def test_sarif_is_valid_shape():
    doc = json.loads(report.to_sarif(sample_result()))
    assert doc["version"] == "2.1.0"
    run = doc["runs"][0]
    assert {r["id"] for r in run["tool"]["driver"]["rules"]} == {"helm-lint:error", "llm:selector-mismatch",
                                                                  "helm-lint:icon"}
    first = run["results"][0]
    assert first["level"] == "error" and first["message"]["text"] == "bad\nFix: fix it"
    assert first["locations"][0]["physicalLocation"]["region"] == {"startLine": 12}
    assert "helmreview/v1" in first["partialFingerprints"]
    llm = next(r for r in run["results"] if r["ruleId"].startswith("llm:"))
    assert llm["level"] == "warning" and llm["properties"]["confidence"] == 0.9
    assert "region" not in llm["locations"][0]["physicalLocation"]
    assert [r["level"] for r in run["results"]][-1] == "note"


def test_gitlab_code_quality():
    items = json.loads(report.to_gitlab(sample_result()))
    assert [i["severity"] for i in items] == ["critical", "major", "info"]
    assert items[0]["description"] == "[helm-lint:error] bad — fix it"
    assert items[1]["location"] == {"path": "charts/web/values.yaml", "lines": {"begin": 1}}
    assert items[0]["categories"] == ["Reliability"]
    assert len({i["fingerprint"] for i in items}) == 3


def test_terminal_summary_limits_and_errors():
    text = report.terminal_summary(sample_result(), limit=2)
    assert "charts/web/templates/svc.yaml:12  [helm-lint:error] bad" in text
    assert "… 1 more in the report" in text and "! helm not found" in text


def test_write_reports_skips_unknown_formats(tmp_path):
    written = report.write_reports(sample_result(), tmp_path / "out", ["md", "SARIF", "gitlab", "pdf"], "en", {})
    assert sorted(p.name for p in written) == ["gl-code-quality-report.json", "report.md", "report.sarif"]
    assert json.loads((tmp_path / "out" / "report.sarif").read_text())["version"] == "2.1.0"
