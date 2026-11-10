"""`helmreview serve`: a small self-hosted web UI on top of the CLI.

Design notes
------------
* Standard library only (http.server), no CDN assets: it runs on an air-gapped box.
* Scans and fixes run as `python -m helmreview scan|fix` subprocesses, so the web UI gets
  exactly the CLI behaviour (cache, queue.json, reports, the dirty-tree guard) for free and
  a crash in a job cannot take the server down.
* Locked down by default: binds to 127.0.0.1, every API call needs the access token,
  POSTs need a custom header (no cross-site form posts), only repositories under the
  configured --root folders are reachable, and the Ollama host can never be set from the
  browser — it comes from ~/.helmreview.yml, the environment or the server's own flags.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import hmac
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import itertools
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .. import __version__
from ..config import load_config
from ..discover import is_project_root, looks_like_project
from ..models import Severity
from .uploads import UploadError, UploadStore

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/app.css": ("app.css", "text/css; charset=utf-8"),
                "/favicon.svg": ("favicon.svg", "image/svg+xml")}

COOKIE = "helmreview_token"
CSRF_HEADER = "X-Helmreview"
MAX_BODY = 64 * 1024
MAX_FILE = 2 * 1024 * 1024
MAX_LOG_LINES = 20000
KEEP_JOBS = 50
MAX_PENDING = 20
# Result files a job can offer for download: name -> (label, produced by)
ARTIFACTS = {
    "report.md": ("Report (Markdown)", "scan"),
    "report.json": ("Report (JSON)", "scan"),
    "report.sarif": ("Report (SARIF)", "scan"),
    "fix.patch": ("Patch", "fix"),
    "fix-report.md": ("Fix report", "fix"),
}
_REPO_CACHE: dict[str, tuple[int, bool]] = {}
SEVERITIES = tuple(s.label for s in Severity)

GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/~^@{}-]{0,199}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")

SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; script-src 'self'; style-src 'self'; "
                                "img-src 'self' data:; connect-src 'self'; "
                                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------- options

def _flag(cond) -> bool:
    return cond is True or cond in ("1", "true", "on", "yes")


def _only_list(value) -> list[str]:
    if not value:
        return []
    items = value if isinstance(value, list) else str(value).replace(",", "\n").splitlines()
    out = []
    for raw in items:
        p = str(raw).strip()
        if not p:
            continue
        if (len(p) > 256 or p.startswith(("-", "/")) or "\x00" in p
                or ".." in Path(p).parts or not p.isprintable()):
            raise ApiError(400, f"invalid path filter: {p[:60]!r}")
        out.append(p)
    if len(out) > 50:
        raise ApiError(400, "too many path filters (max 50)")
    return out


def build_argv(action: str, repo: Path, opts: dict) -> list[str]:
    """Translate whitelisted UI options into CLI arguments.

    Every value goes in as `--flag=value`, so nothing the browser sends can be read as
    another option, and options the UI does not offer (host, output dir, config file)
    cannot be set at all."""
    if action not in ("scan", "fix"):
        raise ApiError(400, "action must be scan or fix")
    argv = [action, str(repo)]
    model = (opts.get("model") or "").strip()
    if model:
        if not MODEL_RE.match(model):
            raise ApiError(400, "invalid model name")
        argv.append(f"--model={model}")
    for p in _only_list(opts.get("only")):
        argv.append(f"--only={p}")
    if _flag(opts.get("no_cache")):
        argv.append("--no-cache")

    if action == "scan":
        argv.append("--format=md,json,sarif")
        diff = (opts.get("diff") or "").strip()
        if diff:
            if not GIT_REF_RE.match(diff):
                raise ApiError(400, "invalid git ref")
            argv.append(f"--diff={diff}")
            scope = opts.get("diff_scope") or "lines"
            if scope not in ("lines", "files"):
                raise ApiError(400, "diff_scope must be lines or files")
            argv.append(f"--diff-scope={scope}")
        for key, flag in (("no_llm", "--no-llm"), ("no_lint", "--no-lint"),
                          ("no_static", "--no-static"), ("strict", "--strict")):
            if _flag(opts.get(key)):
                argv.append(flag)
        sev = (opts.get("min_severity") or "").strip()
        if sev:
            if sev not in SEVERITIES:
                raise ApiError(400, "unknown severity")
            argv.append(f"--min-severity={sev}")
        lang = (opts.get("lang") or "").strip()
        if lang:
            if lang not in ("en", "nl"):
                raise ApiError(400, "lang must be en or nl")
            argv.append(f"--lang={lang}")
    else:
        for key, flag in (("dry_run", "--dry-run"), ("allow_dirty", "--allow-dirty"),
                          ("no_llm", "--no-llm"), ("no_static_fix", "--no-static-fix")):
            if _flag(opts.get(key)):
                argv.append(flag)
        sev = (opts.get("min_severity") or "").strip()
        if sev:
            if sev not in SEVERITIES:
                raise ApiError(400, "unknown severity")
            argv.append(f"--min-severity={sev}")
        conf = opts.get("min_confidence")
        if conf not in (None, ""):
            try:
                c = float(conf)
            except (TypeError, ValueError):
                raise ApiError(400, "min_confidence must be a number") from None
            if not 0.0 <= c <= 1.0:
                raise ApiError(400, "min_confidence must be between 0 and 1")
            argv.append(f"--min-confidence={c}")
    return argv


# --------------------------------------------------------------------------- jobs

@dataclass
class Job:
    id: str
    repo_id: str
    repo_name: str
    action: str
    argv: list[str]
    options: dict
    path: Path | None = None
    status: str = "queued"          # queued | running | done | failed | cancelled
    exit_code: int | None = None
    created: float = field(default_factory=time.time)
    started: float | None = None
    ended: float | None = None
    lines: deque = field(default_factory=lambda: deque(maxlen=MAX_LOG_LINES))
    total_lines: int = 0
    proc: subprocess.Popen | None = None
    cancel_requested: bool = False
    downloads: list = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.status in ("queued", "running")

    def add_line(self, text: str):
        self.lines.append(text)
        self.total_lines += 1

    def log_since(self, since: int) -> tuple[list[str], int]:
        first = self.total_lines - len(self.lines)
        start = max(since, first) - first
        return list(itertools.islice(self.lines, start, None)), self.total_lines

    def summary(self) -> dict:
        begin = self.started or self.created
        return {"id": self.id, "repo": self.repo_id, "repo_name": self.repo_name,
                "action": self.action, "options": self.options, "status": self.status,
                "dry_run": bool(self.options.get("dry_run")),
                "exit_code": self.exit_code, "created": self.created,
                "started": self.started, "ended": self.ended,
                "seconds": round((self.ended or time.time()) - begin, 1) if self.started else 0,
                "downloads": self.downloads}


# --------------------------------------------------------------------------- app

class WebApp:
    """Everything the HTTP handler needs; kept separate so tests can drive it directly."""

    def __init__(self, roots: list[Path], token: str | None, python: str | None = None,
                 uploads: UploadStore | None = None, state_dir: Path | None = None):
        self.uploads = uploads
        # Settings chosen in the UI (the model, for now), kept across restarts.
        self.settings_path = Path(state_dir) / "settings.json" if state_dir else None
        self.settings: dict = self._load_settings()
        self.roots = [Path(r).expanduser().resolve() for r in roots]
        for r in self.roots:
            if not r.is_dir():
                raise ValueError(f"{r} is not a directory")
        self.token = token
        self.python = python or sys.executable
        self.lock = threading.Lock()
        self.jobs: dict[str, Job] = {}
        # One worker runs the queue in order: every job shares the GPU.
        self.cv = threading.Condition(self.lock)
        self.pending: deque[str] = deque()
        threading.Thread(target=self._worker, name="helmreview-jobs", daemon=True).start()
        self._status_cache: tuple[float, dict] | None = None

    # ---- auth
    def check_token(self, candidate: str | None) -> bool:
        if self.token is None:
            return True
        return bool(candidate) and hmac.compare_digest(candidate.encode(), self.token.encode())

    # ---- repositories
    @staticmethod
    def _looks_like_repo(p: Path) -> bool:
        """A git repo, a Helm chart, a folder of charts or of Kubernetes manifests.
        Cached per folder until the folder's own entries change."""
        try:
            key, mtime = str(p), p.stat().st_mtime_ns
        except OSError:
            return False
        hit = _REPO_CACHE.get(key)
        if hit and hit[0] == mtime:
            return hit[1]
        ok = (p / ".git").exists() or looks_like_project(p)
        if len(_REPO_CACHE) > 2000:
            _REPO_CACHE.clear()
        _REPO_CACHE[key] = (mtime, ok)
        return ok

    @staticmethod
    def _is_single(root: Path) -> bool:
        """A --root that is one repository itself, rather than a folder of repositories."""
        return (root / ".git").exists() or is_project_root(root)

    def list_repos(self) -> list[dict]:
        out = []
        for i, root in enumerate(self.roots):
            if self._is_single(root):
                out.append(self._repo_info(f"{i}:.", root))
                continue
            try:
                children = sorted(c for c in root.iterdir()
                                  if c.is_dir() and not c.name.startswith("."))
            except OSError:
                continue
            for c in children:
                if self._looks_like_repo(c):
                    out.append(self._repo_info(f"{i}:{c.name}", c.resolve()))
        if self.uploads:
            for meta in self.uploads.list():
                try:
                    project = self.uploads.project(meta["id"])
                except (UploadError, OSError, KeyError):
                    continue
                out.append(self._repo_info(f"u:{meta['id']}", project, meta))
        return out

    def resolve_repo(self, repo_id: str) -> Path:
        if (repo_id or "").startswith("u:"):
            if not self.uploads:
                raise ApiError(404, "unknown repository")
            try:
                path = self.uploads.project(repo_id[2:])
            except UploadError:
                raise ApiError(404, "unknown repository") from None
            if not path.is_dir():
                raise ApiError(404, "unknown repository")
            return path
        m = re.fullmatch(r"(\d+):(.+)", repo_id or "")
        if not m or int(m.group(1)) >= len(self.roots):
            raise ApiError(404, "unknown repository")
        root = self.roots[int(m.group(1))]
        name = m.group(2)
        if name != "." and ("/" in name or "\\" in name or name.startswith(".")):
            raise ApiError(404, "unknown repository")
        path = (root / name).resolve() if name != "." else root
        try:
            path.relative_to(root)
        except ValueError:
            raise ApiError(404, "unknown repository") from None
        if name == "." and not self._is_single(root):
            raise ApiError(404, "unknown repository")
        if not path.is_dir() or not self._looks_like_repo(path):
            raise ApiError(404, "unknown repository")
        return path

    def _git(self, path: Path, *args: str) -> str | None:
        try:
            p = subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                               text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return p.stdout.strip() if p.returncode == 0 else None

    def _repo_info(self, repo_id: str, path: Path, upload: dict | None = None) -> dict:
        report = path / ".helmreview" / "report.json"
        info = {"id": repo_id, "name": upload["name"] if upload else path.name,
                "path": str(path), "upload": _upload_public(upload),
                "branch": self._git(path, "rev-parse", "--abbrev-ref", "HEAD"),
                "report_time": None, "counts": None, "fix_time": None, "model": None}
        if report.is_file():
            info["report_time"] = report.stat().st_mtime
            try:
                data = json.loads(report.read_text(encoding="utf-8"))
                info["counts"] = data.get("counts")
                info["model"] = data.get("model")
            except (OSError, ValueError):
                pass
        patch = path / ".helmreview" / "fix.patch"
        if patch.is_file():
            info["fix_time"] = patch.stat().st_mtime
        return info

    def repo_detail(self, repo_id: str) -> dict:
        path = self.resolve_repo(repo_id)
        info = self._repo_info(repo_id, path, self._upload_meta(repo_id))
        cfg, warnings = load_config(path)
        status = self._git(path, "status", "--porcelain", "-uno", "--", ".",
                           ":(exclude).helmreview")
        info.update({
            "dirty": None if status is None else bool(status),
            "changed_files": [] if not status else [ln[3:] for ln in status.splitlines()][:50],
            "config": {"model": self.settings.get("model") or cfg.model, "language": cfg.language,
                       "fail_on": cfg.fail_on, "min_severity": cfg.min_severity,
                       "lint_strict": cfg.lint_strict, "workers": cfg.workers},
            "config_warnings": warnings,
            "last_commit": self._git(path, "log", "-1", "--format=%h %s"),
            "artifacts": self._artifacts(repo_id, path),
        })
        return info

    def _artifacts(self, repo_id: str, path: Path) -> list[dict]:
        """Every downloadable result this repo has right now, newest kind first."""
        base = f"/api/repos/{quote(repo_id, safe='')}"
        out = []
        if repo_id.startswith("u:"):
            out.append({"name": "fixed.zip", "label": "Project as ZIP (with applied fixes)",
                        "url": f"{base}/download"})
        for name, (label, _) in ARTIFACTS.items():
            f = path / ".helmreview" / name
            if f.is_file():
                out.append({"name": name, "label": label, "url": f"{base}/artifacts/{name}",
                            "time": f.stat().st_mtime})
        return out

    def _upload_meta(self, repo_id: str) -> dict | None:
        if not (repo_id or "").startswith("u:") or not self.uploads:
            return None
        try:
            return self.uploads.meta(repo_id[2:])
        except (UploadError, OSError, ValueError):
            return None

    # ---- uploads
    def add_upload(self, stream, length: int, filename: str) -> dict:
        if not self.uploads:
            raise ApiError(403, "uploads are switched off on this server (--no-upload)")
        try:
            meta = self.uploads.add(stream, length, filename, self._looks_like_repo)
        except UploadError as exc:
            raise ApiError(exc.status, exc.message) from None
        return {"id": f"u:{meta['id']}", **_upload_public(meta)}

    def delete_upload(self, repo_id: str) -> None:
        if not repo_id.startswith("u:") or not self.uploads:
            raise ApiError(404, "only uploads can be deleted")
        with self.lock:
            if any(j.active and j.repo_id == repo_id for j in self.jobs.values()):
                raise ApiError(409, "a job is queued or running on this upload; cancel it first")
            try:
                self.uploads.delete(repo_id[2:])
            except UploadError as exc:
                raise ApiError(exc.status, exc.message) from None

    def download_upload(self, repo_id: str) -> tuple[bytes, str]:
        if not repo_id.startswith("u:") or not self.uploads:
            raise ApiError(404, "only uploads can be downloaded")
        try:
            return self.uploads.download(repo_id[2:])
        except UploadError as exc:
            raise ApiError(exc.status, exc.message) from None

    def report(self, repo_id: str) -> dict:
        path = self.resolve_repo(repo_id)
        f = path / ".helmreview" / "report.json"
        if not f.is_file():
            return {"report": None}
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ApiError(500, f"could not read report.json: {exc}") from None
        return {"report": data, "time": f.stat().st_mtime}

    def fix_result(self, repo_id: str) -> dict:
        path = self.resolve_repo(repo_id)
        d = path / ".helmreview"
        out = {"patch": None, "report_md": None, "time": None}
        for key, name in (("patch", "fix.patch"), ("report_md", "fix-report.md")):
            f = d / name
            if f.is_file():
                out[key] = f.read_text(encoding="utf-8", errors="replace")[:MAX_FILE]
                out["time"] = max(out["time"] or 0, f.stat().st_mtime)
        return out

    def read_file(self, repo_id: str, rel: str) -> dict:
        path = self.resolve_repo(repo_id)
        if not rel or rel.startswith("/") or "\x00" in rel:
            raise ApiError(400, "invalid path")
        target = (path / rel).resolve()
        try:
            inner = target.relative_to(path)
        except ValueError:
            raise ApiError(403, "path is outside the repository") from None
        if inner.parts and inner.parts[0] == ".git":
            raise ApiError(403, "not allowed")
        if not target.is_file():
            raise ApiError(404, "file not found")
        if target.stat().st_size > MAX_FILE:
            raise ApiError(413, "file too large to show")
        return {"path": str(inner), "content": target.read_text(encoding="utf-8",
                                                                errors="replace")}

    def queue(self, repo_id: str) -> dict | None:
        f = self.resolve_repo(repo_id) / ".helmreview" / "queue.json"
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    # ---- settings
    def _load_settings(self) -> dict:
        if not self.settings_path:
            return {}
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        model = data.get("model") if isinstance(data, dict) else None
        return {"model": model} if isinstance(model, str) and MODEL_RE.match(model) else {}

    def config_model(self) -> str:
        cfg, _ = load_config(Path.home())
        return cfg.model

    def effective_model(self) -> str:
        """The model jobs use: the one picked in the UI, else the configured default."""
        return self.settings.get("model") or self.config_model()

    def set_model(self, model: str | None) -> dict:
        model = (model or "").strip()
        if model and not MODEL_RE.match(model):
            raise ApiError(400, "invalid model name")
        with self.lock:
            if model:
                self.settings["model"] = model
            else:
                self.settings.pop("model", None)   # back to the configured default
            if self.settings_path:
                try:
                    self.settings_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = self.settings_path.with_suffix(".tmp")
                    tmp.write_text(json.dumps(self.settings), encoding="utf-8")
                    tmp.replace(self.settings_path)
                except OSError as exc:
                    raise ApiError(500, f"could not save the setting: {exc}") from None
            self._status_cache = None
        return {"model": self.effective_model(), "model_source": self._model_source(),
                "persisted": bool(self.settings_path)}

    def _model_source(self) -> str:
        return "ui" if self.settings.get("model") else "config"

    def server_status(self) -> dict:
        now = time.time()
        if self._status_cache and now - self._status_cache[0] < 15:
            return self._status_cache[1]
        from .. import lint
        from ..ollama import OllamaClient
        cfg, _ = load_config(Path.home())
        model = self.settings.get("model") or cfg.model
        st = {"version": __version__, "ollama_host": cfg.ollama_host, "model": model,
              "model_source": self._model_source(), "default_model": cfg.model,
              "ollama": None, "model_available": None, "models": [],
              "helm": lint.version(),
              "roots": [str(r) for r in self.roots], "auth": self.token is not None,
              "uploads": ({"enabled": True, "max_mb": self.uploads.limits.max_upload // 2**20,
                           "keep_days": self.uploads.keep_days}
                          if self.uploads else {"enabled": False})}
        client = OllamaClient(cfg.ollama_host, model, api_key=cfg.api_key, timeout=5)
        try:
            st["ollama"] = client.version()
            st["models"] = client.models()[:50]
            st["model_available"] = client.has_model()
        except Exception as exc:  # noqa: BLE001 - surface any connection problem
            st["ollama_error"] = f"{exc.__class__.__name__}: {str(exc)[:200]}"
        self._status_cache = (now, st)
        return st

    # ---- jobs
    def running_job(self) -> Job | None:
        return next((j for j in self.jobs.values() if j.status == "running"), None)

    def start_job(self, repo_id: str, action: str, opts: dict) -> Job:
        """Queue a scan or fix. It runs in the background, after the jobs before it."""
        path = self.resolve_repo(repo_id)
        opts = {k: v for k, v in (opts or {}).items() if v not in (None, "", False, [])}
        if "model" not in opts and self.settings.get("model"):
            opts["model"] = self.settings["model"]     # the model picked in the UI
        argv = build_argv(action, path, opts)
        with self.cv:
            for j in self.jobs.values():
                if j.active and j.repo_id == repo_id and j.action == action \
                        and j.options == opts:
                    raise ApiError(409, f"the same {action} of {j.repo_name} is already "
                                        f"{'running' if j.status == 'running' else 'queued'}")
            if len(self.pending) >= MAX_PENDING:
                raise ApiError(429, f"{MAX_PENDING} jobs are already waiting; try again later")
            meta = self._upload_meta(repo_id)
            job = Job(id=secrets.token_hex(6), repo_id=repo_id,
                      repo_name=meta["name"] if meta else path.name,
                      action=action, argv=argv, options=opts, path=path)
            self.jobs[job.id] = job
            self.pending.append(job.id)
            finished = sorted((j for j in self.jobs.values() if not j.active),
                              key=lambda j: j.created)
            for old in finished[:max(0, len(self.jobs) - KEEP_JOBS)]:
                self.jobs.pop(old.id, None)
            self.cv.notify()
        return job

    def _worker(self):
        while True:
            with self.cv:
                while not self.pending:
                    self.cv.wait()
                job = self.jobs.get(self.pending.popleft())
                if not job or job.status != "queued":
                    continue
                job.status = "running"
                job.started = time.time()
            try:
                self._run(job, job.path)
            except Exception as exc:  # noqa: BLE001 - a broken job must not stop the queue
                job.add_line(f"error: {exc.__class__.__name__}: {exc}")
                job.status, job.ended = "failed", time.time()

    def _run(self, job: Job, repo: Path):
        # `python -m` puts the working directory first on sys.path, so running from inside
        # the scanned repo would let a planted helmreview.py there replace the tool. The CLI
        # gets the repo as an absolute path; run it from our own static folder instead
        # (and set PYTHONSAFEPATH, which does the same on Python 3.11+).
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8",
                   PYTHONSAFEPATH="1")
        cmd = [self.python, "-m", "helmreview", *job.argv]
        job.add_line("$ helmreview " + " ".join(job.argv[:1] + [repo.name] + job.argv[2:]))
        try:
            job.proc = subprocess.Popen(cmd, cwd=str(STATIC_DIR), env=env,
                                        stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, encoding="utf-8", errors="replace",
                                        bufsize=1, start_new_session=True)
        except OSError as exc:
            job.add_line(f"error: could not start: {exc}")
            job.status, job.ended = "failed", time.time()
            return
        # A Cancel that came in while the process was being started must not get lost.
        if job.cancel_requested:
            self._kill(job)
        assert job.proc.stdout is not None
        for line in job.proc.stdout:
            job.add_line(line.rstrip("\n"))
        job.exit_code = job.proc.wait()
        job.ended = time.time()
        job.add_line(f"[exit {job.exit_code}]")
        if job.cancel_requested:
            job.status = "cancelled"
        elif job.action == "scan":
            job.status = "done" if job.exit_code in (0, 1) else "failed"
        else:
            job.status = "done" if job.exit_code == 0 else "failed"
        if job.status == "done":
            job.downloads = self._downloads_for(job, repo)

    def _downloads_for(self, job: Job, repo: Path) -> list[dict]:
        """What this job produced, as download links (only files written by this run)."""
        base = f"/api/repos/{quote(job.repo_id, safe='')}"
        out = []
        for name, (label, kind) in ARTIFACTS.items():
            f = repo / ".helmreview" / name
            if kind == job.action and f.is_file() and f.stat().st_mtime >= (job.started or 0) - 2:
                out.append({"name": name, "label": label, "url": f"{base}/artifacts/{name}"})
        if job.action == "fix" and job.repo_id.startswith("u:") and not job.options.get("dry_run"):
            out.insert(0, {"name": "fixed.zip", "label": "Fixed ZIP", "url": f"{base}/download",
                           "primary": True})
        elif job.action == "fix" and out:
            out[0]["primary"] = True
        return out

    def artifact(self, repo_id: str, name: str) -> tuple[bytes, str, str]:
        if name not in ARTIFACTS:
            raise ApiError(404, "unknown file")
        path = self.resolve_repo(repo_id)
        f = path / ".helmreview" / name
        if not f.is_file():
            raise ApiError(404, f"{name} does not exist yet")
        meta = self._upload_meta(repo_id)
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", meta["name"] if meta else path.name)
        ctype = {"md": "text/markdown; charset=utf-8", "json": "application/json",
                 "sarif": "application/sarif+json", "patch": "text/x-diff; charset=utf-8"}
        return f.read_bytes(), f"{stem}-{name}", ctype[name.rsplit(".", 1)[1]]

    def cancel_job(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if not job:
            raise ApiError(404, "unknown job")
        with self.cv:
            if job.status == "queued":
                try:
                    self.pending.remove(job.id)
                except ValueError:
                    pass
                job.status, job.ended = "cancelled", time.time()
                job.add_line("cancelled before it started")
                return job
        if job.status == "running":
            job.cancel_requested = True     # also covers the moment before the process exists
            self._kill(job)
        return job

    @staticmethod
    def _kill(job: Job) -> None:
        if job.proc and job.proc.poll() is None:
            try:
                if hasattr(os, "killpg"):
                    os.killpg(job.proc.pid, signal.SIGTERM)
                else:
                    job.proc.terminate()
            except ProcessLookupError:
                pass

    def job_list(self) -> list[dict]:
        with self.cv:
            order = list(self.pending)
        out = []
        for j in sorted(self.jobs.values(), key=lambda j: j.created, reverse=True):
            d = j.summary()
            d["position"] = order.index(j.id) + 1 if j.id in order else None
            if j.status == "running":
                d["progress"] = self._progress(j)
            out.append(d)
        return out

    def _progress(self, job: Job) -> dict | None:
        try:
            q = self.queue(job.repo_id)
        except ApiError:
            return None
        if not q or q.get("stage") != job.action:
            return None
        counts = q.get("counts") or {}
        running = next((f.get("file") for f in q.get("files") or []
                        if f.get("status") == "running"), None)
        return {"total": q.get("total") or 0,
                "done": sum(counts.get(k, 0) for k in ("done", "cached", "failed")),
                "file": running}

    def job_detail(self, job_id: str, since: int = 0) -> dict:
        job = self.jobs.get(job_id)
        if not job:
            raise ApiError(404, "unknown job")
        lines, nxt = job.log_since(since)
        out = next((d for d in self.job_list() if d["id"] == job_id), job.summary())
        out.update(lines=lines, next=nxt)
        return out


# --------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = f"helmreview/{__version__}"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> WebApp:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # ---- low-level
    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data, extra: dict | None = None):
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", extra)

    def _token(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            return None
        morsel = jar.get(COOKIE)
        return morsel.value if morsel else None

    def _cookie_header(self, token: str) -> str:
        return f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict"

    def _read_body(self):
        """Read the request body up front, so an early error never leaves unread bytes
        on a keep-alive connection (they would be parsed as the next request)."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            raise ApiError(413, "request too large")
        self._raw = self.rfile.read(length) if length else b""

    def _body(self) -> dict:
        raw = getattr(self, "_raw", b"")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            raise ApiError(400, "body must be JSON") from None
        if not isinstance(data, dict):
            raise ApiError(400, "body must be a JSON object")
        return data

    def _check_post(self):
        if self.headers.get(CSRF_HEADER) != "1":
            raise ApiError(403, f"missing {CSRF_HEADER} header")
        origin = self.headers.get("Origin")
        if origin and origin != "null":
            if urlsplit(origin).netloc != self.headers.get("Host", ""):
                raise ApiError(403, "cross-origin request refused")

    # ---- dispatch
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str):
        url = urlsplit(self.path)
        path = url.path
        qs = parse_qs(url.query)
        try:
            if method == "POST" and path == "/api/uploads":
                # Streamed to disk after the auth checks; never kept in memory. One upload
                # per connection, so an early refusal cannot leave half a body behind.
                self.close_connection = True
            elif method == "POST":
                self._read_body()
            if method == "GET" and path in STATIC_FILES:
                return self._static(path, qs)
            if not path.startswith("/api/"):
                raise ApiError(404, "not found")
            if method == "POST":
                self._check_post()
            if path == "/api/login" and method == "POST":
                return self._login()
            if not self.app.check_token(self._token()):
                raise ApiError(401, "access token required")
            return self._api(method, path, qs)
        except ApiError as exc:
            self._json(exc.status, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001 - never leak a traceback page
            self._json(500, {"error": f"{exc.__class__.__name__}: {exc}"})

    def _static(self, path: str, qs: dict):
        name, ctype = STATIC_FILES[path]
        token = (qs.get("token") or [None])[0]
        if path == "/" and token:
            # Trade the token in the URL for a cookie and drop it from the address bar.
            if not self.app.check_token(token):
                raise ApiError(401, "wrong access token")
            self._send(303, b"", "text/plain", {"Location": "/",
                                                 "Set-Cookie": self._cookie_header(token)})
            return
        body = (STATIC_DIR / name).read_bytes()
        self._send(200, body, ctype)

    def _login(self):
        token = str(self._body().get("token") or "").strip()
        if not self.app.check_token(token):
            time.sleep(0.5)
            raise ApiError(401, "wrong access token")
        self._json(200, {"ok": True}, {"Set-Cookie": self._cookie_header(token)})

    def _api(self, method: str, path: str, qs: dict):
        app = self.app
        parts = [unquote(p) for p in path[len("/api/"):].split("/") if p]
        q = lambda k, d=None: (qs.get(k) or [d])[0]  # noqa: E731

        if method == "GET" and parts == ["status"]:
            return self._json(200, app.server_status())
        if method == "GET" and parts == ["repos"]:
            return self._json(200, {"repos": app.list_repos()})
        if method == "POST" and parts == ["uploads"]:
            return self._upload()
        if method == "POST" and parts == ["settings"]:
            body = self._body()
            if "model" not in body:
                raise ApiError(400, "nothing to change")
            return self._json(200, app.set_model(body.get("model")))
        if len(parts) >= 2 and parts[0] == "repos":
            rid = parts[1]
            rest = parts[2:]
            if method == "GET" and rest == []:
                return self._json(200, app.repo_detail(rid))
            if method == "GET" and rest == ["report"]:
                return self._json(200, app.report(rid))
            if method == "GET" and rest == ["fix"]:
                return self._json(200, app.fix_result(rid))
            if method == "GET" and rest == ["file"]:
                return self._json(200, app.read_file(rid, q("path", "")))
            if method == "GET" and rest == ["download"]:
                data, name = app.download_upload(rid)
                safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
                return self._send(200, data, "application/zip", {
                    "Content-Disposition": f"attachment; filename=\"{safe}\"; "
                                           f"filename*=UTF-8''{quote(name)}"})
            if method == "GET" and len(rest) == 2 and rest[0] == "artifacts":
                data, name, ctype = app.artifact(rid, rest[1])
                return self._send(200, data, ctype, {
                    "Content-Disposition": f"attachment; filename=\"{name}\""})
            if method == "POST" and rest == ["delete"]:
                app.delete_upload(rid)
                return self._json(200, {"ok": True})
            if method == "POST" and rest in (["scan"], ["fix"]):
                job = app.start_job(rid, rest[0], self._body())
                return self._json(202, job.summary())
        if method == "GET" and parts == ["jobs"]:
            return self._json(200, {"jobs": app.job_list()})
        if len(parts) == 2 and parts[0] == "jobs" and method == "GET":
            try:
                since = int(q("since", "0"))
            except ValueError:
                since = 0
            return self._json(200, app.job_detail(parts[1], since))
        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel" and method == "POST":
            return self._json(200, app.cancel_job(parts[1]).summary())
        raise ApiError(404, "not found")


    def _upload(self):
        try:
            length = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            length = -1
        if length < 0:
            raise ApiError(411, "Content-Length required")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in ("application/zip", "application/x-zip-compressed",
                         "application/octet-stream"):
            raise ApiError(415, "send the ZIP file as the request body (application/zip)")
        filename = unquote(self.headers.get("X-Filename") or "upload.zip")
        result = self.app.add_upload(self.rfile, length, filename)
        self._json(201, result)


def _upload_public(meta: dict | None) -> dict | None:
    if not meta:
        return None
    return {k: meta.get(k) for k in ("filename", "uploaded", "files", "bytes", "zip_bytes",
                                     "skipped", "git")}


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, app: WebApp, verbose: bool = False):
        host = addr[0]
        if ":" in host:
            import socket
            self.address_family = socket.AF_INET6
        super().__init__(addr, Handler)
        self.app = app
        self.verbose = verbose


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def make_server(app: WebApp, bind: str = "127.0.0.1", port: int = 8766,
                verbose: bool = False) -> _Server:
    return _Server((bind, port), app, verbose)


PLACEHOLDER_TOKENS = {"change-me", "changeme", "change_me", "secret", "token"}


def default_upload_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "helmreview" / "uploads"


def serve(roots: list[Path], bind: str = "127.0.0.1", port: int = 8766,
          token: str | None = None, no_auth: bool = False, open_browser: bool = False,
          verbose: bool = False, upload_dir: Path | None = None, no_upload: bool = False,
          max_upload_mb: int = 100, upload_keep_days: float = 7) -> int:
    if no_auth and not is_loopback(bind):
        print("error: --no-auth is only allowed when binding to localhost", file=sys.stderr)
        return 2
    if not no_auth:
        token = token or os.environ.get("HELMREVIEW_WEB_TOKEN") or secrets.token_urlsafe(24)
        if token.strip().lower() in PLACEHOLDER_TOKENS:
            print(f"error: the access token is still the example value '{token}'; set your own "
                  "(e.g. openssl rand -base64 24)", file=sys.stderr)
            return 2
    uploads = None
    if not no_upload:
        from .uploads import Limits
        try:
            uploads = UploadStore(upload_dir or default_upload_dir(),
                                  Limits(max_upload=max_upload_mb * 2**20,
                                         max_unpacked=max(5 * max_upload_mb, 50) * 2**20),
                                  keep_days=upload_keep_days)
        except OSError as exc:
            print(f"error: cannot use upload folder: {exc}", file=sys.stderr)
            return 2
    state_dir = (Path(upload_dir).expanduser() if upload_dir else default_upload_dir()).parent
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        state_dir = None   # settings then last until the server stops
    try:
        app = WebApp(roots, None if no_auth else token, uploads=uploads, state_dir=state_dir)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        srv = make_server(app, bind, port, verbose)
    except OSError as exc:
        print(f"error: cannot listen on {bind}:{port}: {exc}", file=sys.stderr)
        return 2
    in_container = os.environ.get("HELMREVIEW_CONTAINER") == "1"
    shown = "localhost" if bind in ("127.0.0.1", "::1", "localhost") else bind
    if bind in ("0.0.0.0", "::"):
        # In the Docker image 0.0.0.0 is only the container side; the user reaches it
        # through the port they published, normally on localhost.
        shown = "localhost" if in_container else "<this-host>"
    host_part = f"[{shown}]" if ":" in shown else shown
    url = f"http://{host_part}:{srv.server_address[1]}/"
    if token and not no_auth:
        url += "?token=" + quote(token)
    repos = app.list_repos()
    print(f"helmreview {__version__} web UI", file=sys.stderr)
    for r in app.roots:
        print(f"  root: {r}", file=sys.stderr)
    print(f"  {len(repos)} repositor{'y' if len(repos) == 1 else 'ies'} found", file=sys.stderr)
    if uploads:
        print(f"  uploads: {uploads.base} (max {max_upload_mb} MB, kept "
              f"{upload_keep_days:g} days)", file=sys.stderr)
    print(f"  open: {url}", file=sys.stderr)
    if in_container:
        print("  (inside the container; use the host port you published, and publish it "
              "on 127.0.0.1 unless a TLS proxy sits in front)", file=sys.stderr)
    elif not is_loopback(bind):
        print("  warning: listening on a non-local address over plain HTTP; put it behind "
              "a TLS reverse proxy or an SSH tunnel", file=sys.stderr)
    if open_browser:
        import webbrowser
        webbrowser.open(url.replace("<this-host>", "localhost"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for job in list(app.jobs.values()):
            if job.active:
                app.cancel_job(job.id)
        srv.server_close()
    return 0
