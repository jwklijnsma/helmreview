"""helm lint and helm template: lint findings, and the rendered manifests the static checks
look at."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import yaml

from .models import Finding, Severity

LINT_LINE = re.compile(r"^\[(INFO|WARNING|ERROR)\]\s+([^:]*?):\s*(.*)$")
# "template: mychart/templates/deployment.yaml:12:5: executing ..."
TEMPLATE_POS = re.compile(r"(?:template: )?([\w.-]+(?:/charts/[\w.-]+)*/templates/[^\s:]+):(\d+)")
YAML_ERR_ON = re.compile(r"(?:YAML parse error on|error converting YAML) ?([\w.-]+(?:/charts/[\w.-]+)*/"
                         r"templates/[^\s:]+)?")
SOURCE_RE = re.compile(r"^# Source: (.+)$", re.M)
MISSING_DEPS = re.compile(r"missing in charts/ directory|missing these dependencies", re.I)
SEVERITY = {"ERROR": Severity.HIGH, "WARNING": Severity.LOW, "INFO": Severity.INFO}
RELEASE = "release-name"
NAMESPACE = "default"


def available() -> str | None:
    return shutil.which(os.environ.get("HELMREVIEW_HELM", "helm"))


def version() -> str:
    exe = available()
    if not exe:
        return ""
    try:
        out = subprocess.run([exe, "version", "--short"], capture_output=True, text=True,
                             timeout=30)
        return out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _helm_env() -> dict:
    """helm with no cluster and no plugins: it must never talk to a cluster from a review."""
    env = dict(os.environ)
    env.setdefault("HELM_CACHE_HOME", os.path.join(tempfile.gettempdir(), "helmreview-helm-cache"))
    env["KUBECONFIG"] = os.devnull
    env["HELM_NO_PLUGINS"] = "1"
    return env


def _run(args: list[str], cwd: Path, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run([available() or "helm", *args], cwd=cwd, capture_output=True,
                          text=True, timeout=timeout, env=_helm_env())


def _chart_file(chart_rel: str, chart_name: str, helm_path: str) -> str | None:
    """Map a path as helm prints it (mychart/templates/x.yaml, mychart/charts/sub/...) to a
    path relative to the repository."""
    parts = helm_path.strip().split("/")
    if not parts or parts[0] != chart_name:
        return None
    inner = "/".join(parts[1:])
    return f"{chart_rel}/{inner}" if chart_rel else inner


@dataclass
class Rendered:
    """Output of helm template for one chart."""
    docs: list[tuple[str, str]] = field(default_factory=list)   # (repo-relative source, YAML)
    error: str = ""
    error_file: str | None = None
    error_line: int | None = None
    note: str = ""                       # e.g. dependencies were left out


def _strip_dependencies(src: Path, dst: Path):
    """Copy a chart without its (unvendored) dependencies, so the chart itself renders."""
    shutil.copytree(src, dst, symlinks=False, ignore=shutil.ignore_patterns(".git"))
    chart = dst / "Chart.yaml"
    data = yaml.safe_load(chart.read_text(errors="replace")) or {}
    data.pop("dependencies", None)
    chart.write_text(yaml.safe_dump(data, sort_keys=False))
    for name in ("Chart.lock", "requirements.yaml", "requirements.lock"):
        (dst / name).unlink(missing_ok=True)


def render(root: Path, chart_rel: str, chart_name: str, timeout: int = 300) -> Rendered:
    out = Rendered()
    if not available():
        out.error = "helm not found on PATH"
        return out
    chart_dir = root / chart_rel if chart_rel else root
    args = ["template", RELEASE, ".", "--namespace", NAMESPACE]
    try:
        proc = _run(args, chart_dir, timeout)
        if proc.returncode != 0 and MISSING_DEPS.search(proc.stderr):
            with tempfile.TemporaryDirectory(prefix="helmreview-render-") as tmp:
                copy = Path(tmp) / chart_name
                _strip_dependencies(chart_dir, copy)
                proc = _run(args, copy, timeout)
            out.note = ("dependencies are not in charts/ (run `helm dependency build`); "
                        "rendered the chart without them")
    except subprocess.TimeoutExpired:
        out.error = f"helm template timed out after {timeout}s"
        return out
    except (OSError, yaml.YAMLError) as exc:
        out.error = f"helm template failed: {exc}"
        return out
    if proc.returncode != 0:
        out.error = _one_line(proc.stderr or proc.stdout) or "helm template failed"
        msg = out.error
        m = TEMPLATE_POS.search(msg)
        if m:
            out.error_file = _chart_file(chart_rel, chart_name, m.group(1))
            out.error_line = int(m.group(2))
        else:
            m = YAML_ERR_ON.search(msg)
            if m and m.group(1):
                out.error_file = _chart_file(chart_rel, chart_name, m.group(1))
        return out
    pieces = re.split(r"^---\s*$", proc.stdout, flags=re.M)
    for piece in pieces:
        m = SOURCE_RE.search(piece)
        if not m or not piece.strip():
            continue
        rel = _chart_file(chart_rel, chart_name, m.group(1))
        if rel and (root / rel).is_file():          # vendored .tgz dependencies are skipped
            out.docs.append((rel, piece))
    return out


def _one_line(text: str) -> str:
    """helm 4 spreads one error over several indented lines; make it one readable line."""
    lines = [l.strip() for l in text.splitlines() if l.strip()
             and not l.startswith("Use --debug")]
    return re.sub(r"^Error: ", "", " ".join(lines))[:600]


def parse_lint(stdout: str) -> list[tuple[str, str, str]]:
    """[(level, where, message)] from helm lint output; continuation lines (helm 4) are
    joined to their entry."""
    out: list[list[str]] = []
    for raw in stdout.splitlines():
        m = LINT_LINE.match(raw.strip())
        if m:
            out.append(list(m.groups()))
        elif out and raw.strip() and not raw.startswith(("==>", "Error:")) \
                and not re.match(r"^\d+ chart\(s\) linted", raw.strip()):
            out[-1][2] = (out[-1][2] + " " + raw.strip()).strip()
    return [tuple(x) for x in out]


def lint_chart(root: Path, chart_rel: str, chart_name: str, strict: bool = False,
               timeout: int = 300) -> tuple[list[Finding], list[str]]:
    if not available():
        return [], ["helm not found on PATH; helm lint and rendering skipped "
                    "(https://helm.sh/docs/intro/install/)"]
    chart_dir = root / chart_rel if chart_rel else root
    args = ["lint", "."] + (["--strict"] if strict else [])
    try:
        proc = _run(args, chart_dir, timeout)
        if proc.returncode != 0 and MISSING_DEPS.search(proc.stdout + proc.stderr):
            with tempfile.TemporaryDirectory(prefix="helmreview-lint-") as tmp:
                copy = Path(tmp) / chart_name
                _strip_dependencies(chart_dir, copy)
                proc = _run(args, copy, timeout)
    except subprocess.TimeoutExpired:
        return [], [f"helm lint {chart_rel or '.'} timed out after {timeout}s"]
    except (OSError, yaml.YAMLError) as exc:
        return [], [f"helm lint {chart_rel or '.'} failed: {exc}"]
    findings = []
    chart_yaml = f"{chart_rel}/Chart.yaml" if chart_rel else "Chart.yaml"
    for level, where, msg in parse_lint(proc.stdout):
        if MISSING_DEPS.search(msg):
            continue
        file, line = None, None
        pos = TEMPLATE_POS.search(msg)
        if pos:
            file = _chart_file(chart_rel, chart_name, pos.group(1))
            line = int(pos.group(2))
        if not file:
            where = where.strip().rstrip("/")
            cand = f"{chart_rel}/{where}" if chart_rel else where
            file = cand if where and (root / cand).is_file() else chart_yaml
        if level == "INFO" and "icon is recommended" in msg:
            sev, rule = Severity.INFO, "helm-lint:icon"
        else:
            sev, rule = SEVERITY[level], f"helm-lint:{level.lower()}"
        findings.append(Finding(rule=rule, severity=sev, file=file, line=line,
                                message=msg.strip(), source="helm-lint",
                                category="reliability" if level == "ERROR" else "best-practice"))
    return findings, []
