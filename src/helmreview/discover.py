"""Find the reviewable units in a repository: Helm charts, and plain Kubernetes manifests
(Kustomize bases, Argo CD apps, raw YAML) outside the charts."""

from __future__ import annotations

from collections import defaultdict
from fnmatch import fnmatch
import os
from pathlib import Path
import re
import subprocess

import yaml

from .models import Unit

YAML_EXT = {".yml", ".yaml"}
CHART_EXT = YAML_EXT | {".tpl", ".txt", ".json"}
MAX_FILE_BYTES = 512 * 1024
# Files at the top of a chart that are part of what it ships.
CHART_TOP_FILES = re.compile(r"^(Chart\.ya?ml|requirements\.ya?ml|values[\w.-]*\.ya?ml|"
                             r"values\.schema\.json)$")
CHART_DIRS = ("templates", "crds", "ci")
SKIP_YAML = {".gitlab-ci.yml", ".pre-commit-config.yaml", ".helmreview.yml", ".helmreview.yaml",
             "docker-compose.yml", "docker-compose.yaml", "mkdocs.yml", ".yamllint",
             ".yamllint.yml", "Chart.lock", "renovate.json"}
PROJECT_MARKERS = ("Chart.yaml", "Chart.yml", "kustomization.yaml", "kustomization.yml",
                   "helmfile.yaml", "charts")


def is_excluded(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch(rel, p) or fnmatch(rel + "/", p) for p in patterns)


def _walk(root: Path, exclude: list[str]):
    """(path, rel) for every regular file, without following symlinks or entering
    excluded folders."""
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        rel_dir = d.relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        dirnames[:] = sorted(n for n in dirnames if not (d / n).is_symlink()
                             and not is_excluded(f"{rel_dir}/{n}".lstrip("/"), exclude))
        for name in sorted(filenames):
            p = d / name
            rel = f"{rel_dir}/{name}".lstrip("/")
            if p.is_symlink() or not p.is_file() or is_excluded(rel, exclude):
                continue
            yield p, rel


def chart_roots(rels: list[str]) -> list[str]:
    """Folders holding a Chart.yaml ('' for a chart at the repository root). A chart's
    templates/ folder never holds another chart."""
    roots = []
    for rel in rels:
        parts = rel.split("/")
        if parts[-1] in ("Chart.yaml", "Chart.yml") and "templates" not in parts[:-1]:
            roots.append("/".join(parts[:-1]))
    return sorted(set(roots), key=lambda r: (-r.count("/"), r))   # deepest first


def owning_chart(rel: str, roots: list[str]) -> str | None:
    """The deepest chart folder a file belongs to (subcharts own their own files)."""
    for r in roots:                      # deepest first
        if r == "" or rel == r or rel.startswith(r + "/"):
            return r
    return None


def _in_chart(rel_in_chart: str) -> bool:
    parts = rel_in_chart.split("/")
    if len(parts) == 1:
        return bool(CHART_TOP_FILES.match(parts[0]))
    return parts[0] in CHART_DIRS


def _is_manifest(path: Path) -> bool:
    """Plain Kubernetes YAML: at least one document with apiVersion and kind."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return False
    if "apiVersion" not in text or "kind" not in text or "{{" in text:
        return False
    try:
        for doc in yaml.safe_load_all(text):
            if isinstance(doc, dict) and "apiVersion" in doc and "kind" in doc:
                return True
    except yaml.YAMLError:
        return False
    return False


def chart_name(root: Path, chart_rel: str) -> str:
    try:
        data = yaml.safe_load((root / chart_rel / "Chart.yaml").read_text(errors="replace"))
        if isinstance(data, dict) and data.get("name"):
            return str(data["name"])
    except (OSError, yaml.YAMLError):
        pass
    return Path(chart_rel).name if chart_rel else (root.resolve().name or "chart")


def discover(root: Path, exclude: list[str]) -> list[Unit]:
    files = []
    for path, rel in _walk(root, exclude):
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        files.append((path, rel))
    roots = chart_roots([rel for _, rel in files])
    charts: dict[str, list[str]] = defaultdict(list)
    manifests: dict[str, list[str]] = defaultdict(list)
    for path, rel in files:
        chart = owning_chart(rel, roots)
        if chart is not None:
            inner = rel[len(chart) + 1:] if chart else rel
            if path.suffix.lower() in CHART_EXT and _in_chart(inner):
                charts[chart].append(rel)
            continue
        if path.suffix.lower() in YAML_EXT and path.name not in SKIP_YAML \
                and not rel.startswith(".github/") and _is_manifest(path):
            manifests["/".join(rel.split("/")[:-1]) or "."].append(rel)

    units = [Unit("chart", chart_label(root, r), sorted(f), path=r)
             for r, f in sorted(charts.items())]
    units += [Unit("manifests", d, sorted(f), path="" if d == "." else d)
              for d, f in sorted(manifests.items())]
    return units


def chart_label(root: Path, chart_rel: str) -> str:
    name = chart_name(root, chart_rel)
    if not chart_rel or Path(chart_rel).name == name:
        return chart_rel or name
    return f"{chart_rel} ({name})"


def is_project_root(p: Path) -> bool:
    """Shallow: this folder itself is a chart, a Kustomize base, or a chart repository
    (charts/<name>/Chart.yaml). Used for a --root folder, which may also be a folder that
    holds several repositories."""
    try:
        if any((p / m).is_file() for m in PROJECT_MARKERS[:-1]):
            return True
        charts = p / "charts"
        return charts.is_dir() and any((c / "Chart.yaml").is_file() for c in charts.iterdir()
                                       if c.is_dir())
    except OSError:
        return False


def looks_like_project(p: Path, max_depth: int = 3, max_entries: int = 3000) -> bool:
    """Is this folder a Helm chart, a folder of charts, or a folder of Kubernetes
    manifests? Bounded, so a huge folder does not stall the web UI."""
    try:
        if any((p / m).exists() for m in PROJECT_MARKERS[:-1]):
            return True
    except OSError:
        return False
    seen = 0
    yamls: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(p):
        d = Path(dirpath)
        depth = len(d.relative_to(p).parts)
        dirnames[:] = [n for n in sorted(dirnames) if not n.startswith(".")
                       and n != "node_modules" and depth < max_depth]
        for name in filenames:
            seen += 1
            if name in ("Chart.yaml", "Chart.yml", "kustomization.yaml", "kustomization.yml"):
                return True
            if Path(name).suffix.lower() in YAML_EXT and len(yamls) < 25:
                yamls.append(d / name)
        if seen > max_entries:
            break
    return any(_is_manifest(y) for y in yamls if y.stat().st_size <= 256 * 1024)


def git_changed_files(root: Path, base: str) -> set[str]:
    """Files changed versus `base` (committed + staged + unstaged + untracked)."""
    def run(*args: str) -> list[str]:
        try:
            out = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                                 text=True, check=True, timeout=60)
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"git {' '.join(args)} failed: {getattr(exc, 'stderr', exc)}") from exc
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]

    prefix = run("rev-parse", "--show-prefix")
    prefix = prefix[0] if prefix else ""
    files = set(run("diff", "--name-only", "--relative", f"{base}...HEAD"))
    files |= set(run("diff", "--name-only", "--relative", "HEAD"))
    files |= set(run("ls-files", "--others", "--exclude-standard"))
    return {f[len(prefix):] if prefix and f.startswith(prefix) else f for f in files}


HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def git_changed_lines(root: Path, base: str) -> dict[str, set[int] | None]:
    """Map file -> set of added/modified line numbers vs `base` (None = whole file is new)."""
    try:
        mb = subprocess.run(["git", "-C", str(root), "merge-base", base, "HEAD"],
                            capture_output=True, text=True, check=True, timeout=60).stdout.strip()
        out = subprocess.run(["git", "-C", str(root), "diff", "-U0", "--relative", "--no-color", mb],
                             capture_output=True, text=True, check=True, timeout=60).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"git diff vs {base} failed: {getattr(exc, 'stderr', exc)}") from exc
    result: dict[str, set[int] | None] = {}
    current = None
    for line in out.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            current = None if path == "/dev/null" else path[2:] if path.startswith("b/") else path
            if current is not None:
                result.setdefault(current, set())
        elif current is not None:
            m = HUNK_RE.match(line)
            if m:
                start, count = int(m.group(1)), int(m.group(2) or 1)
                lines = result[current]
                if lines is not None:
                    lines.update(range(start, start + count))
    for f in git_changed_files(root, base):
        if f not in result:
            result[f] = None
    return result


def filter_units(units: list[Unit], changed: set[str]) -> list[Unit]:
    """Keep units that contain at least one changed file (whole unit is still reviewed)."""
    return [u for u in units if any(f in changed for f in u.files)]
