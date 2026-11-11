"""GitLab merge-request watcher: comment text, config, and full polls against a fake GitLab."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from helmreview import gitlab
from helmreview.cli import main
from helmreview.config import load_config
from helmreview.gitlab import GitLab, MRWatcher, build_note, note_sha, watcher_from_config

from fake_gitlab import FakeGitLab
import mock_ollama

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sample-repo"
TASKS = "deploy/rbac.yaml"
BAD_TASK = ("---\napiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRoleBinding\n"
            "metadata:\n  name: ci-admin\nroleRef:\n  apiGroup: rbac.authorization.k8s.io\n"
            "  kind: ClusterRole\n  name: cluster-admin\nsubjects:\n"
            "  - kind: ServiceAccount\n    name: ci\n    namespace: ci\n")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                           *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def origin(tmp_path):
    """A project with main and merge request !1 (one more binding to cluster-admin)."""
    repo = tmp_path / "origin"
    shutil.copytree(SAMPLE, repo)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    git(repo, "checkout", "-qb", "feature")
    with (repo / TASKS).open("a") as fh:
        fh.write(BAD_TASK)
    git(repo, "commit", "-qam", "ci admin")
    git(repo, "update-ref", "refs/merge-requests/1/head", "HEAD")
    return repo


@pytest.fixture()
def fake(origin, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    fg = FakeGitLab()
    fg.start()
    fg.add_project(7, "platform/infra", str(origin), group="platform")
    fg.add_mr(7, 1, git(origin, "rev-parse", "refs/merge-requests/1/head"))
    yield fg
    fg.stop()


def watcher(fake, tmp_path, **kw):
    kw.setdefault("projects", ["platform/infra"])
    kw.setdefault("scan_args", lambda: ["--no-llm", "--no-lint"])
    kw.setdefault("say", lambda msg: None)
    kw.setdefault("state_dir", tmp_path / "state")
    return MRWatcher(gitlab=GitLab(fake.url, fake.token), token=fake.token, **kw)


def push(origin, fake, text="# reviewed by the platform team\n"):
    with (origin / TASKS).open("a") as fh:
        fh.write(text)
    git(origin, "commit", "-qam", "more")
    git(origin, "update-ref", "refs/merge-requests/1/head", "HEAD")
    sha = git(origin, "rev-parse", "HEAD")
    fake.add_mr(7, 1, sha)
    return sha


# ---- the comment

def test_note_lists_findings_with_links():
    report = {"units": [{"kind": "role"}], "model": "qwen", "findings": [
        {"rule": "HR002", "severity": "high", "file": "charts/a b/templates/x.yaml", "line": 12,
         "message": "privileged container", "suggestion": "drop privileged: true"},
        {"rule": "llm:x", "severity": "low", "file": "site.yml", "line": None, "message": "meh"}]}
    body = build_note(report, sha="a" * 40, web_url="https://gl/p", lang="nl", fail_on="high")
    assert body.startswith(gitlab.MARKER) and note_sha(body) == "a" * 40
    assert "2 bevinding(en)" in body and "op of boven `high`: 1" in body
    assert f"[L12](https://gl/p/-/blob/{'a' * 40}/charts/a%20b/templates/x.yaml#L12)" in body
    assert body.index("HR002") < body.index("llm:x")          # worst first


def test_note_cannot_run_quick_actions_or_mention_people():
    evil = "fine\n/merge\n/close @all <!-- x --> ![t](http://evil/x.png)"
    report = {"units": [1], "errors": ["boom\n/approve"], "findings": [
        {"rule": "r`\n/merge", "severity": "weird", "file": "a.yml\n/close", "line": 3,
         "message": evil, "suggestion": evil}]}
    body = build_note(report, sha="b" * 40)
    assert not [line for line in body.splitlines() if line.lstrip().startswith("/")]
    assert "@all" not in body and "![t]" not in body
    assert body.count("<!--") == 1                             # only our own marker


def test_note_without_findings_and_without_units():
    assert "No findings" in build_note({"units": [1], "findings": []}, sha="c" * 40)
    assert "changes nothing" in build_note({"units": [], "findings": []}, sha="c" * 40)
    many = [{"rule": "r", "severity": "low", "file": "a.yml", "line": i, "message": "m"}
            for i in range(1, 60)]
    assert "and 19 more" in build_note({"units": [1], "findings": many}, sha="c" * 40)


# ---- config

def test_config_from_env_and_repo_config_is_not_trusted(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HELMREVIEW_GITLAB_PROJECTS", "a/b, c/d ,")
    monkeypatch.setenv("HELMREVIEW_GITLAB_INTERVAL", "30")
    monkeypatch.setenv("HELMREVIEW_GITLAB_SKIP_DRAFTS", "true")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".helmreview.yml").write_text("gitlab_url: https://evil.example\n")
    cfg, warnings = load_config(repo)
    assert cfg.gitlab_projects == ["a/b", "c/d"] and cfg.gitlab_interval == 30
    assert cfg.gitlab_skip_drafts is True and cfg.gitlab_url == ""
    assert any("gitlab_url" in w for w in warnings)


def test_watcher_from_config_says_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("GITLAB_TOKEN", "HELMREVIEW_GITLAB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    cfg, _ = load_config(tmp_path)
    with pytest.raises(ValueError, match="GitLab URL"):
        watcher_from_config(cfg)
    cfg.gitlab_url = "gitlab.example.nl"
    with pytest.raises(ValueError, match="token"):
        watcher_from_config(cfg)
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    with pytest.raises(ValueError, match="nothing to watch"):
        watcher_from_config(cfg)
    cfg.gitlab_groups = ["platform"]
    w = watcher_from_config(cfg, state_dir=tmp_path / "s")
    assert w.gitlab.url == "https://gitlab.example.nl" and w.groups == ["platform"]


def test_git_gets_the_token_through_the_environment_only(monkeypatch):
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    env = gitlab.git_env("https://gitlab.example.nl/", "s3cret")
    assert env["GIT_CONFIG_KEY_0"] == "http.https://gitlab.example.nl/.extraHeader"
    assert "s3cret" not in env["GIT_CONFIG_VALUE_0"] and env["GIT_TERMINAL_PROMPT"] == "0"
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")              # settings already there stay
    env = gitlab.git_env("https://gitlab.example.nl", "s3cret")
    assert env["GIT_CONFIG_COUNT"] == "3" and env["GIT_CONFIG_KEY_2"].endswith(".extraHeader")


# ---- polling

def test_poll_reviews_once_then_updates_the_same_comment(fake, origin, tmp_path):
    w = watcher(fake, tmp_path)
    assert w.poll_once() == {"open": 1, "reviewed": 1, "failed": 0, "skipped": 0}
    notes = fake.notes[(7, 1)]
    assert len(notes) == 1 and "HR011" in notes[0]["body"] and "ci-admin" in notes[0]["body"]
    # only the changed lines are reported: the sample's older problems stay out
    assert "deployer-admin" not in notes[0]["body"] and "HR001" not in notes[0]["body"]

    assert w.poll_once()["skipped"] == 1 and len(fake.notes[(7, 1)]) == 1

    sha = push(origin, fake)
    assert w.poll_once()["reviewed"] == 1
    assert len(fake.notes[(7, 1)]) == 1 and note_sha(notes[0]["body"]) == sha
    work = next((tmp_path / "state" / "work").iterdir())
    assert fake.token not in (work / ".git" / "config").read_text()


def test_lost_state_does_not_review_again(fake, tmp_path):
    watcher(fake, tmp_path).poll_once()
    shutil.rmtree(tmp_path / "state")
    before = fake.notes[(7, 1)][0]["body"]
    w = watcher(fake, tmp_path)
    w.checkout = None                       # a second review would have to check out
    assert w.poll_once()["reviewed"] == 1
    assert fake.notes[(7, 1)][0]["body"] == before
    assert json.loads((tmp_path / "state" / "state.json").read_text())["7!1"]["status"] == "done"


def test_group_drafts_forks_and_stale_merge_requests(fake, origin, tmp_path):
    sha = fake.mrs[7][0]["sha"]
    fake.add_mr(7, 2, sha, draft=True)
    fake.add_mr(7, 3, sha, source_project_id=99)
    fake.add_mr(7, 4, sha, updated_at="2001-01-01T00:00:00Z")
    fake.add_mr(7, 5, None)
    w = watcher(fake, tmp_path, projects=["platform/infra"], groups=["platform"],
                skip_drafts=True, skip_forks=True, dry_run=True)
    said = []
    w.say = said.append
    assert w.poll_once() == {"open": 5, "reviewed": 1, "failed": 0, "skipped": 4}
    assert len(said) == 1 and "platform/infra!1" in said[0]
    assert not fake.notes and not (tmp_path / "state").exists()      # dry run touches nothing


def test_nothing_reviewable_means_no_comment(fake, origin, tmp_path):
    git(origin, "checkout", "-q", "main")
    git(origin, "checkout", "-qb", "docs")
    (origin / "NOTES.txt").write_text("hello\n")
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", "notes")
    git(origin, "update-ref", "refs/merge-requests/2/head", "HEAD")
    fake.mrs[7] = []
    fake.add_mr(7, 2, git(origin, "rev-parse", "HEAD"))
    w = watcher(fake, tmp_path)
    assert w.poll_once()["reviewed"] == 1 and not fake.notes.get((7, 2))
    assert w.poll_once()["skipped"] == 1


def test_failed_scan_is_retried_then_given_up(fake, tmp_path):
    w = watcher(fake, tmp_path, scan_args=lambda: ["--no-such-flag"])
    for _ in range(gitlab.MAX_FAILURES):
        assert w.poll_once()["failed"] == 1
    assert w.poll_once() == {"open": 1, "reviewed": 0, "failed": 0, "skipped": 1}
    assert not fake.notes.get((7, 1))
    said = []                                                 # GitLab down: warn, carry on
    w = watcher(fake, tmp_path, state_dir=tmp_path / "s2", say=said.append)
    fake.token = "rotated"
    assert w.poll_once()["open"] == 0 and "HTTP 401" in said[0]


def test_a_planted_symlink_is_not_followed(fake, origin, tmp_path):
    secret = tmp_path / "host-secret.yml"
    secret.write_text("api_password: hunter2hunter2\n")
    (origin / "deploy" / "leak.yaml").symlink_to(secret)
    git(origin, "add", "-A")
    push(origin, fake)
    w = watcher(fake, tmp_path)
    assert w.poll_once()["reviewed"] == 1
    work = next((tmp_path / "state" / "work").iterdir())
    leak = work / "deploy" / "leak.yaml"
    assert not leak.is_symlink() and "hunter2" not in leak.read_text()
    assert "hunter2" not in fake.notes[(7, 1)][0]["body"]


def test_cli_once_with_the_model(fake, origin, tmp_path, monkeypatch, capsys):
    srv, url = mock_ollama.start()
    try:
        with (origin / "charts/webapp/templates/service.yaml").open("a") as fh:
            fh.write("    tier: web\n")
        sha = push(origin, fake)
        monkeypatch.setenv("OLLAMA_HOST", url)
        monkeypatch.setenv("GITLAB_TOKEN", fake.token)
        monkeypatch.setenv("HELMREVIEW_GITLAB_URL", fake.url)
        rc = main(["watch-mrs", "--project", "7", "--once", "--lang", "nl",
                   "--state-dir", str(tmp_path / "state")])
        assert rc == 0, capsys.readouterr().err
        body = fake.notes[(7, 1)][0]["body"]
        assert note_sha(body) == sha and "helmreview-review" in body
        assert "llm:selector-label-mismatch" in body and "qwen2.5-coder:14b" in body
        assert main(["watch-mrs", "--project", "7", "--once", "--dry-run",
                     "--state-dir", str(tmp_path / "state")]) == 0
        assert "0 reviewed" in capsys.readouterr().err
    finally:
        srv.shutdown()
