"""Timestamped source captions used for semantic highlight selection."""
from __future__ import annotations

import html
import math
import os
import re
import sys
import time
from pathlib import Path

_TIME_RE = re.compile(
    r"(?P<a>\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?)\s*-->\s*"
    r"(?P<b>\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?)")
_TAG_RE = re.compile(r"<[^>]+>")
_WORD_RE = re.compile(r"\S+")


def _seconds(value: str) -> float:
    value = value.replace(",", ".")
    parts = [float(part) for part in value.split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        return minutes * 60 + seconds
    hours, minutes, seconds = parts
    return hours * 3600 + minutes * 60 + seconds


def _clean_text(lines: list[str]) -> str:
    text = " ".join(line.strip() for line in lines if line.strip())
    text = html.unescape(_TAG_RE.sub("", text))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_caption_file(path: Path) -> list[dict]:
    """Parse WebVTT or SRT into ``start/end/text`` dictionaries."""
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return []
    entries: list[dict] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        match = _TIME_RE.search(line)
        if not match:
            index += 1
            continue
        start, end = _seconds(match.group("a")), _seconds(match.group("b"))
        index += 1
        text_lines: list[str] = []
        while index < len(lines) and lines[index].strip():
            if not _TIME_RE.search(lines[index]):
                text_lines.append(lines[index])
            index += 1
        text = _clean_text(text_lines)
        if text and end > start:
            if entries and entries[-1]["start"] == start and entries[-1]["end"] == end:
                if text != entries[-1]["text"]:
                    entries[-1]["text"] += " " + text
            else:
                entries.append({"start": round(start, 3), "end": round(end, 3),
                                "text": text})
        index += 1
    # Automatic captions often repeat the previous line while the next line
    # rolls in. Remove only overlapping repeated text, never distant repeats.
    deduplicated: list[dict] = []
    for entry in entries:
        text = entry["text"]
        if deduplicated and entry["start"] <= deduplicated[-1]["end"] + 0.1:
            previous = deduplicated[-1]["text"].split()
            current = text.split()
            for n in range(min(len(previous), len(current)), 0, -1):
                if previous[-n:] == current[:n]:
                    text = " ".join(current[n:])
                    break
        if text:
            deduplicated.append({**entry, "text": text})
    return deduplicated


def sidecar_caption_file(video: Path) -> Path | None:
    """Find a local VTT/SRT sidecar without requiring a caption download."""
    candidates = [
        video.with_suffix(".vtt"),
        video.with_suffix(".srt"),
        video.with_name(video.stem + ".en.vtt"),
        video.with_name(video.stem + ".en.srt"),
    ]
    return next((candidate for candidate in candidates if candidate.exists()), None)


def load_caption_file(path: Path | None) -> list[dict]:
    return parse_caption_file(path) if path and path.exists() else []


def caption_words(captions: list[dict]) -> list[dict]:
    """Approximate word timings from timestamped caption cues.

    YouTube VTT/SRT tracks generally timestamp phrases rather than individual
    words. Distributing each cue's duration across its words gives the ASS
    renderer a useful karaoke timeline without paying for a second Whisper
    pass. Whisper remains the fallback when no usable source captions exist.
    """
    words: list[dict] = []
    for cue in captions:
        try:
            start = float(cue["start"])
            end = float(cue["end"])
            text = str(cue.get("text", ""))
        except (KeyError, TypeError, ValueError):
            continue
        tokens = _WORD_RE.findall(" ".join(text.split()))
        if not tokens or not math.isfinite(start + end) or start < 0 or end <= start:
            continue
        span = end - start
        # Weight longer words slightly more while keeping every token visible.
        weights = [max(1, len(token.strip(".,!?;:()[]{}\"'"))) for token in tokens]
        total = sum(weights)
        cursor = start
        for index, token in enumerate(tokens):
            next_cursor = end if index == len(tokens) - 1 else cursor + span * weights[index] / total
            if next_cursor > cursor:
                words.append({"start": round(cursor, 3), "end": round(next_cursor, 3),
                              "word": token})
            cursor = next_cursor
    return words


def validate_caption_edits(value: object) -> list[dict]:
    """Bound editor input before storing it in a worker request."""
    if not isinstance(value, list) or len(value) > 200:
        raise ValueError("Caption edits must be a list of at most 200 phrases.")
    edits, seen = [], set()
    for edit in value:
        if not isinstance(edit, dict):
            raise ValueError("Each caption edit needs start, end and text.")
        start, end, text = edit.get("start"), edit.get("end"), edit.get("text")
        if (type(start) not in {int, float} or type(end) not in {int, float}
                or not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end
                or not isinstance(text, str) or not text.strip() or len(text) > 1000 or "\0" in text):
            raise ValueError("Caption edits need valid times and 1–1000 characters of text.")
        key = (start, end)
        if key in seen:
            raise ValueError("A caption phrase can only be edited once.")
        seen.add(key)
        edits.append({"start": start, "end": end, "text": text.strip()})
    return edits


def apply_caption_edits(captions: list[dict], edits: list[dict]) -> list[dict]:
    replacements = {(edit["start"], edit["end"]): edit["text"]
                    for edit in validate_caption_edits(edits)}
    if not replacements.keys() <= {(cue["start"], cue["end"]) for cue in captions}:
        raise ValueError("A caption edit no longer matches this source. Analyze it again before exporting.")
    return [{**cue, "text": replacements.get((cue["start"], cue["end"]), cue["text"])}
            for cue in captions]


def _language_patterns() -> list[str]:
    raw = os.environ.get("SOURCE_SUB_LANGS", "en,en-US,en-GB").strip()
    return [part.strip() for part in raw.split(",") if part.strip()]


def download_source_captions(url: str, video_id: str, output_dir: Path,
                             existing: Path | None = None) -> Path | None:
    """Download one caption track through yt-dlp, preferring English.

    This downloads captions only (`skip_download=True`), never a second video.
    It returns the first non-empty VTT path and quietly falls back when a
    source has no captions.
    """
    if existing and existing.exists() and parse_caption_file(existing):
        return existing
    try:
        import yt_dlp
    except ImportError:
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    from .downloader import (_auth_failure, is_youtube_auth_error,
                              yt_dlp_auth_opts)
    # Cache the outcome, including unavailable tracks for a day. Never choose
    # an arbitrary neighbouring video's captions using a broad glob.
    from .checkpoints import load_checkpoint, save_checkpoint, digest
    languages = _language_patterns()
    cache = output_dir / f"captions_{digest([url, languages])}.json"
    signature = {"url": url, "languages": languages, "version": 1}
    previous = load_checkpoint(cache, signature)
    if isinstance(previous, dict) and time.time() - previous.get("checked", 0) < 86400:
        saved = Path(previous["path"]) if previous.get("path") else None
        if saved and saved.is_file() and parse_caption_file(saved):
            return saved
        if saved is None:
            return None
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", video_id) or digest(url)
    opts = {
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 2,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": languages,
        "subtitlesformat": "vtt",
        "outtmpl": str(output_dir / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        **yt_dlp_auth_opts(),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:
        if is_youtube_auth_error(exc):
            print(f"[captions] YouTube authentication failed: {_auth_failure(exc)}",
                  file=sys.stderr)
        else:
            print(f"[captions] source captions unavailable for {video_id}: {exc}",
                  file=sys.stderr)
        return None
    candidates = sorted(output_dir.glob(f"{safe_id}.*.vtt"))
    for language in languages:
        candidate = output_dir / f"{safe_id}.{language}.vtt"
        if candidate in candidates:
            candidates.remove(candidate)
            candidates.insert(0, candidate)
            break
    selected = next((p for p in candidates if parse_caption_file(p)), None)
    save_checkpoint(cache, signature, {"checked": time.time(),
                                     "path": str(selected) if selected else ""})
    return selected

