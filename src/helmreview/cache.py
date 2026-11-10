"""Small JSON cache for model answers, so re-runs only pay for what changed."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading


class JsonCache:
    def __init__(self, path: Path | None):
        self.path = path
        self.lock = threading.Lock()
        self.data: dict = {}
        self.dirty = False
        if path:
            try:
                self.data = json.loads(path.read_text())
            except (OSError, ValueError):
                self.data = {}

    @staticmethod
    def key(*parts: str) -> str:
        h = hashlib.sha256()
        for p in parts:
            h.update(str(p).encode("utf-8", "surrogateescape") + b"\0")
        return h.hexdigest()

    def get(self, key: str):
        with self.lock:
            return self.data.get(key)

    def put(self, key: str, value):
        with self.lock:
            self.data[key] = value
            self.dirty = True

    def save(self):
        if not self.path or not self.dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock:
                text = json.dumps(self.data)
            self.path.write_text(text)
        except OSError:
            pass
