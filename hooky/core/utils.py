"""Small media helpers shared by highlight selection, subtitles, and cutting."""
from __future__ import annotations

import json
import subprocess
from functools import lru_cache
from pathlib import Path

from ..config import CLIP_HEIGHT, CLIP_WIDTH


def _probe_key(path: Path) -> tuple[str, int, int]:
    """Return a cache key that changes when a media file is replaced."""
    resolved = path.resolve()
    stat = resolved.stat()
    return str(resolved), stat.st_mtime_ns, stat.st_size


@lru_cache(maxsize=256)
def _get_duration_cached(path: str, _mtime_ns: int, _size: int) -> float:
    result = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "format=duration", "-of", "json", path,
    ], capture_output=True, text=True, check=True, timeout=30)
    return float(json.loads(result.stdout)["format"]["duration"])


def get_duration(path: Path) -> float:
    """Return video duration using ffprobe, cached per file revision."""
    return _get_duration_cached(*_probe_key(path))


def format_timestamp(seconds: float) -> str:
    """Format seconds as H:MM:SS or M:SS string."""
    seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def validate_video_url(url: str) -> bool:
    """Validate that a URL has an http(s) scheme, a host, and no leading option flags."""
    if not isinstance(url, str):
        return False
    trimmed = url.strip()
    if not trimmed or trimmed.startswith("-"):
        return False
    from urllib.parse import urlparse
    try:
        parsed = urlparse(trimmed)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def clamp(value: float, low: float, high: float) -> float:
    """Clamp a number to the inclusive interval [low, high]."""
    return max(low, min(high, value))


@lru_cache(maxsize=256)
def _has_audio_cached(path: str, _mtime_ns: int, _size: int) -> bool:
    result = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=index", "-of", "csv=p=0", path,
    ], capture_output=True, text=True, timeout=30)
    return result.returncode == 0 and result.stdout.strip() != ""


def has_audio(path: Path) -> bool:
    """Return whether the video has at least one audio stream."""
    return _has_audio_cached(*_probe_key(path))


def safe_duration_clamp(start: float, end: float, total: float) -> tuple[float, float]:
    """Ensure a (start, end) window is inside the video bounds and valid."""
    start = clamp(start, 0.0, max(0.0, total - 0.5))
    end = clamp(end, start + 0.5, total)
    return start, end


def vertical_crop_filter(framing: str = "center", background_height: int = 0) -> str:
    """Fit or center-crop a source onto the 1080x1920 Instagram canvas.

    ``fit`` is the safe selective smart-framing mode: it preserves the entire
    source with a blurred duplicate behind it, avoiding accidental loss of a
    speaker, slide, or two-person conversation. ``center`` remains the clean
    default for close-up/portrait material.
    """
    if framing == "fit":
        if background_height:
            height = max(2, int(background_height) // 2 * 2)
            width = max(2, round(CLIP_WIDTH * height / CLIP_HEIGHT) // 2 * 2)
            background = (
                f"[a]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},gblur=sigma=12,eq=brightness=-0.10,"
                f"scale={CLIP_WIDTH}:{CLIP_HEIGHT}[bg];")
        else:
            background = (
                f"[a]scale={CLIP_WIDTH}:{CLIP_HEIGHT}:force_original_aspect_ratio=increase,"
                f"crop={CLIP_WIDTH}:{CLIP_HEIGHT},gblur=sigma=24,eq=brightness=-0.10[bg];")
        return (
            f"split[a][b];"
            + background +
            f"[b]scale={CLIP_WIDTH}:{CLIP_HEIGHT}:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2"
        )
    if framing == "pad":
        return (f"scale={CLIP_WIDTH}:{CLIP_HEIGHT}:force_original_aspect_ratio=decrease,"
                f"pad={CLIP_WIDTH}:{CLIP_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black")
    return (
        f"scale={CLIP_WIDTH}:{CLIP_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={CLIP_WIDTH}:{CLIP_HEIGHT}"
    )

