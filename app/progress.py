"""Progress of long-running tasks with a periodic status line.

While a task runs, a background thread logs one line every STATUS_INTERVAL
seconds: task | phase n/m (%) | elapsed | estimated remaining time of the
phase. Nothing is logged while idle or for tasks shorter than one interval.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

log = logging.getLogger("wissensdatenbank-mcp.index")


def fmt_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class BuildCancelled(Exception):
    """The running update was cancelled (wdb cancel)."""


class Progress:
    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._lock = threading.Lock()
        self.task: str | None = None
        self.task_t0 = 0.0
        self.phase: str | None = None
        self.phase_t0 = 0.0
        self.done = 0
        self.total: int | None = None
        self._depth = 0
        self.cancel_requested = threading.Event()
        if interval > 0:
            threading.Thread(target=self._loop, name="status", daemon=True).start()

    def start(self, task: str) -> None:
        with self._lock:
            self._depth += 1
            if self._depth == 1:
                self.task, self.task_t0 = task, time.monotonic()
                self.phase, self.phase_t0, self.done, self.total = None, self.task_t0, 0, None

    def end(self) -> None:
        with self._lock:
            self._depth = max(self._depth - 1, 0)
            if self._depth == 0:
                self.task = self.phase = None

    def set_phase(self, phase: str, total: int | None = None) -> None:
        with self._lock:
            self.phase, self.phase_t0, self.done, self.total = phase, time.monotonic(), 0, total

    def advance(self, n: int = 1) -> None:
        with self._lock:
            self.done += n

    def check_cancel(self) -> None:
        if self.cancel_requested.is_set():
            raise BuildCancelled("update cancelled")

    def snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            if self.task is None:
                return None
            now = time.monotonic()
            snap: dict[str, Any] = {"task": self.task, "phase": self.phase,
                                    "elapsed_s": round(now - self.task_t0),
                                    "phase_elapsed_s": round(now - self.phase_t0)}
            if self.total:
                snap.update(done=self.done, total=self.total,
                            percent=round(100 * self.done / self.total))
                if self.done:
                    rate = (now - self.phase_t0) / self.done
                    snap["phase_remaining_s"] = round(rate * (self.total - self.done))
            return snap

    def line(self) -> str | None:
        snap = self.snapshot()
        if not snap:
            return None
        parts = [f"Status: {snap['task']}"]
        if snap.get("phase"):
            p = snap["phase"]
            if "total" in snap:
                p += f" {snap['done']}/{snap['total']} ({snap['percent']} %)"
            parts.append(p)
        parts.append(f"phase {fmt_duration(snap['phase_elapsed_s'])}, total {fmt_duration(snap['elapsed_s'])}")
        if "phase_remaining_s" in snap:
            parts.append(f"phase remaining ~{fmt_duration(snap['phase_remaining_s'])}")
        return " | ".join(parts)

    def _loop(self) -> None:
        while True:
            time.sleep(self.interval)
            snap = self.snapshot()
            if not snap or snap["elapsed_s"] < self.interval:
                continue
            line = self.line()
            if line:
                log.info(line)


progress = Progress(float(os.environ.get("STATUS_INTERVAL", "60")))
