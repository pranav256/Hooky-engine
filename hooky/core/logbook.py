"""Human-readable run events and opt-in pipeline timing records.

Appends one plain line to output/logbook.txt for every important event:
downloads, clips made, uploads, failures, waits. One line = one thing that
happened, oldest first (newest at the bottom), with a timestamp. Written
best-effort — the pipeline must never die because logging failed.
"""
from __future__ import annotations

import datetime
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..config import OUTPUT_DIR
from .checkpoints import atomic_json

LOGBOOK = OUTPUT_DIR / "logbook.txt"

ICONS = {
    "download": "⬇",
    "twin": "🎞",
    "clips": "✂",
    "caption": "💬",
    "queued": "📋",
    "post": "🚀",
    "fail": "❌",
    "wait": "💤",
    "skip": "⏭",
    "info": "•",
    "error": "‼",
}


def log(event: str, message: str, path: Path | None = None) -> None:
    """Append one line to the logbook. Never raises.

    `event` is one of the ICONS keys (unknown -> •). The file lives at
    output/logbook.txt; override the location with `path` (tests use this).
    """
    try:
        target = path if path is not None else LOGBOOK
        target.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%d-%m %H:%M")
        icon = ICONS.get(event, "•")
        with target.open("a", encoding="utf-8") as fh:
            fh.write(f"{ts}  {icon}  {message}\n")
    except Exception:
        pass  # logging must never break the pipeline


def tail(n: int = 30, path: Path | None = None) -> str:
    """Last n lines of the logbook (for --queue-status / debugging)."""
    try:
        target = path if path is not None else LOGBOOK
        if not target.exists():
            return "(logbook is empty — nothing has happened yet)"
        lines = target.read_text(encoding="utf-8").splitlines()
        return "\n".join(lines[-n:])
    except Exception as exc:
        return f"(could not read logbook: {exc})"


class PipelineProfile:
    """Small JSON-safe profiler that is inactive unless explicitly enabled."""

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled
        self.data: dict = {"version": 1, "stages": {}, "counters": {}}

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        if not self.enabled:
            yield
            return
        started = time.perf_counter()
        try:
            yield
        finally:
            self.data["stages"][name] = round(time.perf_counter() - started, 4)

    def count(self, name: str, value: int = 1) -> None:
        if self.enabled:
            self.data["counters"][name] = self.data["counters"].get(name, 0) + value

    def save(self, path: Path) -> None:
        if self.enabled:
            atomic_json(path, self.data)

