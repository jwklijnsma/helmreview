"""A tiny fake GitLab API for tests: projects, open merge requests and their notes."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import threading
from urllib.parse import unquote, urlsplit


class FakeGitLab:
    def __init__(self, token: str = "glpat-test"):
        self.token = token
        self.projects: dict[int, dict] = {}
        self.mrs: dict[int, list[dict]] = {}
        self.groups: dict[str, list[int]] = {}
        self.notes: dict[tuple[int, int], list[dict]] = {}
        self.calls: list[tuple[str, str]] = []
        self._next_note = 100
        self.server: ThreadingHTTPServer | None = None
        self.url = ""

    def add_project(self, pid: int, path: str, repo_url: str, group: str | None = None):
        self.projects[pid] = {"id": pid, "path_with_namespace": path, "http_url_to_repo": repo_url,
                              "web_url": f"https://gitlab.test/{path}"}
        self.mrs.setdefault(pid, [])
        if group:
            self.groups.setdefault(group, []).append(pid)

    def add_mr(self, pid: int, iid: int, sha: str, **extra) -> dict:
        mr = {"iid": iid, "project_id": pid, "sha": sha, "title": f"MR {iid}",
              "source_branch": "feature", "target_branch": "main", "draft": False,
              "source_project_id": pid, "target_project_id": pid,
              "updated_at": "2999-01-01T00:00:00Z", **extra}
        self.mrs[pid] = [m for m in self.mrs[pid] if m["iid"] != iid] + [mr]
        return mr

    def _find_project(self, ref: str) -> dict | None:
        for p in self.projects.values():
            if ref in (str(p["id"]), p["path_with_namespace"]):
                return p
        return None

    def handle(self, method: str, path: str, body: dict) -> tuple[int, object]:
        self.calls.append((method, path))
        m = re.fullmatch(r"/api/v4/projects/([^/]+)", path)
        if m and method == "GET":
            p = self._find_project(unquote(m.group(1)))
            return (200, p) if p else (404, {"message": "404 Project Not Found"})
        m = re.fullmatch(r"/api/v4/(project|group)s/([^/]+)/merge_requests", path)
        if m and method == "GET":
            ref = unquote(m.group(2))
            if m.group(1) == "project":
                p = self._find_project(ref)
                return (200, self.mrs[p["id"]]) if p else (404, {"message": "404"})
            if ref not in self.groups:
                return 404, {"message": "404 Group Not Found"}
            return 200, [mr for pid in self.groups[ref] for mr in self.mrs[pid]]
        m = re.fullmatch(r"/api/v4/projects/([^/]+)/merge_requests/(\d+)/notes(?:/(\d+))?", path)
        if m:
            p = self._find_project(unquote(m.group(1)))
            if not p:
                return 404, {"message": "404"}
            notes = self.notes.setdefault((p["id"], int(m.group(2))), [])
            if method == "GET":
                return 200, list(reversed(notes))
            if method == "POST":
                self._next_note += 1
                note = {"id": self._next_note, "body": body["body"], "system": False}
                notes.append(note)
                return 201, note
            if method == "PUT":
                for note in notes:
                    if note["id"] == int(m.group(3)):
                        note["body"] = body["body"]
                        return 200, note
                return 404, {"message": "404 Note Not Found"}
        return 404, {"message": "404 Not Found"}

    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _do(self, method: str):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                if self.headers.get("PRIVATE-TOKEN") != fake.token:
                    status, data = 401, {"message": "401 Unauthorized"}
                else:
                    status, data = fake.handle(method, urlsplit(self.path).path, body)
                raw = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._do("GET")

            def do_POST(self):
                self._do("POST")

            def do_PUT(self):
                self._do("PUT")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self.url

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
