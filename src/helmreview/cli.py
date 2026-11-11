"""helmreview command line."""

from __future__ import annotations

import argparse
from fnmatch import fnmatch
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from . import __version__, lint
from .config import Config, load_config
from .discover import chart_name, discover, filter_units, git_changed_files, git_changed_lines
from .fix import Fixer, fix_report_md
from .models import Finding, ScanResult, Severity
from .ollama import OllamaClient
from .perf import speed_check
from .report import terminal_summary, write_reports
from .review import Reviewer
from .static import RULE_DOCS, run_static

EXIT_OK, EXIT_FINDINGS, EXIT_ERROR = 0, 1, 2

SAMPLE_CONFIG = """\
# helmreview configuration. Put it in the repo root or in ~/.helmreview.yml.
# ollama_host and api_key are only honoured from ~/.helmreview.yml, env or CLI
# (a repo cannot redirect your review to another server).

model: qwen2.5-coder:14b        # any Ollama model; qwen3-coder:30b if VRAM allows
language: en                    # en | nl
num_ctx: 16384                  # context window sent to Ollama
workers: 1                      # parallel requests; raise if OLLAMA_NUM_PARALLEL > 1
fail_on: high                   # exit 1 when a finding is at/above: info|low|medium|high|critical
min_severity: info              # hide findings below this level in reports
min_confidence: 0.5             # drop model findings it is unsure about
lint_strict: false              # helm lint --strict
disabled_rules: []              # e.g. [HR009, helm-lint:icon]
exclude:
  - ".git/**"
  - "**/charts/*.tgz"

# Review GitLab merge requests (`helmreview watch-mrs`); only honoured from ~/.helmreview.yml.
# The token comes from $GITLAB_TOKEN (scope: api).
# gitlab_url: https://gitlab.example.nl
# gitlab_projects: [platform/charts]
# gitlab_groups: []

house_rules: |
  - Every value in values.yaml has a comment explaining it.
  - Images come from our registry (registry.example.com), never docker.io directly.
  - Charts must install on OpenShift with the restricted-v2 SCC.
"""


def _progress(quiet: bool):
    def emit(msg: str):
        if not quiet:
            print(msg, file=sys.stderr, flush=True)
    return emit


def make_client(cfg: Config, temperature: float | None = None) -> OllamaClient:
    return OllamaClient(cfg.ollama_host, cfg.model, api_key=cfg.api_key,
                        timeout=int(cfg.timeout), retries=int(cfg.retries),
                        temperature=float(cfg.temperature if temperature is None else temperature),
                        num_ctx=int(cfg.num_ctx), num_predict=int(cfg.num_predict),
                        deadline=int(cfg.deadline), keep_alive=str(cfg.keep_alive))


def _apply_cli(cfg: Config, a: argparse.Namespace):
    for attr, key in [("model", "model"), ("host", "ollama_host"), ("lang", "language"),
                      ("workers", "workers"), ("num_ctx", "num_ctx"), ("fail_on", "fail_on"),
                      ("min_severity", "min_severity"), ("timeout", "timeout"),
                      ("min_confidence", "min_confidence")]:
        val = getattr(a, attr, None)
        if val is not None:
            setattr(cfg, key, val)
    if getattr(a, "no_llm", False):
        cfg.use_llm = False
    if getattr(a, "no_lint", False):
        cfg.use_lint = False
    if getattr(a, "strict", False):
        cfg.lint_strict = True
    if getattr(a, "no_static", False):
        cfg.use_static = False
    for pat in getattr(a, "exclude", None) or []:
        cfg.exclude.append(pat)
    for rule in getattr(a, "disable", None) or []:
        cfg.disabled_rules.append(rule)
    if "://" not in cfg.ollama_host:
        cfg.ollama_host = "http://" + cfg.ollama_host
    cfg.ollama_host = cfg.ollama_host.rstrip("/")


def touches_change(f, changed_lines: dict) -> bool:
    """True if the finding's task/line range overlaps an added or modified line."""
    lines = changed_lines.get(f.file, set())
    if lines is None or f.line is None:      # new file, or file-level finding
        return True
    end = f.end_line if f.end_line and f.end_line >= f.line else f.line + 2
    return any(f.line <= n <= end for n in lines)


def cmd_scan(a: argparse.Namespace) -> int:
    root = Path(a.path).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return EXIT_ERROR
    cfg, warnings = load_config(root, Path(a.config) if a.config else None)
    _apply_cli(cfg, a)
    say = _progress(a.quiet)
    for w in warnings:
        say(f"warning: {w}")
    t0 = time.time()

    units = discover(root, cfg.exclude)
    changed: set[str] | None = None
    changed_lines: dict[str, set[int] | None] = {}
    if a.diff:
        try:
            changed = git_changed_files(root, a.diff)
            if a.diff_scope == "lines":
                changed_lines = git_changed_lines(root, a.diff)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ERROR
        units = filter_units(units, changed)
        say(f"Diff mode vs {a.diff}: {len(changed)} changed file(s), {len(units)} unit(s) affected")
    if a.only:
        units = [u for u in units if any(
            fnmatch(u.name, p) or any(fnmatch(f, p) or f.startswith(p.rstrip("/") + "/")
                                      for f in u.files) for p in a.only)]
    say(f"helmreview {__version__}: {root.name} — {len(units)} unit(s): "
        + ", ".join(f"{k} {sum(1 for u in units if u.kind == k)}"
                    for k in ("chart", "manifests")))
    if not units:
        say("Nothing to review.")

    files = sorted({f for u in units for f in u.files})
    scope = sorted(changed & set(files)) if changed is not None else files
    res = ScanResult(root=str(root), units=units, findings=[])

    in_scope = set(scope)
    if cfg.use_lint and units:
        if not lint.available():
            res.errors.append("helm not found on PATH; helm lint and rendered checks skipped "
                              "(https://helm.sh/docs/intro/install/)")
        else:
            charts = [u for u in units if u.kind == "chart"]
            if charts:
                say(f"helm lint on {len(charts)} chart(s)…")
            for u in charts:
                found, errs = lint.lint_chart(root, u.path, chart_name(root, u.path),
                                              strict=bool(cfg.lint_strict))
                res.findings += [f for f in found if f.file in in_scope
                                 and f.rule not in cfg.disabled_rules]
                res.errors += errs

    if cfg.use_static and scope:
        say(f"Static checks on {len(units)} unit(s) (rendering charts with helm template)…")
        found, errs, notes = run_static(root, units, scope, cfg.disabled_rules)
        # A chart that does not render is already a helm lint error on the same file.
        lint_errors = {f.file for f in res.findings if f.source == "helm-lint"
                       and f.severity >= Severity.HIGH}
        res.findings += [f for f in found if not (f.rule == "HR000" and f.file in lint_errors)]
        res.errors += [e for e in errs if e not in res.errors]
        res.stats["notes"] = notes
        for n in notes:
            say(f"note: {n}")

    summaries: dict[str, str] = {}
    if cfg.use_llm and units:
        client = make_client(cfg)
        try:
            client.version()
            reachable = True
        except Exception as exc:
            reachable = False
            msg = f"Ollama not reachable at {cfg.ollama_host} ({exc.__class__.__name__}); LLM review skipped"
            res.errors.append(msg)
            say("warning: " + msg)
            if a.require_llm:
                print("error: --require-llm set and Ollama is unreachable", file=sys.stderr)
                return EXIT_ERROR
        if reachable:
            res.model = cfg.model
            reviewer = Reviewer(root, cfg, client, say, use_cache=not getattr(a, 'no_cache', False))
            res.findings += reviewer.review(units, list(res.findings))
            res.errors += reviewer.errors
            summaries = reviewer.summaries
            res.stats["tokens_in"] = client.tokens_in
            res.stats["tokens_out"] = client.tokens_out

    # In diff mode only report on changed files/lines (the LLM still saw whole units).
    if changed is not None:
        res.findings = [f for f in res.findings if f.file in changed]
        if a.diff_scope == "lines":
            res.findings = [f for f in res.findings if touches_change(f, changed_lines)]
    floor = cfg.min_severity_level
    res.findings = [f for f in res.findings if f.severity >= floor
                    and f.rule not in cfg.disabled_rules]
    res.stats["seconds"] = round(time.time() - t0, 1)

    formats = [x for x in (a.format or "md,json").split(",") if x]
    out_dir = Path(a.output) if a.output else root / ".helmreview"
    written = write_reports(res, out_dir, formats, cfg.language, summaries)
    if not a.quiet:
        print(terminal_summary(res), file=sys.stderr)
        for p in written:
            print(f"Report: {p}", file=sys.stderr)
        print(f"Done in {res.stats['seconds']}s", file=sys.stderr)

    worst = res.worst()
    if worst is not None and worst >= cfg.fail_on_severity:
        return EXIT_FINDINGS
    return EXIT_OK


def cmd_doctor(a: argparse.Namespace) -> int:
    root = Path(a.path).resolve()
    cfg, warnings = load_config(root, Path(a.config) if a.config else None)
    _apply_cli(cfg, a)
    ok = True
    for w in warnings:
        print(f"! {w}")
    v = lint.version()
    print(f"{'✓' if v else '✗'} helm: {v or 'not installed (https://helm.sh/docs/intro/install/)'}")
    ok = ok and bool(v)
    client = OllamaClient(cfg.ollama_host, cfg.model, api_key=cfg.api_key)
    try:
        print(f"✓ Ollama {client.version()} at {cfg.ollama_host}")
        if client.has_model():
            print(f"✓ model {cfg.model} is available")
            if not getattr(a, "no_speed", False):
                print("… measuring speed (two short requests)", flush=True)
                try:
                    lines, fast = speed_check(cfg)
                    print("\n".join(lines))
                except Exception as exc:
                    fast = False
                    print(f"✗ speed check failed: {exc}")
                ok = ok and fast
        else:
            ok = False
            names = ", ".join(client.models()[:10]) or "none"
            print(f"✗ model {cfg.model} not pulled (have: {names}) — run: ollama pull {cfg.model}")
    except Exception as exc:
        ok = False
        print(f"✗ Ollama not reachable at {cfg.ollama_host}: {exc}")
    units = discover(root, cfg.exclude)
    print(f"✓ {root}: {len(units)} reviewable unit(s)")
    return EXIT_OK if ok else EXIT_ERROR


def cmd_rules(_: argparse.Namespace) -> int:
    for rule, doc in RULE_DOCS.items():
        print(f"{rule}  {doc}")
    print("\nPlus helm lint (helm-lint:*), and llm:* findings from the model.")
    return EXIT_OK


def cmd_init(a: argparse.Namespace) -> int:
    target = Path(a.path).resolve() / ".helmreview.yml"
    if target.exists() and not a.force:
        print(f"{target} already exists (use --force to overwrite)", file=sys.stderr)
        return EXIT_ERROR
    target.write_text(SAMPLE_CONFIG)
    print(f"Wrote {target}")
    return EXIT_OK


def _load_report(path: Path) -> tuple[list[Finding], str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for d in data.get("findings", []):
        try:
            out.append(Finding(
                rule=d["rule"], severity=Severity.parse(d.get("severity")), file=d["file"],
                line=d.get("line"), message=d.get("message", ""), source=d.get("source", ""),
                category=d.get("category", "general"), suggestion=d.get("suggestion", ""),
                confidence=d.get("confidence"), end_line=d.get("end_line")))
        except KeyError:
            continue
    return out, str(data.get("root", ""))


FIXABLE_SUFFIXES = (".yml", ".yaml", ".tpl", ".txt", ".json")


def _git_dirty(root: Path) -> list[str] | None:
    """Uncommitted paths helmreview could touch, or None when root is not a git repo.

    Modified tracked files always count. Untracked files only count when fix could edit
    them (YAML, templates, ...): an untracked docs/ or build output does not block a run,
    because `git checkout -- .` would not undo a change to it anyway."""
    try:
        proc = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "-uall", "--",
                               ".", ":(exclude).helmreview"],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        status, path = line[:2], line[3:].strip('"')
        if status == "??" and not path.endswith(FIXABLE_SUFFIXES):
            continue
        out.append(path)
    return out


def cmd_fix(a: argparse.Namespace) -> int:
    root = Path(a.path).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return EXIT_ERROR
    cfg, warnings = load_config(root, Path(a.config) if a.config else None)
    _apply_cli(cfg, a)
    for attr in ("fix_min_severity", "fix_min_confidence", "fix_batch_size"):
        if getattr(a, attr, None) is not None:
            setattr(cfg, attr, getattr(a, attr))
    say = _progress(a.quiet)
    for w in warnings:
        say(f"warning: {w}")

    dirty = _git_dirty(root)
    if dirty is None and not a.dry_run:
        say("warning: not a git repository; a backup of every changed file goes to .helmreview/backup/")
    elif dirty and not (a.dry_run or a.allow_dirty):
        print("error: the repository has uncommitted changes:\n  " + "\n  ".join(dirty[:10])
              + ("\n  …" if len(dirty) > 10 else "")
              + "\nCommit or stash them first so `git diff` shows only helmreview's fixes, "
                "or pass --allow-dirty (or --dry-run).", file=sys.stderr)
        return EXIT_ERROR

    out_dir = Path(a.output) if a.output else root / ".helmreview"
    findings: list[Finding] = []
    if not a.no_llm:
        report = Path(a.from_report) if a.from_report else out_dir / "report.json"
        if not report.is_file():
            say(f"No report at {report}; running a scan first…")
            argv = ["scan", str(root), "-q", "-f", "json", "-o", str(report.parent)]
            for flag, val in (("--host", a.host), ("--model", a.model), ("-c", a.config),
                              ("--lang", a.lang)):
                if val:
                    argv += [flag, val]
            for p in a.only or []:
                argv += ["--only", p]
            cmd_scan(build_parser().parse_args(argv))
        if report.is_file():
            findings, report_root = _load_report(report)
            if report_root and Path(report_root).name != root.name:
                say(f"warning: {report} was made for '{Path(report_root).name}', not '{root.name}'")
            say(f"Using {report} ({sum(1 for f in findings if f.source == 'llm')} model findings)")

    client = None
    if not a.no_llm:
        client = make_client(cfg, temperature=0.0)
        try:
            client.version()
        except Exception as exc:
            say(f"warning: Ollama not reachable at {cfg.ollama_host} ({exc.__class__.__name__}); "
                "model fixes skipped")
            client = None

    fixer = Fixer(root, cfg, client=client, static_fix=not a.no_static_fix,
                  only=a.only, say=say, use_cache=not a.no_cache)
    run = fixer.run(findings)

    out_dir.mkdir(parents=True, exist_ok=True)
    patch = out_dir / "fix.patch"
    patch.write_text(run.diff(), encoding="utf-8")
    written = []
    if not a.dry_run:
        backup = out_dir / "backup" / time.strftime("%Y%m%d-%H%M%S") if dirty is None else None
        for rel, (old, new) in sorted(run.changed.items()):
            target = root / rel
            try:
                current = target.read_text(encoding="utf-8", errors="surrogateescape")
            except OSError as exc:
                run.errors.append(f"{rel}: {exc}")
                continue
            if current != old:
                run.errors.append(f"{rel}: changed on disk during the fix run; left alone")
                continue
            if backup:
                (backup / rel).parent.mkdir(parents=True, exist_ok=True)
                (backup / rel).write_text(old, encoding="utf-8", errors="surrogateescape")
            target.write_text(new, encoding="utf-8", errors="surrogateescape")
            written.append(rel)
    report_md = out_dir / "fix-report.md"
    report_md.write_text(fix_report_md(run, str(root), a.dry_run, cfg.language), encoding="utf-8")

    if not a.quiet:
        print("", file=sys.stderr)
        for stage in ("llm", "static"):
            print(f"  {stage:<6} fixed {run.count('fixed', stage):>3}   declined "
                  f"{run.count('declined', stage):>3}   failed {run.count('failed', stage):>3}   "
                  f"manual {run.count('manual', stage):>3}", file=sys.stderr)
        for e in run.errors[:10]:
            print(f"  ! {e}", file=sys.stderr)
        verb = "would change" if a.dry_run else "changed"
        print(f"\n{len(run.changed)} file(s) {verb}. Patch: {patch}\nReport: {report_md}",
              file=sys.stderr)
        if written and dirty is not None:
            print("Review with `git diff`; undo everything with `git checkout -- .`",
                  file=sys.stderr)
    return EXIT_OK


def _apply_gitlab_cli(cfg: Config, a: argparse.Namespace):
    if getattr(a, "gitlab_url", None):
        cfg.gitlab_url = a.gitlab_url
    if getattr(a, "project", None):
        cfg.gitlab_projects = list(a.project)
    if getattr(a, "group", None):
        cfg.gitlab_groups = list(a.group)
    if getattr(a, "interval", None):
        cfg.gitlab_interval = a.interval
    if getattr(a, "skip_drafts", False):
        cfg.gitlab_skip_drafts = True
    if getattr(a, "skip_forks", False):
        cfg.gitlab_skip_forks = True


def cmd_watch_mrs(a: argparse.Namespace) -> int:
    """Poll GitLab for open merge requests and review each new head commit."""
    from .gitlab import watcher_from_config

    # Settings come from ~/.helmreview.yml, the environment and flags: never from a repo.
    cfg, warnings = load_config(Path(os.devnull), Path(a.config) if a.config else None)
    _apply_cli(cfg, a)
    _apply_gitlab_cli(cfg, a)
    say = _progress(False)
    for w in warnings:
        say(f"warning: {w}")
    scan_args = []
    for flag, val in (("--model", a.model), ("--host", a.host), ("--config", a.config),
                      ("--lang", a.lang)):
        if val:
            scan_args.append(f"{flag}={val}")
    if a.no_llm:
        scan_args.append("--no-llm")
    try:
        watcher = watcher_from_config(
            cfg, state_dir=Path(a.state_dir) if a.state_dir else None,
            dry_run=a.dry_run, say=say, scan_args=lambda: list(scan_args))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    targets = ", ".join(watcher.projects + [g + "/*" for g in watcher.groups])
    say(f"helmreview {__version__}: watching merge requests of {targets} on {watcher.gitlab.url}")
    if a.once or a.dry_run:
        stats = watcher.poll_once()
        say(f"{stats['open']} open, {stats['reviewed']} reviewed, {stats['failed']} failed, "
            f"{stats['skipped']} already done or skipped")
        return EXIT_ERROR if stats["failed"] else EXIT_OK
    say(f"polling every {watcher.interval}s (Ctrl-C stops)")
    watcher.run_forever()
    return EXIT_OK


def cmd_serve(a: argparse.Namespace) -> int:
    from .web import serve
    roots = [Path(r) for r in (a.root or ["."])]
    watcher = None
    if a.watch_mrs or os.environ.get("HELMREVIEW_WATCH_MRS", "").lower() in ("1", "true", "yes", "on"):
        from .gitlab import watcher_from_config
        cfg, _ = load_config(Path(os.devnull))
        try:
            watcher = watcher_from_config(cfg)
        except ValueError as exc:
            print(f"error: --watch-mrs: {exc}", file=sys.stderr)
            return EXIT_ERROR
    return serve(roots, bind=a.bind, port=a.port, token=a.token, no_auth=a.no_auth,
                 open_browser=a.open, verbose=a.verbose,
                 upload_dir=Path(a.upload_dir) if a.upload_dir else None,
                 no_upload=a.no_upload, max_upload_mb=a.max_upload_mb,
                 upload_keep_days=a.upload_keep_days, mr_watcher=watcher)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="helmreview",
        description="Review Helm charts and Kubernetes manifests with helm lint, static "
                    "security checks on the rendered output, and a local Ollama model.")
    p.add_argument("--version", action="version", version=f"helmreview {__version__}")
    sub = p.add_subparsers(dest="command")

    def common(sp):
        sp.add_argument("path", nargs="?", default=".", help="repository root (default: .)")
        sp.add_argument("-c", "--config", help="extra config file")
        sp.add_argument("-m", "--model", help="Ollama model (default qwen2.5-coder:14b)")
        sp.add_argument("--host", help="Ollama URL, e.g. http://gpu-box:11434")

    s = sub.add_parser("scan", help="review the codebase (default command)")
    common(s)
    s.add_argument("--diff", metavar="BASE", help="only review units changed vs a git ref, e.g. origin/main")
    s.add_argument("--diff-scope", choices=["lines", "files"], default="lines",
                   help="with --diff: report only on changed lines (default) or whole changed files")
    s.add_argument("--only", action="append", metavar="PATTERN",
                   help="limit to units/paths matching (repeatable), e.g. charts/web")
    s.add_argument("-f", "--format", help="md,json,sarif,gitlab (default md,json)")
    s.add_argument("-o", "--output", help="report directory (default <repo>/.helmreview)")
    s.add_argument("--lang", choices=["en", "nl"], help="report language")
    s.add_argument("--fail-on", choices=[x.label for x in Severity])
    s.add_argument("--min-severity", choices=[x.label for x in Severity])
    s.add_argument("--min-confidence", type=float)
    s.add_argument("--workers", type=int)
    s.add_argument("--num-ctx", type=int)
    s.add_argument("--timeout", type=int, help="max seconds without model output (default 600)")
    s.add_argument("--no-cache", action="store_true",
                   help="ignore .helmreview/scan-cache.json and ask the model again")
    s.add_argument("--strict", action="store_true", help="helm lint --strict")
    s.add_argument("--exclude", action="append", metavar="GLOB")
    s.add_argument("--disable", action="append", metavar="RULE")
    s.add_argument("--no-llm", action="store_true", help="skip the Ollama review")
    s.add_argument("--no-lint", action="store_true", help="skip helm lint")
    s.add_argument("--no-static", action="store_true", help="skip the built-in HR checks")
    s.add_argument("--require-llm", action="store_true", help="fail if Ollama is unreachable")
    s.add_argument("-q", "--quiet", action="store_true")
    s.set_defaults(func=cmd_scan)

    fx = sub.add_parser("fix", help="automatically fix findings with the model (verified by helm template)")
    common(fx)
    fx.add_argument("--from-report", metavar="FILE",
                    help="report.json to fix from (default <repo>/.helmreview/report.json; "
                         "a scan runs first when it does not exist)")
    fx.add_argument("--dry-run", action="store_true", help="only write fix.patch, change nothing")
    fx.add_argument("--allow-dirty", action="store_true",
                    help="run even with uncommitted changes in the repo")
    fx.add_argument("--only", action="append", metavar="PATH", help="limit to paths (repeatable)")
    fx.add_argument("--min-severity", dest="fix_min_severity",
                    choices=[x.label for x in Severity],
                    help="model fixes only for findings at/above this (default medium)")
    fx.add_argument("--min-confidence", dest="fix_min_confidence", type=float,
                    help="model fixes only at/above this confidence (default 0.8)")
    fx.add_argument("--no-llm", action="store_true", help="skip model fixes")
    fx.add_argument("--no-static-fix", action="store_true",
                    help="do not list the HR findings that need a human")
    fx.add_argument("--lang", choices=["en", "nl"])
    fx.add_argument("--workers", type=int,
                    help="parallel model requests; match OLLAMA_NUM_PARALLEL on the server")
    fx.add_argument("--timeout", type=int, help="max seconds without model output (default 600)")
    fx.add_argument("--batch-size", dest="fix_batch_size", type=int,
                    help="findings per model request (default 4; split further on timeout)")
    fx.add_argument("--no-cache", action="store_true",
                    help="ignore .helmreview/fix-cache.json and ask the model again")
    fx.add_argument("-o", "--output", help="where reports live (default <repo>/.helmreview)")
    fx.add_argument("-q", "--quiet", action="store_true")
    fx.set_defaults(func=cmd_fix)

    d = sub.add_parser("doctor", help="check helm, Ollama and the model")
    common(d)
    d.add_argument("--no-speed", action="store_true", help="skip the speed check")
    d.set_defaults(func=cmd_doctor)

    r = sub.add_parser("rules", help="list built-in rules")
    r.set_defaults(func=cmd_rules)

    i = sub.add_parser("init", help="write a sample .helmreview.yml")
    i.add_argument("path", nargs="?", default=".")
    i.add_argument("--force", action="store_true")
    i.set_defaults(func=cmd_init)

    w = sub.add_parser("serve", help="start the web UI")
    w.add_argument("--root", action="append", metavar="DIR",
                   help="a repo, or a folder of repos, the UI may scan (repeatable; default .)")
    w.add_argument("--bind", default="127.0.0.1",
                   help="address to listen on (default 127.0.0.1; 0.0.0.0 for all)")
    w.add_argument("--port", type=int, default=8766, help="port (default 8766)")
    w.add_argument("--token", help="access token (default: $HELMREVIEW_WEB_TOKEN or a random one)")
    w.add_argument("--no-auth", action="store_true",
                   help="no access token (only allowed with a localhost --bind)")
    w.add_argument("--open", action="store_true", help="open the UI in a browser")
    w.add_argument("-v", "--verbose", action="store_true", help="log every request")
    w.add_argument("--upload-dir", metavar="DIR",
                   help="where uploaded ZIPs are unpacked (default ~/.cache/helmreview/uploads)")
    w.add_argument("--max-upload-mb", type=int, default=100, metavar="MB",
                   help="largest ZIP accepted (default 100)")
    w.add_argument("--upload-keep-days", type=float, default=7, metavar="DAYS",
                   help="delete uploads older than this (default 7, 0 = keep)")
    w.add_argument("--no-upload", action="store_true", help="switch ZIP uploads off")
    w.add_argument("--watch-mrs", action="store_true",
                   help="also review GitLab merge requests in the background (settings: "
                        "gitlab_* in ~/.helmreview.yml or $HELMREVIEW_GITLAB_*; same as "
                        "$HELMREVIEW_WATCH_MRS=1)")
    w.set_defaults(func=cmd_serve)

    g = sub.add_parser("watch-mrs",
                       help="review GitLab merge requests as they come in and comment on them")
    g.add_argument("-c", "--config", help="extra config file")
    g.add_argument("-m", "--model", help="Ollama model")
    g.add_argument("--host", help="Ollama URL, e.g. http://gpu-box:11434")
    g.add_argument("--gitlab-url", metavar="URL", help="e.g. https://gitlab.example.nl")
    g.add_argument("--project", action="append", metavar="PATH",
                   help="project to watch, group/name or id (repeatable)")
    g.add_argument("--group", action="append", metavar="PATH",
                   help="watch every project in this group and its subgroups (repeatable)")
    g.add_argument("--interval", type=int, metavar="SECONDS",
                   help="seconds between polls (default 120)")
    g.add_argument("--once", action="store_true", help="poll once and exit (cron, CI)")
    g.add_argument("--dry-run", action="store_true",
                   help="only list the merge requests that would be reviewed")
    g.add_argument("--skip-drafts", action="store_true", help="leave draft merge requests alone")
    g.add_argument("--skip-forks", action="store_true",
                   help="leave merge requests from forks alone")
    g.add_argument("--state-dir", metavar="DIR",
                   help="checkouts and what was reviewed (default ~/.cache/helmreview/mr)")
    g.add_argument("--lang", choices=["en", "nl"], help="language of the comment")
    g.add_argument("--no-llm", action="store_true", help="skip the Ollama review")
    g.set_defaults(func=cmd_watch_mrs)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0] not in {"scan", "fix", "doctor", "rules", "init", "serve",
                                    "watch-mrs", "-h", "--help", "--version"}):
        argv.insert(0, "scan")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
