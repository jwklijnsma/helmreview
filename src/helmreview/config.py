"""Configuration: defaults < ~/.helmreview.yml < repo .helmreview.yml < env < CLI flags."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
import os
from pathlib import Path

import yaml

from .models import Severity

CONFIG_NAMES = (".helmreview.yml", ".helmreview.yaml")

# Keys a repo-level config is NOT allowed to set unless the user opts in,
# so a cloned repo cannot point your review at someone else's server.
REPO_UNTRUSTED_KEYS = {"ollama_host", "api_key", "gitlab_url", "gitlab_token_env",
                       "gitlab_projects", "gitlab_groups"}


@dataclass
class Config:
    ollama_host: str = "http://localhost:11434"
    model: str = "qwen2.5-coder:14b"
    api_key: str = ""                 # for an Ollama behind an auth proxy
    language: str = "en"              # "en" or "nl"
    temperature: float = 0.1
    num_ctx: int = 16384
    timeout: int = 600                # max seconds without output from Ollama (incl. queue wait)
    deadline: int = 0                 # max seconds for one request (0 = 2 x timeout)
    num_predict: int = 4096           # cap on generated tokens; stops runaway JSON loops
    keep_alive: str = "30m"           # keep the model loaded between requests
    retries: int = 2
    workers: int = 1                  # parallel LLM requests (1 = one GPU)
    per_file: bool = True             # one model request per file (False: group a role's files)
    max_chars_per_unit: int = 24000   # split a unit when its source exceeds this
    max_file_chars: int = 20000       # truncate single huge files
    use_llm: bool = True
    use_lint: bool = True
    use_static: bool = True
    lint_strict: bool = False         # helm lint --strict (warnings fail the lint)
    values_context_chars: int = 6000  # values.yaml shown next to a template under review
    fail_on: str = "high"             # exit non-zero at or above this severity
    min_severity: str = "info"        # hide findings below this in reports
    min_confidence: float = 0.5       # drop LLM findings below this confidence
    exclude: list[str] = field(default_factory=lambda: [
        ".git/**", ".tox/**", ".venv/**", "venv/**", "**/node_modules/**",
        ".cache/**", ".github/**", ".helmreview/**", "**/charts/*.tgz",
    ])
    fix_min_severity: str = "medium"  # `fix`: only let the model fix findings at/above this
    fix_min_confidence: float = 0.8   # `fix`: ...and at/above this confidence
    fix_batch_size: int = 0           # `fix`: findings per request; 0 = all of a file in one
                                      # request (split further on timeout)
    fix_context_lines: int = 25       # `fix`: lines of context around each finding
    fix_full_file_lines: int = 120    # `fix`: send the whole file when it is this short
    # `watch-mrs` / `serve --watch-mrs`: review GitLab merge requests as they come in.
    gitlab_url: str = ""              # e.g. https://gitlab.example.nl
    gitlab_token_env: str = "GITLAB_TOKEN"   # env var holding the token (scope: api)
    gitlab_projects: list[str] = field(default_factory=list)   # group/project paths or ids
    gitlab_groups: list[str] = field(default_factory=list)     # every project in these groups
    gitlab_interval: int = 120        # seconds between polls
    gitlab_skip_drafts: bool = False  # leave draft merge requests alone
    gitlab_skip_forks: bool = False   # leave merge requests from forks alone
    gitlab_max_age_days: float = 14   # ignore merge requests not updated for this long (0 = off)
    house_rules: str = ""             # extra instructions appended to the LLM prompt
    disabled_rules: list[str] = field(default_factory=list)  # e.g. ["HR009", "helm-lint:icon"]

    @property
    def fail_on_severity(self) -> Severity:
        return Severity.parse(self.fail_on, Severity.HIGH)

    @property
    def min_severity_level(self) -> Severity:
        return Severity.parse(self.min_severity, Severity.INFO)

    def update(self, data: dict, *, trusted: bool = True, source: str = "") -> list[str]:
        """Apply a dict of settings. Returns warnings."""
        warnings = []
        known = {f.name: f for f in fields(self)}
        for key, value in (data or {}).items():
            key = key.replace("-", "_")
            if key not in known:
                warnings.append(f"{source}: unknown config key '{key}' ignored")
                continue
            if not trusted and key in REPO_UNTRUSTED_KEYS:
                warnings.append(
                    f"{source}: '{key}' ignored in a repo config "
                    "(set HELMREVIEW_TRUST_REPO_CONFIG=1 to allow)")
                continue
            if value is None:
                continue
            setattr(self, key, value)
        return warnings


ENV_MAP = {
    "OLLAMA_HOST": "ollama_host",
    "HELMREVIEW_OLLAMA_HOST": "ollama_host",
    "HELMREVIEW_MODEL": "model",
    "HELMREVIEW_API_KEY": "api_key",
    "HELMREVIEW_LANGUAGE": "language",
    "HELMREVIEW_WORKERS": "workers",
    "HELMREVIEW_NUM_CTX": "num_ctx",
    "HELMREVIEW_TIMEOUT": "timeout",
    "HELMREVIEW_NUM_PREDICT": "num_predict",
    "HELMREVIEW_FIX_BATCH_SIZE": "fix_batch_size",
    "HELMREVIEW_FAIL_ON": "fail_on",
    "HELMREVIEW_GITLAB_URL": "gitlab_url",
    "HELMREVIEW_GITLAB_TOKEN_ENV": "gitlab_token_env",
    "HELMREVIEW_GITLAB_PROJECTS": "gitlab_projects",     # comma-separated
    "HELMREVIEW_GITLAB_GROUPS": "gitlab_groups",         # comma-separated
    "HELMREVIEW_GITLAB_INTERVAL": "gitlab_interval",
    "HELMREVIEW_GITLAB_SKIP_DRAFTS": "gitlab_skip_drafts",
    "HELMREVIEW_GITLAB_SKIP_FORKS": "gitlab_skip_forks",
}


def _read_yaml(path: Path) -> dict:
    try:
        with path.open() as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def _normalise_host(host: str) -> str:
    host = host.strip().rstrip("/")
    if host and "://" not in host:
        host = "http://" + host
    # OLLAMA_HOST is often "0.0.0.0:11434" on the server itself
    return host.replace("://0.0.0.0", "://127.0.0.1")


def load_config(root: Path, explicit: Path | None = None) -> tuple[Config, list[str]]:
    cfg = Config()
    warnings: list[str] = []

    for name in CONFIG_NAMES:
        p = Path.home() / name
        if p.is_file():
            warnings += cfg.update(_read_yaml(p), source=str(p))
            break

    trust_repo = os.environ.get("HELMREVIEW_TRUST_REPO_CONFIG") == "1"
    for name in CONFIG_NAMES:
        p = root / name
        if p.is_file():
            warnings += cfg.update(_read_yaml(p), trusted=trust_repo, source=str(p))
            break

    if explicit:
        warnings += cfg.update(_read_yaml(explicit), source=str(explicit))

    int_keys = {"workers", "num_ctx", "timeout", "num_predict", "fix_batch_size",
                "gitlab_interval"}
    list_keys = {"gitlab_projects", "gitlab_groups"}
    bool_keys = {"gitlab_skip_drafts", "gitlab_skip_forks"}
    for env, key in ENV_MAP.items():
        val = os.environ.get(env)
        if not val:
            continue
        if key in list_keys:
            setattr(cfg, key, [v.strip() for v in val.split(",") if v.strip()])
        elif key in bool_keys:
            setattr(cfg, key, val.strip().lower() in ("1", "true", "yes", "on"))
        else:
            setattr(cfg, key, int(val) if key in int_keys else val)
    for key in list_keys:                     # `gitlab_projects: group/repo` in YAML
        if isinstance(getattr(cfg, key), str):
            setattr(cfg, key, [getattr(cfg, key)])

    cfg.ollama_host = _normalise_host(cfg.ollama_host)
    return cfg, warnings
