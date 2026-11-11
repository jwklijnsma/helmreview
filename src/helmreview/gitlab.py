"""GitLab merge requests: `helmreview watch-mrs` and `helmreview serve --watch-mrs`.

The watcher polls GitLab for open merge requests (no webhook, so nothing has to reach this
machine), checks each new head commit out into its own work folder, runs a normal
`helmreview scan --diff <target branch>` on it and keeps ONE comment per merge request up to
date with the result.

Design notes
------------
* Polling only needs outgoing HTTPS to GitLab, so it works from a GPU box behind a firewall.
* The token is read from the environment and is never written to disk: git gets it through
  GIT_CONFIG_* variables (not argv, not .git/config), scoped to the GitLab URL.
* A merge request is somebody else's code. It is checked out with core.symlinks=false, so a
  planted link to a file on this host arrives as a small text file, not as that file.
* Everything the comment shows that came from the scan (and so maybe from a model that read
  the merge request) is flattened to one line and never starts a line: GitLab runs lines that
  begin with `/` as quick actions (/merge, /close) with the token's rights.
* What was reviewed is remembered in a state file and in the comment itself (the commit sha),
  so a lost state file does not make every merge request get a second review.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Callable, Iterator
from urllib.parse import quote

import requests

from . import __version__

TOOL = "helmreview"
MARKER = f"<!-- {TOOL}:mr-review"
MAX_FAILURES = 3              # give up on a commit after this many failed scans
MAX_LISTED = 40               # findings shown in the comment; the rest is counted
SEVERITIES = ("critical", "high", "medium", "low", "info")
ZWSP = "\u200b"            # zero-width space: breaks @mentions and the like, shows nothing
ICON = {"critical": "🟥", "high": "🟧", "medium": "🟨", "low": "🟦", "info": "⬜"}

TXT = {
    "en": {"title": "{tool} review", "commit": "commit", "none": "No findings on the changed lines. 🎉",
           "found": "{n} finding(s) on the changed lines", "blocking": "at or above `{level}`: {n}",
           "nothing": "This version changes nothing {tool} reviews.",
           "more": "… and {n} more (lowest severity first to go).", "fix": "Fix",
           "problems": "Problems during the scan", "model": "model", "no_model": "no model review",
           "footer": "Updated by {tool} {version} on every push to this merge request."},
    "nl": {"title": "{tool}-review", "commit": "commit", "none": "Geen bevindingen op de gewijzigde regels. 🎉",
           "found": "{n} bevinding(en) op de gewijzigde regels", "blocking": "op of boven `{level}`: {n}",
           "nothing": "Deze versie wijzigt niets dat {tool} beoordeelt.",
           "more": "… en nog {n} (de minst ernstige zijn weggelaten).", "fix": "Oplossing",
           "problems": "Problemen tijdens de scan", "model": "model", "no_model": "geen modelreview",
           "footer": "Bijgewerkt door {tool} {version} bij elke push naar deze merge request."},
}


class GitLabError(RuntimeError):
    pass


# --------------------------------------------------------------------------- API

class GitLab:
    """The few GitLab REST calls the watcher needs."""

    def __init__(self, url: str, token: str, timeout: int = 30,
                 session: requests.Session | None = None):
        self.url = url.rstrip("/")
        self.api = self.url + "/api/v4"
        self.timeout = timeout
        self.s = session or requests.Session()
        self.s.headers["PRIVATE-TOKEN"] = token

    def _req(self, method: str, path: str, **kw) -> requests.Response:
        try:
            r = self.s.request(method, self.api + path, timeout=self.timeout, **kw)
        except requests.RequestException as exc:
            raise GitLabError(f"{method} {path}: {exc.__class__.__name__}") from exc
        if r.status_code >= 400:
            raise GitLabError(f"{method} {path}: HTTP {r.status_code} {r.text[:200]}")
        return r

    def _pages(self, path: str, params: dict, max_pages: int = 20) -> Iterator[dict]:
        page = "1"
        for _ in range(max_pages):
            r = self._req("GET", path, params={**params, "per_page": 100, "page": page})
            data = r.json()
            if not isinstance(data, list):
                raise GitLabError(f"GET {path}: expected a list")
            yield from data
            page = r.headers.get("X-Next-Page") or ""
            if not page:
                return

    @staticmethod
    def _id(ref) -> str:
        return quote(str(ref), safe="")

    def project(self, ref) -> dict:
        return self._req("GET", f"/projects/{self._id(ref)}").json()

    def open_mrs(self, kind: str, ref) -> list[dict]:
        """Open merge requests of a project (kind="project") or a group and its subgroups."""
        return list(self._pages(f"/{kind}s/{self._id(ref)}/merge_requests",
                                {"state": "opened", "order_by": "updated_at", "sort": "desc"}))

    def notes(self, project_id, iid: int) -> Iterator[dict]:
        return self._pages(f"/projects/{self._id(project_id)}/merge_requests/{int(iid)}/notes",
                           {"order_by": "updated_at", "sort": "desc"}, max_pages=5)

    def create_note(self, project_id, iid: int, body: str) -> dict:
        return self._req("POST", f"/projects/{self._id(project_id)}/merge_requests/{int(iid)}/notes",
                         json={"body": body}).json()

    def update_note(self, project_id, iid: int, note_id: int, body: str) -> dict:
        return self._req("PUT", f"/projects/{self._id(project_id)}/merge_requests/{int(iid)}"
                                f"/notes/{int(note_id)}", json={"body": body}).json()


# --------------------------------------------------------------------------- comment

def _flat(text, limit: int = 400) -> str:
    """Scan output as one harmless line: no line breaks, @mentions, HTML comments or images."""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    s = s.replace("@", "@" + ZWSP).replace("<!--", "<" + ZWSP + "!--").replace("![", "!" + ZWSP + "[")
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


def _code(text) -> str:
    return "`" + _flat(text, 200).replace("`", "'") + "`"


def note_sha(body: str) -> str | None:
    m = re.search(re.escape(MARKER) + r" sha=([0-9a-f]{7,64}) -->", body or "")
    return m.group(1) if m else None


def build_note(report: dict, *, sha: str, web_url: str = "", lang: str = "en",
               fail_on: str = "high", seconds: float | None = None) -> str:
    """The merge-request comment for one scan (report = the parsed report.json)."""
    t = TXT.get(lang, TXT["en"])
    findings = report.get("findings") or []
    rank = {s: i for i, s in enumerate(SEVERITIES)}
    findings = sorted(findings, key=lambda f: (rank.get(f.get("severity"), 9),
                                               str(f.get("file")), f.get("line") or 0))
    out = [f"{MARKER} sha={sha} -->",
           f"### {t['title'].format(tool=TOOL)} · {t['commit']} `{sha[:8]}`", ""]
    if not report.get("units"):
        out.append(t["nothing"].format(tool=TOOL))
    elif not findings:
        out.append(t["none"])
    else:
        counts = {s: sum(1 for f in findings if f.get("severity") == s) for s in SEVERITIES}
        blocking = sum(n for s, n in counts.items() if rank[s] <= rank.get(fail_on, 1))
        head = "**" + t["found"].format(n=len(findings)) + "**"
        if blocking:
            head += " · " + t["blocking"].format(level=fail_on, n=blocking)
        out += [head, "",
                " · ".join(f"{ICON[s]} {s} {n}" for s, n in counts.items() if n), ""]
        by_file: dict[str, list[dict]] = {}
        for f in findings[:MAX_LISTED]:
            by_file.setdefault(str(f.get("file")), []).append(f)
        for file, items in by_file.items():
            out.append(f"**{_code(file)}**")
            for f in sorted(items, key=lambda x: x.get("line") or 0):
                sev = f.get("severity") if f.get("severity") in ICON else "info"
                line = f.get("line")
                loc = f"L{int(line)}" if isinstance(line, int) and line > 0 else "—"
                if web_url and loc != "—":
                    # Path from the scan, percent-encoded: it cannot break out of the link.
                    loc = f"[{loc}]({web_url}/-/blob/{sha}/{quote(file, safe='/')}#{loc})"
                row = f"- {ICON[sev]} **{sev}** · {loc} · {_code(f.get('rule'))} · {_flat(f.get('message'))}"
                if f.get("suggestion"):
                    row += f" ↳ _{t['fix']}:_ {_flat(f.get('suggestion'))}"
                out.append(row)
            out.append("")
        if len(findings) > MAX_LISTED:
            out += [t["more"].format(n=len(findings) - MAX_LISTED), ""]
    errors = report.get("errors") or []
    if errors:
        out += ["", f"**{t['problems']}**"] + [f"- {_flat(e, 300)}" for e in errors[:5]]
    meta = [f"{t['model']} {_code(report['model'])}" if report.get("model") else t["no_model"]]
    if seconds is not None:
        meta.append(f"{int(seconds)}s")
    out += ["", "---", f"<sub>{' · '.join(meta)} · {t['footer'].format(tool=TOOL, version=__version__)}</sub>"]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- git

def git_env(base_url: str, token: str) -> dict:
    """Environment that lets git authenticate to `base_url` without the token touching disk."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if token:
        n = int(env.get("GIT_CONFIG_COUNT") or 0)
        basic = base64.b64encode(f"oauth2:{token}".encode()).decode()
        env[f"GIT_CONFIG_KEY_{n}"] = f"http.{base_url.rstrip('/')}/.extraHeader"
        env[f"GIT_CONFIG_VALUE_{n}"] = f"Authorization: Basic {basic}"
        env["GIT_CONFIG_COUNT"] = str(n + 1)
    return env


def _git(work: Path, *args: str, env: dict | None = None, timeout: int = 900) -> str:
    try:
        p = subprocess.run(["git", "-C", str(work), "-c", "core.symlinks=false",
                            "-c", "advice.detachedHead=false", *args],
                           capture_output=True, text=True, timeout=timeout, env=env,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitLabError(f"git {args[0]}: {exc.__class__.__name__}") from exc
    if p.returncode != 0:
        raise GitLabError(f"git {args[0]} failed: {(p.stderr or p.stdout).strip()[-300:]}")
    return p.stdout.strip()


# --------------------------------------------------------------------------- watcher

def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")[:80] or "project"


@dataclass
class MRWatcher:
    gitlab: GitLab
    token: str
    state_dir: Path
    projects: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    interval: int = 120
    skip_drafts: bool = False
    skip_forks: bool = False
    max_age_days: float = 14           # ignore merge requests not touched for this long (0 = off)
    scan_timeout: int = 3600
    lang: str = "en"
    fail_on: str = "high"
    dry_run: bool = False
    # Extra `scan` arguments (model, host, config...), asked again for every scan.
    scan_args: Callable[[], list[str]] = lambda: []
    say: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr, flush=True)
    # Held while a scan runs; `serve` passes the lock its own jobs use (one GPU).
    lock: threading.Lock = field(default_factory=threading.Lock)
    python: str = sys.executable

    def __post_init__(self):
        self.state_dir = Path(self.state_dir).expanduser()
        self.state_file = self.state_dir / "state.json"
        self.state: dict[str, dict] = {}
        self._projects: dict[str, dict] = {}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.state = {k: v for k, v in data.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            pass

    # ---- state
    def _save(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - 90 * 86400
        self.state = {k: v for k, v in self.state.items() if v.get("at", 0) >= cutoff}
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(self.state_file)

    def _remember(self, key: str, **fields):
        self.state[key] = {**fields, "at": time.time()}
        if not self.dry_run:
            self._save()

    # ---- discovery
    def _project(self, project_id) -> dict:
        key = str(project_id)
        if key not in self._projects:
            self._projects[key] = self.gitlab.project(project_id)
        return self._projects[key]

    def candidates(self) -> list[dict]:
        """Open merge requests from every watched project and group, each once."""
        seen: dict[str, dict] = {}
        for kind, refs in (("project", self.projects), ("group", self.groups)):
            for ref in refs:
                try:
                    mrs = self.gitlab.open_mrs(kind, ref)
                except GitLabError as exc:
                    self.say(f"warning: {kind} {ref}: {exc}")
                    continue
                for mr in mrs:
                    seen.setdefault(f"{mr.get('project_id')}!{mr.get('iid')}", mr)
        return list(seen.values())

    def _skip_reason(self, mr: dict) -> str | None:
        if not mr.get("sha"):
            return "no commits"
        if self.skip_drafts and (mr.get("draft") or mr.get("work_in_progress")):
            return "draft"
        if self.skip_forks and mr.get("source_project_id") != mr.get("target_project_id"):
            return "from a fork"
        if self.max_age_days and mr.get("updated_at"):
            try:
                updated = datetime.fromisoformat(str(mr["updated_at"]).replace("Z", "+00:00"))
                if updated < datetime.now(timezone.utc) - timedelta(days=self.max_age_days):
                    return "stale"
            except ValueError:
                pass
        return None

    # ---- one poll
    def poll_once(self) -> dict:
        """Review every merge request whose head commit was not reviewed yet."""
        stats = {"open": 0, "reviewed": 0, "failed": 0, "skipped": 0}
        for mr in self.candidates():
            stats["open"] += 1
            key = f"{mr['project_id']}!{mr['iid']}"
            st = self.state.get(key, {})
            if self._skip_reason(mr) or (st.get("sha") == mr["sha"] and (
                    st.get("status") == "done" or st.get("failures", 0) >= MAX_FAILURES)):
                stats["skipped"] += 1
                continue
            try:
                done = self.review(mr)
            except GitLabError as exc:
                done = False
                self._fail(key, mr["sha"], str(exc))
            stats["reviewed" if done else "failed"] += 1
        return stats

    def _fail(self, key: str, sha: str, why: str):
        st = self.state.get(key, {})
        failures = (st.get("failures", 0) if st.get("sha") == sha else 0) + 1
        self._remember(key, sha=sha, status="failed", failures=failures)
        self.say(f"warning: {key} @ {sha[:8]}: {why}"
                 + (f" (gave up after {failures} tries)" if failures >= MAX_FAILURES else ""))

    def _own_note(self, project_id, iid: int) -> dict | None:
        for note in self.gitlab.notes(project_id, iid):
            if not note.get("system") and str(note.get("body") or "").startswith(MARKER):
                return note
        return None

    def checkout(self, proj: dict, mr: dict) -> tuple[Path, str, str]:
        """Fetch the merge request into its project's work folder. Returns (folder, sha, base ref)."""
        iid = int(mr["iid"])
        work = self.state_dir / "work" / f"{_slug(proj.get('path_with_namespace', ''))}-{proj['id']}"
        if not (work / ".git").is_dir():
            work.mkdir(parents=True, exist_ok=True)
            _git(work, "init", "-q")
        head, base = f"refs/{TOOL}/mr/{iid}", f"refs/{TOOL}/target/{iid}"
        _git(work, "fetch", "--quiet", "--force", "--no-tags", proj["http_url_to_repo"],
             f"+refs/merge-requests/{iid}/head:{head}",
             f"+refs/heads/{mr['target_branch']}:{base}",
             env=git_env(self.gitlab.url, self.token))
        _git(work, "checkout", "--quiet", "--force", "--detach", head)
        _git(work, "clean", "-fdxq", "-e", f".{TOOL}")
        return work, _git(work, "rev-parse", "HEAD"), base

    def _scan(self, work: Path, base: str) -> tuple[int, str]:
        argv = [self.python, "-m", TOOL, "scan", str(work), f"--diff={base}",
                "--format=md,json", f"--output={work / ('.' + TOOL)}", *self.scan_args()]
        # Not from inside the checkout: `python -m` would put that folder on sys.path.
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONSAFEPATH="1")
        with self.lock:
            try:
                p = subprocess.run(argv, cwd=str(self.state_dir), env=env, stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=self.scan_timeout)
            except subprocess.TimeoutExpired:
                return 2, f"scan took longer than {self.scan_timeout}s"
            except OSError as exc:
                return 2, f"could not start the scan: {exc}"
        return p.returncode, (p.stderr or p.stdout or "").strip()[-400:]

    def review(self, mr: dict) -> bool:
        """Scan one merge request and write the comment. False when the scan failed."""
        key = f"{mr['project_id']}!{mr['iid']}"
        proj = self._project(mr["project_id"])
        name = f"{proj.get('path_with_namespace', mr['project_id'])}!{mr['iid']}"
        if self.dry_run:
            self.say(f"would review {name} @ {mr['sha'][:8]}: {_flat(mr.get('title'), 80)}")
            return True
        note = self._own_note(mr["project_id"], mr["iid"])
        if note and note_sha(note.get("body", "")) == mr["sha"]:
            self._remember(key, sha=mr["sha"], status="done", note_id=note.get("id"))
            return True                    # reviewed before; only the state file forgot
        self.say(f"reviewing {name} @ {mr['sha'][:8]}: {_flat(mr.get('title'), 80)}")
        work, sha, base = self.checkout(proj, mr)
        report_file = work / f".{TOOL}" / "report.json"
        report_file.unlink(missing_ok=True)
        t0 = time.time()
        rc, tail = self._scan(work, base)
        try:
            report = json.loads(report_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = None
        if rc not in (0, 1) or not isinstance(report, dict):
            self._fail(key, mr["sha"], f"scan failed (exit {rc}): {tail}")
            return False
        if not report.get("units") and not note:
            self._remember(key, sha=sha, status="done")    # nothing for us in this one
            self.say(f"  {name}: nothing to review, no comment")
            return True
        body = build_note(report, sha=sha, web_url=str(proj.get("web_url") or ""),
                          lang=self.lang, fail_on=self.fail_on, seconds=time.time() - t0)
        if note:
            new = self.gitlab.update_note(mr["project_id"], mr["iid"], note["id"], body)
        else:
            new = self.gitlab.create_note(mr["project_id"], mr["iid"], body)
        self._remember(key, sha=sha, status="done", note_id=new.get("id"))
        self.say(f"  {name}: {len(report.get('findings') or [])} finding(s), comment "
                 f"{'updated' if note else 'posted'}")
        return True

    # ---- loop
    def run_forever(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                stats = self.poll_once()
                if stats["reviewed"] or stats["failed"]:
                    self.say(f"merge requests: {stats['open']} open, {stats['reviewed']} reviewed, "
                             f"{stats['failed']} failed")
            except Exception as exc:  # noqa: BLE001 - a bad poll must not end the watcher
                self.say(f"warning: poll failed: {exc.__class__.__name__}: {exc}")
            stop.wait(max(10, int(self.interval)))


def watcher_from_config(cfg, *, state_dir: Path | None = None, **overrides) -> MRWatcher:
    """Build the watcher from a Config. Raises ValueError with what is missing."""
    url = str(cfg.gitlab_url or "").strip().rstrip("/")
    if not url:
        raise ValueError("no GitLab URL: set gitlab_url in ~/.helmreview.yml, "
                         "$HELMREVIEW_GITLAB_URL or --gitlab-url")
    if "://" not in url:
        url = "https://" + url
    token_env = str(cfg.gitlab_token_env or "GITLAB_TOKEN")
    token = os.environ.get("HELMREVIEW_GITLAB_TOKEN") or os.environ.get(token_env) or ""
    if not token.strip():
        raise ValueError(f"no GitLab token: set ${token_env} (scope: api)")
    projects = [str(p).strip() for p in cfg.gitlab_projects or [] if str(p).strip()]
    groups = [str(g).strip() for g in cfg.gitlab_groups or [] if str(g).strip()]
    if not projects and not groups:
        raise ValueError("nothing to watch: set gitlab_projects or gitlab_groups "
                         "(or --project / --group)")
    if state_dir is None:
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
        state_dir = Path(base) / TOOL / "mr"
    kw = dict(gitlab=GitLab(url, token.strip()), token=token.strip(), state_dir=state_dir,
              projects=projects, groups=groups, interval=int(cfg.gitlab_interval),
              skip_drafts=bool(cfg.gitlab_skip_drafts), skip_forks=bool(cfg.gitlab_skip_forks),
              max_age_days=float(cfg.gitlab_max_age_days), lang=str(cfg.language),
              fail_on=str(cfg.fail_on))
    kw.update(overrides)
    return MRWatcher(**kw)
