"""Report writers: Markdown, JSON, SARIF 2.1.0, GitLab Code Quality, terminal."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path

from . import __version__
from .models import Finding, ScanResult, Severity
from .static import RULE_DOCS

ICON = {Severity.CRITICAL: "🟥", Severity.HIGH: "🟧", Severity.MEDIUM: "🟨",
        Severity.LOW: "🟦", Severity.INFO: "⬜"}

TXT = {
    "en": {"title": "Helm review", "summary": "Summary", "severity": "Severity",
           "count": "Count", "scanned": "Scanned", "units": "units", "model": "Model",
           "notes": "Reviewer notes per unit", "findings": "Findings", "none": "No findings. 🎉",
           "fix": "Fix", "problems": "Problems during the scan", "source": "source",
           "files": "files", "roles": "charts", "playbooks": "manifest folders"},
    "nl": {"title": "Helm-review", "summary": "Samenvatting", "severity": "Ernst",
           "count": "Aantal", "scanned": "Gescand", "units": "onderdelen", "model": "Model",
           "notes": "Opmerkingen per onderdeel", "findings": "Bevindingen",
           "none": "Geen bevindingen. 🎉", "fix": "Oplossing",
           "problems": "Problemen tijdens de scan", "source": "bron", "files": "bestanden",
           "roles": "charts", "playbooks": "manifestmappen"},
}


def _sorted(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (-f.severity, f.file, f.line or 0, f.rule))


def to_markdown(res: ScanResult, lang: str = "en", summaries: dict | None = None) -> str:
    t = TXT.get(lang, TXT["en"])
    counts = res.counts()
    kinds = defaultdict(int)
    for u in res.units:
        kinds[u.kind] += 1
    nfiles = len({f for u in res.units for f in u.files})

    out = [f"# {t['title']}: `{Path(res.root).name}`", ""]
    out.append(f"*{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC · helmreview {__version__}"
               + (f" · {t['model']}: `{res.model}`" if res.model else "") + "*")
    out += ["", f"## {t['summary']}", "",
            f"{t['scanned']}: {len(res.units)} {t['units']} ({kinds.get('chart', 0)} {t['roles']}, "
            f"{kinds.get('manifests', 0)} {t['playbooks']}), {nfiles} {t['files']}.", "",
            f"| {t['severity']} | {t['count']} |", "|---|---:|"]
    for sev in sorted(Severity, reverse=True):
        out.append(f"| {ICON[sev]} {sev.label} | {counts[sev.label]} |")
    by_source = defaultdict(int)
    for f in res.findings:
        by_source[f.source] += 1
    if by_source:
        out += ["", " · ".join(f"{k}: {v}" for k, v in sorted(by_source.items()))]

    if summaries:
        out += ["", f"## {t['notes']}", ""]
        for label, text in sorted(summaries.items()):
            out.append(f"- **{label}** — {text}")

    out += ["", f"## {t['findings']}", ""]
    if not res.findings:
        out.append(t["none"])
    by_file: dict[str, list[Finding]] = defaultdict(list)
    for f in _sorted(res.findings):
        by_file[f.file].append(f)
    for file in sorted(by_file, key=lambda k: (-max(x.severity for x in by_file[k]), k)):
        out += [f"### `{file}`", ""]
        for f in sorted(by_file[file], key=lambda x: (x.line or 0, -x.severity)):
            loc = f"L{f.line}" if f.line else "—"
            name = RULE_DOCS.get(f.rule, f.rule)
            conf = f", {int(f.confidence * 100)}%" if f.confidence is not None else ""
            out.append(f"- {ICON[f.severity]} **{f.severity.label}** · `{loc}` · "
                       f"`{f.rule}`{' (' + name + ')' if name != f.rule else ''} "
                       f"· _{f.source}, {f.category}{conf}_  ")
            out.append(f"  {f.message}")
            if f.suggestion:
                out.append(f"  ↳ **{t['fix']}:** {f.suggestion}")
        out.append("")

    if res.errors:
        out += [f"## {t['problems']}", ""] + [f"- {e}" for e in res.errors] + [""]
    return "\n".join(out).rstrip() + "\n"


def to_json(res: ScanResult, summaries: dict | None = None) -> str:
    return json.dumps({
        "tool": "helmreview", "version": __version__, "root": res.root, "model": res.model,
        "counts": res.counts(), "stats": res.stats, "errors": res.errors,
        "summaries": summaries or {},
        "units": [{"kind": u.kind, "name": u.name, "path": u.path, "files": u.files}
                  for u in res.units],
        "findings": [f.to_dict() for f in _sorted(res.findings)],
    }, indent=2, ensure_ascii=False)


def to_sarif(res: ScanResult) -> str:
    level = {Severity.CRITICAL: "error", Severity.HIGH: "error", Severity.MEDIUM: "warning",
             Severity.LOW: "note", Severity.INFO: "note"}
    rules = {}
    results = []
    for f in _sorted(res.findings):
        rules.setdefault(f.rule, {
            "id": f.rule, "name": RULE_DOCS.get(f.rule, f.rule),
            "shortDescription": {"text": RULE_DOCS.get(f.rule, f.rule)},
            "properties": {"tags": [f.category, f.source]},
        })
        loc = {"physicalLocation": {"artifactLocation": {"uri": f.file}}}
        if f.line:
            loc["physicalLocation"]["region"] = {"startLine": f.line}
        results.append({
            "ruleId": f.rule, "level": level[f.severity],
            "message": {"text": f.message + (f"\nFix: {f.suggestion}" if f.suggestion else "")},
            "locations": [loc],
            "partialFingerprints": {"helmreview/v1": f.fingerprint},
            "properties": {"severity": f.severity.label, "source": f.source,
                           **({"confidence": f.confidence} if f.confidence is not None else {})},
        })
    return json.dumps({
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0",
        "runs": [{"tool": {"driver": {"name": "helmreview", "version": __version__,
                                      "informationUri": "https://github.com/jwklijnsma/helmreview",
                                      "rules": list(rules.values())}},
                  "results": results}],
    }, indent=2, ensure_ascii=False)


def to_gitlab(res: ScanResult) -> str:
    """GitLab Code Quality report (shows inline in merge requests)."""
    sev = {Severity.CRITICAL: "blocker", Severity.HIGH: "critical", Severity.MEDIUM: "major",
           Severity.LOW: "minor", Severity.INFO: "info"}
    return json.dumps([{
        "description": f"[{f.rule}] {f.message}" + (f" — {f.suggestion}" if f.suggestion else ""),
        "check_name": f.rule,
        "fingerprint": f.fingerprint,
        "severity": sev[f.severity],
        "categories": [f.category.capitalize()],
        "location": {"path": f.file, "lines": {"begin": f.line or 1}},
    } for f in _sorted(res.findings)], indent=2, ensure_ascii=False)


def terminal_summary(res: ScanResult, limit: int = 25) -> str:
    counts = res.counts()
    lines = ["", "Results: " + "  ".join(
        f"{ICON[s]} {s.label} {counts[s.label]}" for s in sorted(Severity, reverse=True))]
    for f in _sorted(res.findings)[:limit]:
        loc = f"{f.file}:{f.line}" if f.line else f.file
        lines.append(f"  {ICON[f.severity]} {loc}  [{f.rule}] {f.message[:110]}")
    if len(res.findings) > limit:
        lines.append(f"  … {len(res.findings) - limit} more in the report")
    for e in res.errors:
        lines.append(f"  ! {e}")
    return "\n".join(lines)


WRITERS = {"md": ("report.md", None), "json": ("report.json", None),
           "sarif": ("report.sarif", None), "gitlab": ("gl-code-quality-report.json", None)}


def write_reports(res: ScanResult, out_dir: Path, formats: list[str], lang: str,
                  summaries: dict) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for fmt in formats:
        fmt = fmt.strip().lower()
        if fmt not in WRITERS:
            continue
        name = WRITERS[fmt][0]
        content = {
            "md": lambda: to_markdown(res, lang, summaries),
            "json": lambda: to_json(res, summaries),
            "sarif": lambda: to_sarif(res),
            "gitlab": lambda: to_gitlab(res),
        }[fmt]()
        path = out_dir / name
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written
