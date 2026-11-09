"""Link handling: expand playlist/channel URLs, download videos with yt-dlp.

Supported link formats in links.txt (one per line, '#' starts a comment):

    https://youtube.com/watch?v=...          one video, default settings
    https://youtube.com/watch?v=... | clips=5 | min=10 max=45
                                             per-link overrides ('|' separated)
    /path/to/local/video.mp4                 local file: used directly

Per-link settings: clips=N, min=N, max=N, subs=0 (no subtitles),
style=NAME. Unknown keys are ignored with a warning.

yt-dlp is imported lazily so the rest of the tool works without it.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from ..config import (OUTPUT_DIR, SOURCE_LONG_SECONDS, SOURCE_MAX_HEIGHT_LONG,
                      SOURCE_MAX_HEIGHT_SHORT)
from .lowspec import lowspec_encode_args, lowspec_input_args
from .source_text import (download_source_captions, load_caption_file,
                           sidecar_caption_file)

DOWNLOADS_DIR = OUTPUT_DIR / "downloads"

VIDEO_HOST_HINTS = re.compile(
    r"^https?://", re.IGNORECASE)

# yt-dlp format: pre-merged h264/aac MP4, capped at an adaptive height (the
# cut renders 1080x1920, so this keeps the master sharp before the vertical
# crop). The highest profile uses <=1440p for long videos and <=2160p for
# short ones; QUALITY=high/low opts into lighter source downloads.
PROBE_FORMAT = "bv*+ba/b"    # permissive: only used to LIST formats, not download


class YouTubeAuthRequired(RuntimeError):
    """YouTube refused an anonymous or invalidly authenticated request."""


_YTDLP_BROWSERS = frozenset({
    "brave", "chrome", "chromium", "edge", "firefox", "opera", "safari",
    "vivaldi", "whale",
})


_AUTH_MARKERS = (
    "sign in to confirm", "cookies-from-browser", "cookies from browser",
    "confirm you’re not a bot", "confirm you're not a bot", "login required",
    "authentication required",
)


def is_youtube_auth_error(error: BaseException) -> bool:
    """Recognize YouTube auth failures, including wrapped downloader errors."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = " ".join(
            value for value in (str(current), str(getattr(current, "msg", "")))
            if value
        ).lower()
        if any(marker in text for marker in _AUTH_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False


def _env_value(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    return str(value or "").strip()


def _parse_browser_source(value: str) -> tuple[str, str | None, str | None, str | None]:
    """Parse yt-dlp's browser[+keyring][:profile][::container] syntax."""
    browser_and_profile, separator, container = value.partition("::")
    if separator and "::" in container:
        raise ValueError(
            "YTDLP_COOKIES_FROM_BROWSER has more than one '::' container separator"
        )
    container = container.strip() or None if separator else None

    browser_part, separator, profile = browser_and_profile.partition(":")
    if separator and ":" in profile:
        raise ValueError(
            "YTDLP_COOKIES_FROM_BROWSER accepts one ':profile' segment; "
            "use '::container' for a Firefox container"
        )
    profile = profile.strip() or None if separator else None

    browser_parts = [part.strip() for part in browser_part.split("+")]
    if len(browser_parts) > 2 or not browser_parts[0]:
        raise ValueError(
            "YTDLP_COOKIES_FROM_BROWSER must be "
            "browser[+keyring][:profile][::container]"
        )
    browser = browser_parts[0].lower()
    keyring = browser_parts[1].lower() if len(browser_parts) == 2 else None
    if not browser or browser not in _YTDLP_BROWSERS:
        raise ValueError(
            f"Unsupported browser '{browser}' in YTDLP_COOKIES_FROM_BROWSER; "
            f"choose one of {', '.join(sorted(_YTDLP_BROWSERS))}"
        )
    if len(browser_parts) == 2 and not keyring:
        raise ValueError(
            "YTDLP_COOKIES_FROM_BROWSER has an empty keyring after '+'"
        )
    return browser, profile, keyring, container


def _auth_options_from_env(env: Mapping[str, str]) -> dict[str, object]:
    """Build yt-dlp auth options using metadata only; never opens cookie data."""
    browser_value = _env_value(env, "YTDLP_COOKIES_FROM_BROWSER")
    cookie_value = _env_value(env, "YTDLP_COOKIES_FILE")
    if browser_value and cookie_value:
        raise ValueError(
            "Set only one of YTDLP_COOKIES_FROM_BROWSER or "
            "YTDLP_COOKIES_FILE; unset the other authentication method"
        )

    auth: dict[str, object] = {}
    if browser_value:
        auth["cookiesfrombrowser"] = _parse_browser_source(browser_value)
    if cookie_value:
        path = Path(os.path.expanduser(cookie_value))
        if not path.exists():
            raise FileNotFoundError(
                f"YTDLP_COOKIES_FILE does not exist: {path}. "
                "Set it to an existing Netscape/Mozilla cookies.txt export."
            )
        if not path.is_file():
            raise ValueError(
                f"YTDLP_COOKIES_FILE is not a regular file: {path}. "
                "Set it to a Netscape/Mozilla cookies.txt export."
            )
        if not os.access(path, os.R_OK):
            raise PermissionError(
                f"YTDLP_COOKIES_FILE is not readable: {path}. "
                "Grant read access or choose another cookies.txt export."
            )
        # Do not parse or print the file: it contains live session credentials.
        auth["cookiefile"] = str(path)
    return auth


def youtube_auth_status(env: Mapping[str, str] | None = None) -> dict[str, object]:
    """Return safe, offline-only cookie configuration diagnostics.

    This validates environment syntax and cookie-file metadata only. It does not
    import yt-dlp, open a browser database, read cookie contents, or make a
    network request.
    """
    values = os.environ if env is None else env
    try:
        auth = _auth_options_from_env(values)
    except (OSError, ValueError) as exc:
        return {"ok": False, "method": "invalid", "message": str(exc)}

    if not auth:
        return {
            "ok": True,
            "method": "anonymous",
            "message": "No YouTube cookie source is configured; requests will be anonymous.",
        }
    if "cookiesfrombrowser" in auth:
        return {
            "ok": True,
            "method": "browser",
            "message": (
                "YTDLP_COOKIES_FROM_BROWSER is syntactically valid; browser cookie "
                "storage was not opened."
            ),
        }
    return {
        "ok": True,
        "method": "file",
        "message": (
            "YTDLP_COOKIES_FILE points to a readable regular file; cookie contents "
            "were not inspected."
        ),
    }


def youtube_auth_help() -> str:
    status = youtube_auth_status()
    if not status["ok"]:
        return (
            "Fix the local YouTube cookie configuration before retrying: "
            f"{status['message']}"
        )
    if status["method"] == "browser":
        return (
            "YouTube rejected cookies loaded from YTDLP_COOKIES_FROM_BROWSER. "
            "Check the browser/profile setting, sign in to YouTube manually in "
            "that browser, close it completely, and retry. If browser storage "
            "cannot be read here, export a fresh Netscape/Mozilla cookies.txt "
            "file and set YTDLP_COOKIES_FILE instead. Then run `Hooky doctor youtube "
            "URL` and `Hooky retry`."
        )
    if status["method"] == "file":
        return (
            "YouTube rejected YTDLP_COOKIES_FILE. Export a fresh Netscape/Mozilla "
            "cookies.txt file after a manual YouTube sign-in, keep it outside the "
            "repository, update YTDLP_COOKIES_FILE, run `Hooky doctor youtube URL`, "
            "and retry."
        )
    return (
        "YouTube rejected an anonymous request. Sign in to YouTube manually in a "
        "browser, close it, then set YTDLP_COOKIES_FROM_BROWSER=firefox (or "
        "chrome/brave/edge) and retry. Alternatively set "
        "YTDLP_COOKIES_FILE=/absolute/path/cookies.txt to a fresh Netscape/Mozilla "
        "export. Run `Hooky doctor youtube URL` before `Hooky retry`."
    )


def _auth_failure(error: BaseException) -> YouTubeAuthRequired:
    return YouTubeAuthRequired(f"{error}\n\n[youtube] {youtube_auth_help()}")


def yt_dlp_auth_opts(env: Mapping[str, str] | None = None) -> dict[str, object]:
    """Optional yt-dlp authentication, shared by downloads and channel scans.

    YouTube often answers anonymous downloads with 'Sign in to confirm you're
    not a bot'. Set one of these in .env to fix it:
      YTDLP_COOKIES_FROM_BROWSER=chrome
      YTDLP_COOKIES_FROM_BROWSER=chrome:Profile 1
      YTDLP_COOKIES_FROM_BROWSER=chrome+gnomekeyring:Profile 1
      YTDLP_COOKIES_FILE=cookies.txt

    Configuration is checked locally without reading cookie contents.
    """
    return _auth_options_from_env(os.environ if env is None else env)


def yt_dlp_auth_args(env: Mapping[str, str] | None = None) -> list[str]:
    """Use the same validated authentication settings in Studio subprocesses."""
    values = os.environ if env is None else env
    options = yt_dlp_auth_opts(values)
    if "cookiefile" in options:
        return ["--cookies", str(options["cookiefile"])]
    if "cookiesfrombrowser" in options:
        return ["--cookies-from-browser", values["YTDLP_COOKIES_FROM_BROWSER"]]
    return []


def check_youtube_access(url: str) -> bool:
    """Probe one URL without downloading and print an actionable diagnosis."""
    status = youtube_auth_status()
    if not status["ok"]:
        print(f"[youtube] invalid cookie configuration: {status['message']}",
              file=sys.stderr)
        return False
    try:
        import yt_dlp
        auth = yt_dlp_auth_opts()
        print(f"[youtube] authentication: {status['message']}")
        opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
                "skip_download": True, **auth}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            print("[youtube] no metadata returned", file=sys.stderr)
            return False
        print(f"[youtube] OK: {info.get('title') or info.get('id') or url}")
        return True
    except Exception as exc:
        if is_youtube_auth_error(exc):
            print(f"[youtube] AUTHENTICATION FAILED: {_auth_failure(exc)}",
                  file=sys.stderr)
        elif isinstance(exc, ImportError):
            print("[youtube] yt-dlp is not installed; install the download "
                  "optional dependency before probing a URL", file=sys.stderr)
        else:
            print(f"[youtube] probe failed: {exc}", file=sys.stderr)
        return False


def _format_string(max_height: int) -> str:
    return (
        f"bv*[ext=mp4][height<={max_height}]+ba[ext=m4a]"
        f"/b[ext=mp4][height<={max_height}]/b"
    )


def _pick_height(duration: float | None) -> int:
    """Adaptive master cap: 1440p for short videos, 1080p for long ones.

    Unknown duration -> the safer (lower) cap. When both caps are pinned to
    the same value this is a no-op.
    """
    if SOURCE_MAX_HEIGHT_LONG == SOURCE_MAX_HEIGHT_SHORT:
        return SOURCE_MAX_HEIGHT_SHORT
    if not duration or duration <= 0:
        return SOURCE_MAX_HEIGHT_LONG
    return (SOURCE_MAX_HEIGHT_SHORT if duration <= SOURCE_LONG_SECONDS
            else SOURCE_MAX_HEIGHT_LONG)


# --- analysis twin -----------------------------------------------------------
# AI work (Gemini frames, audio energy, scene detection) runs on a small 480p
# proxy so analysis decodes ~6x faster; the CUT always uses the master.
ANALYSIS_HEIGHT = 480


def _analysis_path(master: Path) -> Path:
    return master.with_name(master.stem + "_analysis.mp4")


def make_analysis_twin(master: Path) -> Path | None:
    """Create (or reuse) a 480p proxy of the master for AI analysis.

    Returns the twin path, or None when the master is missing/invalid or the
    encode failed (callers then just analyse the master — same as before).
    """
    if not master.exists() or master.stat().st_size == 0:
        return None
    twin = _analysis_path(master)
    if twin.exists() and twin.stat().st_size > 0 \
            and twin.stat().st_mtime >= master.stat().st_mtime - 1:
        return twin                      # already built (or master unchanged)
    tmp = twin.with_suffix(".tmp.mp4")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        *lowspec_input_args(),
        "-i", str(master),
        "-vf", f"scale=-2:{ANALYSIS_HEIGHT}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-c:a", "aac", "-b:a", "96k",   # keep audio: energy analysis needs it
        *lowspec_encode_args(),
        str(tmp),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except Exception:
        return None
    if result.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        print(f"[links] analysis twin failed for {master.name}; "
              f"using master for analysis", file=sys.stderr)
        tmp.unlink(missing_ok=True)
        return None
    tmp.replace(twin)
    print(f"[links] analysis twin ready: {twin.name} "
          f"({twin.stat().st_size / (1024 * 1024):.0f} MB)")
    return twin


def _attach_analysis(videos: list[dict]) -> None:
    for v in videos:
        # Semantic source captions already let Gemini select textually; avoid
        # transcoding the entire long master into a 480p proxy. The proxy is
        # retained only for the visual/audio fallback when captions are absent.
        v["analysis"] = (None if v.get("source_transcript")
                          else make_analysis_twin(v["path"]))


def parse_links_file(links_path: Path) -> list[dict]:
    """Parse links.txt into [{url, clips, min_len, max_len, sub_style, no_subs}].

    Skips blanks/#comments and previously-processed lines (marked '# DONE').
    """
    entries: list[dict] = []
    if not links_path.exists():
        return entries

    for line_no, raw in enumerate(links_path.read_text(encoding="utf-8").splitlines(),
                                  start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Accept the older plain ``DONE <url>`` marker as well as ``# DONE``.
        if line.upper().startswith("DONE "):
            continue
        if "# DONE" in raw or "# DONE" in line.upper():
            continue

        parts = [p.strip() for p in line.split("|")]
        url, overrides = parts[0], parts[1:]
        entry = {
            "line_no": line_no,
            "url": url,
            "clips": None, "min_len": None, "max_len": None,
            "sub_style": None, "no_subs": None,
            "raw_line": line,
        }
        for ov in overrides:
            # a segment may hold several settings: "min=10 max=45"
            for token in ov.split():
                key, _, value = token.partition("=")
                key = key.strip().lower()
                value = value.strip()
                try:
                    if key == "clips":
                        entry["clips"] = 0 if value.lower() in ("auto", "0") \
                            else max(1, int(value))
                    elif key == "min":
                        entry["min_len"] = max(3, int(value))
                    elif key == "max":
                        entry["max_len"] = max(5, int(value))
                    elif key == "subs":
                        entry["no_subs"] = value in ("0", "false", "no", "off")
                    elif key == "style":
                        entry["sub_style"] = value
                    else:
                        print(f"[links] line {line_no}: unknown setting "
                              f"'{key}' ignored", file=sys.stderr)
                except ValueError:
                    print(f"[links] line {line_no}: bad value for '{key}': "
                          f"'{value}' — ignored", file=sys.stderr)
        entries.append(entry)
    return entries


def is_url(text: str) -> bool:
    return bool(VIDEO_HOST_HINTS.match(text.strip()))


def _already_downloaded(url: str, info: dict | None = None,
                        min_requested_height: int | None = None) -> Path | None:
    """Find a suitable cached file for this link.

    A cache made with a lower quality cap must not silently satisfy a later
    highest-quality run. Older metadata has no quality marker, so it is only
    accepted when no minimum is requested; the next quality-aware run will
    refresh it.
    """
    if not DOWNLOADS_DIR.exists():
        return None
    for meta_path in DOWNLOADS_DIR.glob("*.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        # downloads/ may also contain transcript or tool JSON. Only the
        # downloader's object metadata can identify a cached URL.
        if not isinstance(meta, dict) or meta.get("url") != url:
            continue
        if min_requested_height is not None:
            cached_height = meta.get("requested_height")
            if (not isinstance(cached_height, (int, float))
                    or cached_height < min_requested_height):
                continue
        video_file = Path(meta.get("file", ""))
        if video_file.exists() and video_file.stat().st_size > 0:
            return video_file
    return None


def _merge_info(base: dict | None, extra: dict | None) -> dict:
    """Merge playlist-entry info with full info; prefer non-empty values."""
    merged = dict(base or {})
    for key, value in (extra or {}).items():
        if value not in (None, "", []) and (key not in merged or merged[key] in (None, "", [])):
            merged[key] = value
    return merged


def expand_and_download(entry: dict, limits: dict) -> tuple[list[dict], bool]:
    """Expand one link entry into processable video dicts.

    Returns (videos, processed_something):
      videos: [{"path": Path, "url": str|None, "title": str, "description": str,
                "uploader": str, "clips": int, "min_len": int, "max_len": int,
                "sub_style": str|None, "no_subs": bool|None}]
      processed_something: False when the link is new and nothing succeeded
    """
    url = entry["url"]
    videos: list[dict] = []

    # --- local file path -----------------------------------------------------
    if not is_url(url):
        p = Path(url)
        if p.exists():
            videos.append(_video_entry(p, None, None, entry, limits))
            if limits.get("make_analysis", True):
                _attach_analysis(videos)
            return videos, True
        entry["_failure_phase"] = "source"
        entry["_failure_error"] = f"Local video does not exist: {url}"
        print(f"[links] line {entry['line_no']}: not a link or file: {url}",
              file=sys.stderr)
        return videos, False

    # --- yt-dlp ---------------------------------------------------------------
    try:
        import yt_dlp
    except ImportError:
        entry["_failure_phase"] = "download"
        entry["_failure_error"] = "yt-dlp is not installed"
        print("[links] yt-dlp not installed — cannot handle links. "
              "Run: uv sync --locked --extra all", file=sys.stderr)
        return videos, False

    ydl_opts = {
        "format": PROBE_FORMAT,
        "outtmpl": str(DOWNLOADS_DIR / "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "socket_timeout": 30,
        "noplaylist": True,           # one source video per link
        "extract_flat": "in_playlist",  # list entries first, download one by one
        **yt_dlp_auth_opts(),
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        entry["_failure_phase"] = "download"
        entry["_failure_error"] = (str(_auth_failure(exc)) if is_youtube_auth_error(exc)
                                    else str(exc))
        if is_youtube_auth_error(exc):
            print(f"[links] YouTube authentication failed: {_auth_failure(exc)}",
                  file=sys.stderr)
        else:
            print(f"[links] Failed to read {url}: {exc}", file=sys.stderr)
        return videos, False

    if info is None:
        return videos, False

    flat_entries = info.get("entries") or []
    max_sources = limits.get("max_sources")
    if flat_entries:
        flat_entries = [entry for entry in flat_entries if entry]
        if max_sources:
            flat_entries = flat_entries[:max(1, int(max_sources))]
        items = [(entry, None) for entry in flat_entries]
    else:
        items = [(info, None)]

    downloaded_any = False
    for flat, _ in items:
        video_id = str(flat.get("id", ""))

        # Adaptive source quality: decide the master cap from the video's
        # length BEFORE checking the cache. A lower-quality old master must not
        # be reused when the user asks for a sharper vertical crop.
        duration = flat.get("duration")
        try:
            duration = float(duration) if duration else None
        except (TypeError, ValueError):
            duration = None
        height = _pick_height(duration)
        source_url = flat.get("url") or flat.get("webpage_url") or url
        cached = _already_downloaded(source_url, min_requested_height=height)
        caption_path = None
        if cached is not None:
            caption_path = _caption_path_from_metadata(cached)
            if caption_path is None:
                caption_path = download_source_captions(
                    source_url, video_id, DOWNLOADS_DIR)
            print(f"[links] Cached ({height}p quality target), skipping download: "
                  f"{cached.name}")
            videos.append(_video_entry(cached, source_url, None, entry, limits,
                                       cached_meta=flat,
                                       source_caption_path=caption_path))
            downloaded_any = True
            continue
        dl_opts = dict(ydl_opts)
        dl_opts["format"] = _format_string(height)
        len_note = (f"{duration / 60:.0f} min" if duration
                    else "length unknown")
        print(f"[links] {flat.get('title', video_id)[:60]}: {len_note} "
              f"-> downloading master at <= {height}p")

        full = None
        try:
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                full = ydl.extract_info(flat.get("url") or flat.get("webpage_url") or url,
                                        download=True)
        except Exception as exc:
            entry["_failure_phase"] = "download"
            entry["_failure_error"] = (str(_auth_failure(exc)) if is_youtube_auth_error(exc)
                                        else str(exc))
            if is_youtube_auth_error(exc):
                print(f"[links] YouTube authentication failed: {_auth_failure(exc)}",
                      file=sys.stderr)
            else:
                print(f"[links] Download failed for {flat.get('title', video_id)}: {exc}",
                      file=sys.stderr)
            continue

        merged = _merge_info(flat, full)
        file_path = Path(full.get("requested_downloads", [{}])[0].get("filepath", "")) \
            if full.get("requested_downloads") else None
        if file_path is None or not file_path.exists():
            # fall back to the id-based template
            candidates = sorted(DOWNLOADS_DIR.glob(f"{video_id}.*"))
            candidates = [c for c in candidates if c.suffix.lower() != ".json"]
            file_path = candidates[0] if candidates else None
        if file_path is None or not file_path.exists():
            entry["_failure_phase"] = "download"
            entry["_failure_error"] = f"Downloaded but file not found for {video_id}"
            print(f"[links] Downloaded but file not found for {video_id}", file=sys.stderr)
            continue

        meta_path = file_path.with_suffix(".json")
        source_url = merged.get("webpage_url") or url
        caption_path = download_source_captions(
            source_url, video_id, DOWNLOADS_DIR)
        meta_path.write_text(json.dumps({
            "url": source_url,
            "file": str(file_path),
            "title": merged.get("title", ""),
            "video_id": video_id,
            "requested_height": height,
            "source_captions": str(caption_path) if caption_path else "",
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        videos.append(_video_entry(file_path, source_url, merged, entry, limits,
                                   source_caption_path=caption_path))
        downloaded_any = True

    if limits.get("make_analysis", True):
        _attach_analysis(videos)
    return videos, downloaded_any


def _caption_path_from_metadata(video_path: Path) -> Path | None:
    meta_path = video_path.with_suffix(".json")
    try:
        value = json.loads(meta_path.read_text(encoding="utf-8")).get(
            "source_captions", "")
    except (OSError, json.JSONDecodeError, AttributeError):
        value = ""
    path = Path(value) if value else None
    return path if path and path.exists() else None


def _video_entry(path: Path, url: str | None, info: dict | None, entry: dict,
                 limits: dict, cached_meta: dict | None = None,
                 source_caption_path: Path | None = None) -> dict:
    meta = info or cached_meta or {}
    merged = _merge_info(cached_meta, info)
    caption_path = source_caption_path or sidecar_caption_file(path)
    return {
        "path": path,
        "url": url or (meta.get("webpage_url") if meta else None),
        "video_id": merged.get("id") or merged.get("video_id") or "",
        "source_transcript": load_caption_file(caption_path),
        "source_caption_path": str(caption_path) if caption_path else "",
        "title": merged.get("title") or path.stem,
        "description": (merged.get("description") or "")[:1200],
        "uploader": merged.get("uploader") or merged.get("channel") or "",
        # clips=None -> AUTO: content determines the count by default
        "clips": entry["clips"] if entry["clips"] is not None else limits["clips"],
        "min_len": entry["min_len"] or limits["min"],
        "max_len": entry["max_len"] or limits["max"],
        "sub_style": entry["sub_style"],
        "no_subs": entry["no_subs"],
        "analysis": None,
    }

