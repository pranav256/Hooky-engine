"""A logbook a human can actually read.

Appends one plain line to output/logbook.txt for every important event:
downloads, clips made, uploads, failures, waits. One line = one thing that
happened, oldest first (newest at the bottom), with a timestamp. Written
best-effort — the pipeline must never die because logging failed.
"""
from __future__ import annotations

import datetime
from pathlib import Path

from ..config import OUTPUT_DIR

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

