"""Small versioned checkpoints. Cache corruption means recompute, never success."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()[:20]


def file_revision(path: Path) -> dict:
    stat = path.stat()
    return {"path": str(path.resolve()), "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns}


def source_id(video: Path, url: str = "") -> str:
    """Keep source identity stable across download quality and title changes."""
    if url:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
            ident = (parsed.path.strip("/") if host == "youtu.be" else
                     parse_qs(parsed.query).get("v", [""])[0])
            if ident:
                return "yt_" + digest(ident)
        return "url_" + digest(url)
    return "local_" + digest(str(video.resolve()))


def clip_id(source: str, start: float, end: float) -> str:
    return f"{source}_{round(start * 1000)}_{round(end * 1000)}"


def atomic_json(path: Path, data: object, *, sort_keys: bool = False,
                allow_nan: bool = True, indent: int | None = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(data, output, indent=indent, ensure_ascii=False,
                      sort_keys=sort_keys, allow_nan=allow_nan)
            output.flush()
            os.fsync(output.fileno())
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


def load_checkpoint(path: Path, signature: object):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("signature") == signature:
            return data["value"]
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        pass
    return None


def save_checkpoint(path: Path, signature: object, value: object) -> None:
    atomic_json(path, {"signature": signature, "value": value})

