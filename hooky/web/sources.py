"""Opaque references to uploaded media; never resolve client-supplied paths."""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
from pathlib import Path

from hooky.config import PROJECT_ROOT
from hooky.web.jobs import _redirected

LOCAL_SOURCE = re.compile(r"local:[0-9a-f]{32}\Z")
MAX_UPLOAD_BYTES = 2 * 1024 ** 3
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


def source_directory() -> Path:
    return Path(os.environ.get("HOOKY_LOCAL_SOURCE_DIR") or
                Path(os.environ.get("STATE_DIR") or PROJECT_ROOT / ".hooky-state") / "web_sources")


def local_source(reference: str, directory: Path | None = None) -> Path:
    if not LOCAL_SOURCE.fullmatch(reference):
        raise ValueError("Invalid local video reference.")
    directory = directory or source_directory()
    folder = directory / reference[6:]
    if _redirected(directory) or _redirected(folder):
        raise ValueError("Local video must remain in Studio's source directory.")
    path = next((folder / ("source" + suffix) for suffix in VIDEO_SUFFIXES
                 if (folder / ("source" + suffix)).is_file()), folder / "source.mp4")
    if _redirected(path):
        raise ValueError("Local video must remain in Studio's source directory.")
    if not path.is_file():
        raise ValueError("Local video is unavailable. Upload it again and detect hooks.")
    return path


def probe_video(path: Path) -> dict:
    result = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
        "-of", "json", str(path),
    ], capture_output=True, text=True, check=True, timeout=30)
    data = json.loads(result.stdout)
    video = next((stream for stream in data["streams"] if stream.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("Selected file has no video stream.")
    duration = float(data["format"]["duration"])
    if not math.isfinite(duration) or not 0 < duration <= 86400 or min(video["width"], video["height"]) <= 0:
        raise ValueError("Choose a video with a valid duration of at most 24 hours.")
    return {"duration": duration, "width": video["width"], "height": video["height"]}
