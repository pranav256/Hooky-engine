"""Date-independent, bounded discovery from compact channel metadata.

Lifetime views dominate ranking; channel-relative performance and title-only
clip/topic heuristics are secondary signals, not claims about video content.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..config import (CHANNEL_BACKLOG_MAX, CHANNEL_SCAN_MAX, CHANNELS_FILE,
                      LINKS_FILE, OUTPUT_DIR)
from .downloader import yt_dlp_auth_opts

SEEN_FILE = OUTPUT_DIR / "channel_history.json"
DEFAULT_TOP_N = 5
MIN_SOURCE_HEIGHT = 720
MIN_SOURCE_SECONDS = 30
_CACHE_FIELDS = {
    "id", "url", "title", "view_count", "like_count", "comment_count",
    "duration", "height", "width", "live_status", "is_live", "is_upcoming",
    "availability", "has_audio", "has_video",
}


def _number_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _int_value(value: Any, default: int = 0) -> int:
    number = _number_value(value)
    return int(number) if number is not None else default


def channel_video_id(url: str) -> str:
    """Recognize watch/shorts/embed/live and short URLs, without query suffixes."""
    value = str(url or "").strip().split("|", 1)[0].split()
    if not value:
        return ""
    value = value[0]
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return value
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return ""
    parts = parsed.path.strip("/").split("/")
    video_id = ""
    if host in {"youtu.be", "www.youtu.be"}:
        video_id = parts[0]
    elif host == "youtube.com" or host.endswith(".youtube.com"):
        if parts[0] == "watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif len(parts) > 1 and parts[0] in {"shorts", "embed", "live"}:
            video_id = parts[1]
    return video_id if re.fullmatch(r"[A-Za-z0-9_-]+", video_id) else ""


def channel_video_url(entry: dict) -> str:
    """Return one canonical watch URL, regardless of listing URL spelling."""
    for key in ("webpage_url", "original_url", "url", "id"):
        video_id = channel_video_id(entry.get(key) or "")
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"
    return ""


def _channel_videos_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("https://", "http://")):
        url = f"https://www.youtube.com/@{url.lstrip('@')}"
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if not path.endswith(("/videos", "/streams", "/shorts")):
        path += "/videos"
    return f"https://{(parsed.hostname or '').lower()}{path}"


def parse_channels_file(path: Path = CHANNELS_FILE) -> list[dict]:
    """Parse channel URLs with optional ``limit``, ``min_views`` and ``scan``.

    ``scan`` caps flat listing entries, not full metadata requests. Zero scans
    the complete listing so old winners are not silently excluded by recency.
    """
    if not path.exists():
        return []
    entries: list[dict] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line.upper().startswith("DONE "):
            continue
        parts = [part.strip() for part in line.split("|")]
        entry = {"line_no": line_no, "url": parts[0], "limit": DEFAULT_TOP_N,
                 "min_views": 0, "scan": CHANNEL_SCAN_MAX}
        for option in parts[1:]:
            for token in option.split():
                key, _, value = token.partition("=")
                key, value = key.lower().strip(), value.strip()
                if key in {"limit", "top", "videos"}:
                    entry["limit"] = max(1, _int_value(value, DEFAULT_TOP_N))
                elif key in {"min_views", "views"}:
                    entry["min_views"] = _int_value(value)
                elif key in {"scan", "scan_max"}:
                    entry["scan"] = _int_value(value)
                elif key:
                    print(f"[channels] line {line_no}: unknown setting '{key}' ignored",
                          file=sys.stderr)
        entries.append(entry)
    return entries


def _load_history(path: Path | None = None) -> dict:
    """Fail closed on unreadable history rather than silently replacing it."""
    path = path or SEEN_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Invalid channel history in {path}")
    return data


def _history_ids(history: dict) -> set[str]:
    return (set(history) | {channel_video_id(key) for key in history}
            | {channel_video_id(item.get("url", "")) for item in history.values()
               if isinstance(item, dict)})


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", delete=False) as output:
            name = output.name
            json.dump(data, output, indent=2, ensure_ascii=False, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


@contextmanager
def _file_lock(path: Path):
    """Serialize short discovery writes; release automatically on process exit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _save_history(history: dict, path: Path | None = None) -> None:
    path = path or SEEN_FILE
    with _file_lock(path.with_name(path.name + ".lock")):
        # Re-read under the lock: other additions must not disappear into our snapshot.
        merged = _load_history(path)
        for video_id, record in history.items():
            old = merged.get(video_id)
            merged[video_id] = ({**old, **record}
                                if isinstance(old, dict) and isinstance(record, dict)
                                else record)
        _atomic_json(path, merged)


def _active_link_count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for raw in path.read_text(encoding="utf-8").splitlines()
               if (line := raw.strip()) and not line.startswith("#")
               and not line.upper().startswith("DONE ") and "# DONE" not in line.upper())


def _existing_links(path: Path) -> set[str]:
    """Include DONE and manually commented URLs: neither should be rediscovered."""
    if not path.exists():
        return set()
    return {match.group(0) for raw in path.read_text(encoding="utf-8").splitlines()
            if (match := re.search(r"https?://[^\s|#]+", raw))}


def _media_facts(entry: dict) -> dict:
    """Summarize known formats, never confusing video-only with no audio."""
    facts = {key: entry[key] for key in ("has_audio", "has_video", "height", "width")
             if key in entry}
    formats = entry.get("formats")
    if isinstance(formats, list) and formats:
        formats = [item for item in formats if isinstance(item, dict)]
        for codec, flag in (("acodec", "has_audio"), ("vcodec", "has_video")):
            known = [item.get(codec) for item in formats]
            if any(value and value != "none" for value in known):
                facts[flag] = True
            elif known and all(value == "none" for value in known):
                facts[flag] = False
        heights = [_number_value(item.get("height")) for item in formats
                   if item.get("vcodec") != "none"]
        heights = [height for height in heights if height is not None]
        if heights:
            facts["height"] = max(heights)
    else:
        for codec, flag in (("acodec", "has_audio"), ("vcodec", "has_video")):
            if entry.get(codec):
                facts[flag] = entry[codec] != "none"
    return facts


def _compact_entry(entry: dict) -> dict:
    compact = {key: value for key, value in entry.items() if key in _CACHE_FIELDS
               and (value is None or isinstance(value, (str, bool, int, float)))}
    compact.update(_media_facts(entry))
    for key in ("view_count", "like_count", "comment_count", "duration", "height", "width"):
        if key in compact:
            compact[key] = _number_value(compact[key])
    url = channel_video_url(entry)
    compact.update(id=channel_video_id(url), url=url)
    return compact


def _suitable(entry: dict) -> bool:
    if entry.get("is_live") is True or entry.get("is_upcoming") is True:
        return False
    if entry.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
        return False
    availability = str(entry.get("availability") or "").lower()
    if availability in {"private", "premium_only", "subscriber_only", "needs_auth",
                        "unavailable", "deleted", "blocked"}:
        return False
    if str(entry.get("title") or "").strip().lower() in {
            "[private video]", "[deleted video]", "[unavailable video]"}:
        return False
    duration = _number_value(entry.get("duration"))
    if duration is not None and duration < MIN_SOURCE_SECONDS:
        return False
    facts = _media_facts(entry)
    if facts.get("has_audio") is False or facts.get("has_video") is False:
        return False
    height = _number_value(facts.get("height"))
    return height is None or height >= MIN_SOURCE_HEIGHT


_POTENTIAL_WORDS = {
    "why", "how", "secret", "truth", "story", "impossible", "mistake",
    "debate", "surprising", "hidden", "first", "last", "does", "happens",
    "explained", "real", "most", "worst", "best",
}
_TOPIC_STOPWORDS = _POTENTIAL_WORDS | {
    "the", "and", "for", "with", "this", "that", "from", "you", "your",
    "are", "was", "part", "episode", "about", "video",
}


def _title_tokens(title: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", title.lower())
            if len(token) > 2 and not token.isdigit()}


def _topic_tokens(entry: dict) -> set[str]:
    return _title_tokens(str(entry.get("title") or "")) - _TOPIC_STOPWORDS


def _similar_topic(tokens: set[str], previous: list[set[str]]) -> bool:
    return bool(tokens) and any(
        tokens == old or (len(tokens & old) >= 2 and
                          len(tokens & old) / max(1, len(tokens | old)) >= 0.6)
        for old in previous)


def _unique_entries(entries: list[dict]) -> list[dict]:
    unique: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        compact = _compact_entry(entry)
        video_id = compact["id"]
        if video_id:
            old = unique.get(video_id, {})
            merged = {**old, **{key: value for key, value in compact.items()
                               if value is not None}}
            if old.get("view_count") is not None:
                merged["view_count"] = max(old["view_count"],
                                           compact.get("view_count") or 0)
            unique[video_id] = merged
    return list(unique.values())


def rank_entries(entries: list[dict], limit: int = DEFAULT_TOP_N,
                 min_views: int = 0, seen_ids: set[str] | None = None, *,
                 include_unknown_views: bool = False,
                 channel_baseline: float | None = None) -> list[dict]:
    """Rank by 85% lifetime views, 10% channel-relative views, 5% title heuristic.

    Relative performance uses the median known lifetime views of the channel
    listing. No upload date or guessed engagement/media facts enter the score.
    Near-identical title topics are deferred, not permanently blacklisted.
    """
    limit = _int_value(limit)
    if not limit:
        return []
    seen_ids = seen_ids or set()
    entries = _unique_entries(entries)
    known_views = [entry["view_count"] for entry in entries
                   if entry.get("view_count") is not None]
    max_views = max(known_views, default=0)
    baseline = channel_baseline if channel_baseline is not None else (
        statistics.median(known_views) if known_views else None)
    candidates: list[dict] = []
    for entry in entries:
        views = entry.get("view_count")
        if entry["id"] in seen_ids or not _suitable(entry):
            continue
        if (views is None and min_views > 0 and not include_unknown_views) or (
                views is not None and views < min_views):
            continue
        lifetime = math.log1p(views or 0) / max(1.0, math.log1p(max_views))
        relative = views / baseline if views is not None and baseline else None
        relative_score = relative / (1 + relative) if relative is not None else 0
        potential = min(1.0, len(_title_tokens(str(entry.get("title") or "")) &
                                 _POTENTIAL_WORDS) / 2.0)
        candidates.append({**entry, "channel_url": entry["url"], "video_id": entry["id"],
                           "view_count": int(views) if views is not None else None,
                           "performance_score": 0.85 * lifetime + 0.10 * relative_score
                           + 0.05 * potential,
                           "ranking_signals": {"lifetime_views": views,
                                               "channel_relative_views": relative,
                                               "clip_potential_title_heuristic": potential}})
    candidates.sort(key=lambda item: (-item["performance_score"],
                                      -(item["view_count"] or 0),
                                      str(item.get("title") or "").lower(), item["video_id"]))
    selected, deferred, topics = [], [], []
    for candidate in candidates:
        tokens = _topic_tokens(candidate)
        if _similar_topic(tokens, topics):
            deferred.append(candidate)
        else:
            selected.append(candidate)
            topics.append(tokens)
    return (selected + deferred)[:limit]


def _fetch_channel_entries(url: str, scan: int = 0) -> list[dict]:
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError(
            "yt-dlp is required for channel discovery. "
            "Install it with: uv sync --locked --extra all"
        )
    opts = {"skip_download": True, "quiet": True, "no_warnings": True,
            "ignoreerrors": True, "extract_flat": "in_playlist", **yt_dlp_auth_opts()}
    if scan > 0:
        opts["playlistend"] = scan
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(_channel_videos_url(url), download=False)
        if not info:
            raise RuntimeError("channel listing unavailable")
        return [entry for entry in (info.get("entries") or []) if isinstance(entry, dict)]


def _cache_path(url: str, scan: int) -> Path:
    key = f"{_channel_videos_url(url)}\n{scan}"
    return OUTPUT_DIR / "channel_cache" / (hashlib.sha256(key.encode()).hexdigest() + ".json")


def _read_cache(url: str, scan: int) -> dict | None:
    try:
        data = json.loads(_cache_path(url, scan).read_text(encoding="utf-8"))
        if (isinstance(data, dict) and data.get("url") == _channel_videos_url(url)
                and data.get("scan") == scan and isinstance(data.get("entries"), list)
                and all(isinstance(entry, dict) for entry in data["entries"])
                and _number_value(data.get("fetched_at")) is not None):
            data["fetched_at"] = _number_value(data["fetched_at"])
            return data
    except (OSError, ValueError, TypeError):
        pass
    return None


def _write_cache(url: str, scan: int, data: dict) -> None:
    try:
        _atomic_json(_cache_path(url, scan), data)
    except (OSError, ValueError, TypeError) as exc:
        print(f"[channels] cache not saved for {url}: {exc}", file=sys.stderr)


def _load_channel_entries(url: str, scan: int) -> list[dict]:
    hours = _number_value(os.environ.get("CHANNEL_CACHE_HOURS", "24"))
    ttl = (24 if hours is None else hours) * 3600
    cached = _read_cache(url, scan)
    if cached and 0 <= time.time() - cached["fetched_at"] < ttl:
        return _unique_entries(cached["entries"])
    entries = _unique_entries(_fetch_channel_entries(url, scan))
    _write_cache(url, scan, {"url": _channel_videos_url(url), "scan": scan,
                            "fetched_at": time.time(), "entries": entries})
    return entries


def _needs_enrichment(entry: dict) -> bool:
    return any(entry.get(key) is None for key in
               ("view_count", "duration", "has_audio", "has_video", "height"))


def _enrich_shortlist(entries: list[dict]) -> list[dict]:
    """Only bounded shortlist entries receive full metadata; never media bytes."""
    if not any(_needs_enrichment(entry) for entry in entries):
        return entries
    try:
        import yt_dlp
    except ImportError:
        return entries
    opts = {"skip_download": True, "quiet": True, "no_warnings": True,
            "ignoreerrors": True, "noplaylist": True, **yt_dlp_auth_opts()}
    enriched = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        for entry in entries:
            if _needs_enrichment(entry):
                try:
                    detail = ydl.extract_info(entry["channel_url"], download=False)
                    if isinstance(detail, dict):
                        compact = _compact_entry(detail)
                        # Never let a redirected/invalid result change the selected ID.
                        if compact["id"] == entry["video_id"]:
                            entry = {**entry, **{key: value for key, value in compact.items()
                                                if value is not None}}
                except Exception as exc:
                    print(f"[channels] metadata skipped for {entry['channel_url']}: {exc}",
                          file=sys.stderr)
            enriched.append(entry)
    return enriched


def _round_robin(pools: list[list[dict]], limits: list[int], slots: int) -> list[dict]:
    pools = [list(pool) for pool in pools]
    selected, seen, topics = [], set(), []
    counts = [0] * len(pools)
    while len(selected) < slots:
        progress = False
        for index, pool in enumerate(pools):
            pool[:] = [entry for entry in pool if entry["video_id"] not in seen]
            if not pool or counts[index] >= limits[index]:
                continue
            varied = next((i for i, entry in enumerate(pool)
                           if not _similar_topic(_topic_tokens(entry), topics)), 0)
            entry = pool.pop(varied)
            selected.append(entry)
            seen.add(entry["video_id"])
            topics.append(_topic_tokens(entry))
            counts[index] += 1
            progress = True
            if len(selected) >= slots:
                break
        if not progress:
            break
    return selected


def scan_channels(channels_path: Path = CHANNELS_FILE,
                  links_path: Path = LINKS_FILE,
                  backlog_max: int = CHANNEL_BACKLOG_MAX, *,
                  pending_render_count: int = 0) -> list[dict]:
    """Append fairly across channels, bounded by active links + pending renders.

    Callers may supply an additional pending-render count; discovery does not
    read or mutate the posting queue. Existing excess links are never removed.
    Full metadata requests are bounded to twice the remaining source slots.
    """
    remaining = max(0, _int_value(backlog_max) - _active_link_count(links_path)
                    - _int_value(pending_render_count))
    if not remaining:
        print(f"[channels] backlog is full (limit {backlog_max}); not adding more")
        return []
    channel_specs = parse_channels_file(channels_path)
    if not channel_specs:
        print(f"[channels] No channels configured in {channels_path}")
        return []
    try:
        history = _load_history()
    except (OSError, ValueError) as exc:
        print(f"[channels] history unreadable; discovery skipped: {exc}", file=sys.stderr)
        return []
    seen = _history_ids(history) | {channel_video_id(url) for url in _existing_links(links_path)}
    pools, specs, listings = [], [], []
    budget = 2 * remaining
    for spec in channel_specs:
        try:
            entries = _load_channel_entries(spec["url"], spec["scan"])
            choices = rank_entries(entries, limit=budget, min_views=spec["min_views"],
                                   seen_ids=seen, include_unknown_views=True)
            pools.append([{**choice, "source_channel": spec["url"]} for choice in choices])
            specs.append(spec)
            listings.append(entries)
        except Exception as exc:
            print(f"[channels] {spec['url']} failed: {exc}", file=sys.stderr)
    shortlist = _round_robin(pools, [2 * spec["limit"] for spec in specs], budget)
    try:
        shortlist = _enrich_shortlist(shortlist)
    except Exception as exc:
        print(f"[channels] shortlist metadata unavailable: {exc}", file=sys.stderr)
    details = {entry["video_id"]: _compact_entry(entry) for entry in shortlist}
    final_pools = []
    for spec, entries in zip(specs, listings):
        updated = [{**entry, **details.get(entry["id"], {})} for entry in entries]
        cached = _read_cache(spec["url"], spec["scan"])
        if cached and details:
            _write_cache(spec["url"], spec["scan"], {**cached, "entries": updated})
        views = [entry["view_count"] for entry in updated if entry.get("view_count") is not None]
        baseline = statistics.median(views) if views else None
        candidates = [entry for entry in shortlist if entry["source_channel"] == spec["url"]]
        ranked = rank_entries(candidates, limit=budget, min_views=spec["min_views"],
                              seen_ids=seen, channel_baseline=baseline)
        final_pools.append([{**entry, "source_channel": spec["url"]} for entry in ranked])
    selected = _round_robin(final_pools, [spec["limit"] for spec in specs], remaining)
    if not selected:
        return []

    # Serialize discovery commits, not network work. External link-file writers
    # must cooperate with this lock to make their read/replace operations atomic.
    with _file_lock(links_path.with_name(links_path.name + ".channels.lock")):
        # Recheck after network work: preserve manual suppression and new links.
        existing_ids = {channel_video_id(url) for url in _existing_links(links_path)}
        try:
            latest_history = _load_history()
        except (OSError, ValueError) as exc:
            print(f"[channels] history changed; discovery skipped: {exc}", file=sys.stderr)
            return []
        existing_ids.update(_history_ids(latest_history))
        remaining = max(0, _int_value(backlog_max) - _active_link_count(links_path)
                        - _int_value(pending_render_count))
        selected = [entry for entry in selected if entry["video_id"] not in existing_ids][:remaining]
        if not selected:
            return []
        with links_path.open("a+", encoding="utf-8") as output:
            output.seek(0, os.SEEK_END)
            if output.tell():
                # Text-mode cookies are not byte offsets; inspect the last byte separately.
                with links_path.open("rb") as existing:
                    existing.seek(-1, os.SEEK_END)
                    if existing.read(1) not in {b"\n", b"\r"}:
                        output.write("\n")
            for choice in selected:
                output.write(choice["channel_url"] + "\n")
            output.flush()
            os.fsync(output.fileno())
        additions = {entry["video_id"]: {"url": entry["channel_url"],
                     "title": entry.get("title", ""), "views": entry.get("view_count"),
                     "channel": entry["source_channel"], "selected_at": time.time(),
                     "ranking_signals": entry["ranking_signals"]} for entry in selected}
        try:
            _save_history(additions)
        except (OSError, ValueError) as exc:
            # Links are durable and suppress reselection even if history fails.
            print(f"[channels] links saved, but history not saved: {exc}", file=sys.stderr)
    for choice in selected:
        views = choice.get("view_count")
        label = f"{views:,} views" if views is not None else "views unknown"
        print(f"[channels] selected {str(choice.get('title') or choice['video_id'])[:70]} "
              f"({label}): {choice['channel_url']}")
    print(f"[channels] Added {len(selected)} high-performing video(s) to {links_path}")
    return selected

