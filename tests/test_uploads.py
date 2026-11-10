"""ZIP uploads: safe extraction (hostile archives) and the upload → scan → fix → download flow."""

import io
import os
from pathlib import Path
import stat
import subprocess
import threading
import time
import zipfile

import pytest
import requests

from helmreview.web.server import WebApp, make_server
from helmreview.web.uploads import Limits, UploadError, UploadStore, extract_zip

import mock_ollama

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sample-repo"
TOKEN = "s3cret-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
POST = {**AUTH, "X-Helmreview": "1"}


def looks_like_repo(p: Path) -> bool:
    return WebApp._looks_like_repo(p)


def make_zip(entries, prefix="", compression=zipfile.ZIP_DEFLATED) -> bytes:
    """entries: {name: bytes} or a list of (ZipInfo|name, bytes)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        items = entries.items() if isinstance(entries, dict) else entries
        for name, data in items:
            if isinstance(name, zipfile.ZipInfo):
                zf.writestr(name, data)
            else:
                zf.writestr(prefix + name, data)
    return buf.getvalue()


def sample_zip(prefix="sample-repo-main/") -> bytes:
    entries = {}
    for p in sorted(SAMPLE.rglob("*")):
        if p.is_file() and ".helmreview" not in p.parts:
            entries[p.relative_to(SAMPLE).as_posix()] = p.read_bytes()
    return make_zip(entries, prefix)


def write(tmp_path, data: bytes) -> Path:
    f = tmp_path / "in.zip"
    f.write_bytes(data)
    return f


# ------------------------------------------------------------------ extraction safety

@pytest.mark.parametrize("name", ["../evil.yml", "a/../../evil.yml", "/etc/evil.yml",
                                  "C:/evil.yml", "..\\evil.yml", "templates\\..\\..\\evil.yml"])
def test_zip_slip_refused(tmp_path, name):
    with pytest.raises(UploadError):
        extract_zip(write(tmp_path, make_zip({name: b"x", "Chart.yaml": b"apiVersion: v2\nname: x\nversion: 0.1.0\n"})),
                    tmp_path / "out", Limits())
    assert not (tmp_path / "evil.yml").exists()


def test_symlinks_and_specials_are_skipped(tmp_path):
    link = zipfile.ZipInfo("templates/passwd")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    fifo = zipfile.ZipInfo("fifo")
    fifo.create_system = 3
    fifo.external_attr = (stat.S_IFIFO | 0o644) << 16
    data = make_zip([(link, b"/etc/passwd"), (fifo, b""), ("Chart.yaml", b"apiVersion: v2\nname: x\nversion: 0.1.0\n")])
    st = extract_zip(write(tmp_path, data), tmp_path / "out", Limits())
    assert st["skipped"] == 2 and st["files"] == 1
    assert not (tmp_path / "out" / "templates" / "passwd").exists()
    assert not (tmp_path / "out" / "fifo").exists()


def test_git_metadata_is_never_extracted(tmp_path):
    evil_cfg = b"[core]\n\tfsmonitor = touch /tmp/pwned-helmreview\n"
    data = make_zip({".git/config": evil_cfg, ".git/hooks/post-commit": b"#!/bin/sh\ntouch x\n",
                     "charts/web/.git": b"gitdir: /etc\n", "__MACOSX/._Chart.yaml": b"junk",
                     "Chart.yaml": b"apiVersion: v2\nname: x\nversion: 0.1.0\n", "charts/web/Chart.yaml": b"apiVersion: v2\nname: x\nversion: 0.1.0\n"})
    st = extract_zip(write(tmp_path, data), tmp_path / "out", Limits())
    assert st["files"] == 2
    assert not (tmp_path / "out" / ".git").exists()
    assert not (tmp_path / "out" / "charts" / "web" / ".git").exists()
    assert not (tmp_path / "out" / "__MACOSX").exists()


def test_zip_bomb_stops_on_ratio(tmp_path):
    data = make_zip({"Chart.yaml": b"apiVersion: v2\nname: x\nversion: 0.1.0\n", "big.yml": b"\0" * (30 * 2**20)})
    with pytest.raises(UploadError) as e:
        extract_zip(write(tmp_path, data), tmp_path / "out", Limits())
    assert e.value.status == 413


def test_total_size_limit_counts_real_bytes(tmp_path):
    data = make_zip({f"f{i}.yml": os.urandom(300_000) for i in range(5)},
                    compression=zipfile.ZIP_STORED)
    with pytest.raises(UploadError, match="unpacks to more than"):
        extract_zip(write(tmp_path, data), tmp_path / "out", Limits(max_unpacked=1 * 2**20))


def test_file_count_limit_and_duplicates(tmp_path):
    with pytest.raises(UploadError, match="more than 5 files"):
        extract_zip(write(tmp_path, make_zip({f"f{i}": b"x" for i in range(9)})),
                    tmp_path / "a", Limits(max_files=5))
    dup = make_zip([("Chart.yaml", b"a"), ("Chart.yaml", b"b")])
    with pytest.raises(UploadError, match="twice"):
        extract_zip(write(tmp_path, dup), tmp_path / "b", Limits())


def test_not_a_zip(tmp_path):
    with pytest.raises(UploadError, match="not a valid ZIP"):
        extract_zip(write(tmp_path, b"PK but not really"), tmp_path / "out", Limits())


# ------------------------------------------------------------------ store

def test_store_unwraps_folder_and_makes_git_repo(tmp_path):
    store = UploadStore(tmp_path / "up")
    data = sample_zip()
    meta = store.add(io.BytesIO(data), len(data), "Infra Repo (1).zip", looks_like_repo)
    project = store.project(meta["id"])
    assert project.name == "sample-repo-main" and (project / "charts" / "webapp" / "Chart.yaml").is_file()
    assert meta["name"] == "Infra Repo -1" and meta["git"] is True
    log = subprocess.run(["git", "-C", str(project), "log", "--format=%s"],
                         capture_output=True, text=True).stdout
    assert log.strip() == "upload: Infra Repo -1"
    assert ".helmreview/" in (project / ".git" / "info" / "exclude").read_text()
    assert stat.S_IMODE((tmp_path / "up").stat().st_mode) == 0o700


def test_store_rejects_non_chart_and_cleans_up(tmp_path):
    store = UploadStore(tmp_path / "up")
    data = make_zip({"docs/readme.md": b"# hi"})
    with pytest.raises(UploadError) as e:
        store.add(io.BytesIO(data), len(data), "docs.zip", looks_like_repo)
    assert e.value.status == 422
    assert list((tmp_path / "up").iterdir()) == []


def test_store_cleanup_old_uploads(tmp_path):
    store = UploadStore(tmp_path / "up", keep_days=1)
    data = sample_zip()
    old = store.add(io.BytesIO(data), len(data), "old.zip", looks_like_repo)
    meta_file = tmp_path / "up" / old["id"] / "meta.json"
    meta_file.write_text(meta_file.read_text().replace(str(old["uploaded"]),
                                                       str(time.time() - 3 * 86400)))
    new = store.add(io.BytesIO(data), len(data), "new.zip", looks_like_repo)  # triggers cleanup
    assert [m["id"] for m in store.list()] == [new["id"]]


def test_store_rejects_bad_ids(tmp_path):
    store = UploadStore(tmp_path / "up")
    for bad in ("../x", "", "ABCDEF123456", "0123456789ab/../.."):
        with pytest.raises(UploadError):
            store.project(bad)


# ------------------------------------------------------------------ API

@pytest.fixture()
def server(tmp_path, monkeypatch):
    srv_o, url = mock_ollama.start()
    monkeypatch.setenv("OLLAMA_HOST", url)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    (tmp_path / "roots").mkdir()
    store = UploadStore(tmp_path / "uploads", Limits(max_upload=5 * 2**20))
    app = WebApp([tmp_path / "roots"], TOKEN, uploads=store)
    srv = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield app, base, tmp_path
    for j in list(app.jobs.values()):
        if j.proc and j.proc.poll() is None:
            app.cancel_job(j.id)
    srv.shutdown()
    srv_o.shutdown()


def upload(base, data, name="infra.zip", headers=POST, ctype="application/zip"):
    return requests.post(f"{base}/api/uploads", data=data, timeout=60,
                         headers={**headers, "Content-Type": ctype, "X-Filename": name})


def wait(base, job_id, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        d = requests.get(f"{base}/api/jobs/{job_id}", headers=AUTH).json()
        if d["status"] not in ("queued", "running"):
            return d
        time.sleep(0.3)
    raise AssertionError("job did not finish")


def test_upload_needs_token_and_csrf_header(server):
    _, base, _ = server
    assert upload(base, sample_zip(), headers={"X-Helmreview": "1"}).status_code == 401
    assert upload(base, sample_zip(), headers=AUTH).status_code == 403
    r = upload(base, sample_zip(), headers={**POST, "Origin": "http://evil.example"})
    assert r.status_code == 403


def test_upload_validation(server):
    _, base, _ = server
    assert upload(base, b"hello", ctype="text/plain").status_code == 415
    assert upload(base, b"not a zip").status_code == 400
    big = make_zip({"Chart.yaml": b"apiVersion: v2\nname: x\nversion: 0.1.0\n", "x.bin": os.urandom(6 * 2**20)},
                   compression=zipfile.ZIP_STORED)
    try:   # refused before the body is read: a 413, or the client sees the socket close
        assert upload(base, big).status_code == 413
    except requests.ConnectionError:
        pass
    r = upload(base, make_zip({"../../evil.yml": b"x"}))
    assert r.status_code == 400 and "leaves the folder" in r.json()["error"]


def test_upload_scan_fix_download_delete(server):
    app, base, tmp = server
    r = upload(base, sample_zip(), name="my infra.zip")
    assert r.status_code == 201, r.text
    up = r.json()
    rid = up["id"]
    assert rid.startswith("u:") and up["files"] > 3

    repos = requests.get(f"{base}/api/repos", headers=AUTH).json()["repos"]
    mine = next(x for x in repos if x["id"] == rid)
    assert mine["name"] == "my infra" and mine["upload"]["filename"] == "my infra.zip"
    assert requests.get(f"{base}/api/status", headers=AUTH).json()["uploads"]["enabled"]

    detail = requests.get(f"{base}/api/repos/{rid}", headers=AUTH).json()
    assert detail["dirty"] is False and detail["upload"]["files"] == up["files"]

    job = requests.post(f"{base}/api/repos/{rid}/scan", json={"no_lint": True}, headers=POST).json()
    assert wait(base, job["id"])["status"] == "done"
    rep = requests.get(f"{base}/api/repos/{rid}/report", headers=AUTH).json()["report"]
    assert {"HR001", "llm:selector-label-mismatch"} <= {f["rule"] for f in rep["findings"]}
    f = requests.get(f"{base}/api/repos/{rid}/file", params={"path": "charts/webapp/values.yaml"},
                     headers=AUTH)
    assert f.status_code == 200

    job = requests.post(f"{base}/api/repos/{rid}/fix", json={}, headers=POST).json()
    done = wait(base, job["id"])
    assert done["status"] == "done"
    assert done["downloads"][0]["name"] == "fixed.zip" and done["downloads"][0]["primary"]
    assert requests.get(f"{base}/api/repos/{rid}", headers=AUTH).json()["dirty"] is True

    dl = requests.get(f"{base}/api/repos/{rid}/download", headers=AUTH)
    assert dl.status_code == 200 and dl.headers["Content-Type"] == "application/zip"
    assert "my infra.zip" in requests.utils.unquote(dl.headers["Content-Disposition"])
    with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
        names = zf.namelist()
        assert all(n.startswith("my infra/") for n in names)
        assert not any("/.git/" in n or "/.helmreview/" in n for n in names)
        svc = zf.read("my infra/charts/webapp/templates/service.yaml").decode()
    assert "webapp.selectorLabels" in svc    # the applied fix is in the download

    assert requests.post(f"{base}/api/repos/{rid}/delete", headers=POST).status_code == 200
    assert requests.get(f"{base}/api/repos/{rid}", headers=AUTH).status_code == 404
    assert list((tmp / "uploads").iterdir()) == []


def test_download_and_delete_only_for_uploads(server):
    _, base, tmp = server
    repo = tmp / "roots" / "infra"
    repo.mkdir()
    (repo / "Chart.yaml").write_text("apiVersion: v2\nname: infra\nversion: 0.1.0\n")
    assert requests.get(f"{base}/api/repos/0:infra/download", headers=AUTH).status_code == 404
    assert requests.post(f"{base}/api/repos/0:infra/delete", headers=POST).status_code == 404
    assert repo.exists()


def test_uploads_switched_off(tmp_path):
    (tmp_path / "r").mkdir()
    app = WebApp([tmp_path / "r"], TOKEN)
    srv = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        assert upload(base, sample_zip()).status_code == 403
    finally:
        srv.shutdown()
