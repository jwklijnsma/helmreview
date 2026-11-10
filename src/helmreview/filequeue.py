"""The work list: one model request per file, status kept in .helmreview/queue.json.

The file is rewritten after every file, so you can `cat`/`jq` it from another terminal to see
what is done, what failed and what is still pending.
"""

from __future__ import annotations

import json
from pathlib import Path
import threading
import time


class FileQueue:
    def __init__(self, path: Path | None, stage: str, files: list[str], say=None):
        self.path = path
        self.stage = stage
        self.say = say or (lambda m: None)
        self.lock = threading.Lock()
        self.items = {f: {"file": f, "status": "pending", "note": "", "seconds": None}
                      for f in files}
        self.order = list(self.items)
        self.started: dict[str, float] = {}
        self.done = 0
        self._write()

    def start(self, rel: str):
        with self.lock:
            self.items[rel]["status"] = "running"
            self.started[rel] = time.monotonic()
            self._write()

    def finish(self, rel: str, status: str, note: str = ""):
        """status: done | cached | failed"""
        with self.lock:
            secs = round(time.monotonic() - self.started.get(rel, time.monotonic()), 1)
            self.items[rel].update(status=status, note=note[:200], seconds=secs)
            self.done += 1
            mark = {"done": "✓", "cached": "✓", "failed": "✗"}.get(status, "·")
            extra = " (cached)" if status == "cached" else f" {secs:.0f}s"
            self.say(f"  [{self.done}/{len(self.order)}] {mark} {rel}{extra}"
                     + (f" — {note}" if note else ""))
            self._write()

    def _write(self):
        if not self.path:
            return
        counts: dict[str, int] = {}
        for it in self.items.values():
            counts[it["status"]] = counts.get(it["status"], 0) + 1
        data = {"stage": self.stage, "total": len(self.order), "counts": counts,
                "files": [self.items[f] for f in self.order]}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1))
            tmp.replace(self.path)
        except OSError:
            pass
