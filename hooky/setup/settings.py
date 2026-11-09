"""Configuration keys, safe defaults, and adaptive recommendations."""
from __future__ import annotations

from pathlib import Path

from .profiler import SystemProfile

PROFILE_NAMES = ("adaptive", "balanced", "quality", "low")
QUALITY_NAMES = ("highest", "high", "low")
WHISPER_MODELS = ("tiny", "base", "small")
WHISPER_COMPUTE_TYPES = ("int8", "float16", "float32")
SUBTITLE_SOURCES = ("auto", "source", "whisper")
SUBTITLE_STYLES = ("hormozi", "clean", "yellow", "gold", "titanium", "minimal", "emerald", "crimson")
YOUTUBE_BROWSERS = (
    "chrome", "chromium", "firefox", "brave", "edge", "opera", "safari",
    "vivaldi", "whale",
)

SECRET_KEYS = frozenset({"GEMINI_API_KEY", "IG_PASSWORD", "IG_2FA_CODE"})

# These are the settings owned by the setup wizard. Unknown .env values are
# retained by storage.py so setup does not erase user extensions.
MANAGED_KEYS = (
    "HOOKY_SETUP_COMPLETE", "HOOKY_SETUP_VERSION", "HOOKY_PROFILE",
    "GEMINI_API_KEY", "GEMINI_MODEL",
    "GEMINI_MODEL_FALLBACKS", "GEMINI_MIN_INTERVAL_SECONDS",
    "GEMINI_SELECTION_CHUNK_CHARS",
    "YTDLP_COOKIES_FROM_BROWSER", "YTDLP_COOKIES_FILE",
    "WHISPER_MODEL", "WHISPER_COMPUTE", "WHISPER_THREADS", "FFMPEG_THREADS",
    "QUALITY", "SOURCE_MAX_HEIGHT_LONG", "SOURCE_MAX_HEIGHT_SHORT", "X264_PRESET",
    "SUBTITLE_SOURCE", "SUB_STYLE", "SUB_LANG",
    "OUTPUT_DIR", "LINKS_FILE", "CHANNELS_FILE", "STATE_DIR",
    "AUTO_CLIP_MODE", "AUTO_MAX_CLIPS", "AUTO_CANDIDATES_PER_CHUNK",
    "AUTO_CLIP_DENSITY_MINUTES", "PARTS_TARGET_SECONDS", "PARTS_BACKGROUND",
    "POST_HOURS", "QUEUE_MIN_GAP_MINUTES", "QUEUE_MAX_GAP_MINUTES",
    "QUEUE_MAX_ATTEMPTS", "QUEUE_MAX_PENDING", "MAKE_MAX_SOURCES",
    "CHANNEL_BACKLOG_MAX", "CHANNEL_SCAN_MAX", "MAKE_COVERS", "AUDIO_NORMALIZATION",
    "DELETE_SOURCE_AFTER_CLIP", "DELETE_POSTED_CLIPS",
    "IG_USERNAME", "IG_PASSWORD", "IG_2FA_CODE", "IG_SESSION_FILE",
)


def _ram_gb(profile: SystemProfile) -> float:
    return profile.ram_gb or 0.0


def quality_defaults(quality: str) -> dict[str, str]:
    """Return source and encoder settings for an explicit quality tier."""
    values = {
        "highest": ("1440", "2160", "medium"),
        "high": ("1080", "1440", "medium"),
        "low": ("720", "1080", "veryfast"),
    }
    try:
        long_height, short_height, preset = values[quality]
    except KeyError as exc:
        raise ValueError(f"Unknown quality tier: {quality}") from exc
    return {
        "QUALITY": quality,
        "SOURCE_MAX_HEIGHT_LONG": long_height,
        "SOURCE_MAX_HEIGHT_SHORT": short_height,
        "X264_PRESET": preset,
    }


def recommended_profile(profile: SystemProfile) -> dict[str, str]:
    """Choose CPU/RAM-safe defaults while keeping the profile name adaptive."""
    ram = _ram_gb(profile)
    threads = profile.cpu_threads
    if ram and ram < 4.0 or threads <= 2:
        quality, model, workers, preset = "low", "tiny", min(2, threads), "veryfast"
    elif ram and ram < 8.0 or threads <= 4:
        quality, model, workers, preset = "high", "tiny", min(4, threads), "medium"
    elif ram >= 16.0 and threads >= 8:
        quality, model, workers, preset = "highest", "small", min(8, threads), "medium"
    else:
        quality, model, workers, preset = "highest", "base", min(8, threads), "medium"

    values = {
        "HOOKY_PROFILE": "adaptive",
        "WHISPER_MODEL": model,
        # Hooky currently decodes Whisper on CPU; int8 is safe across platforms.
        "WHISPER_COMPUTE": "int8",
        "WHISPER_THREADS": str(workers),
        "FFMPEG_THREADS": str(workers),
    }
    values.update(quality_defaults(quality))
    # Keep the machine-specific preset selected above for adaptive mode.
    values["X264_PRESET"] = preset
    return values


def profile_defaults(profile: SystemProfile, name: str) -> dict[str, str]:
    """Return the selected profile's render and speech settings."""
    adaptive = recommended_profile(profile)
    if name == "adaptive":
        return adaptive
    if name == "low":
        values = dict(adaptive)
        values.update(quality_defaults("low"))
        values.update({
            "HOOKY_PROFILE": "low", "QUALITY": "low", "WHISPER_MODEL": "tiny",
            "WHISPER_COMPUTE": "int8", "WHISPER_THREADS": str(min(2, profile.cpu_threads)),
            "FFMPEG_THREADS": str(min(2, profile.cpu_threads)),
        })
        return values
    if name == "balanced":
        workers = min(4, profile.cpu_threads)
        values = dict(adaptive)
        values.update(quality_defaults("high"))
        values.update({
            "HOOKY_PROFILE": "balanced", "QUALITY": "high", "WHISPER_MODEL": "tiny",
            "WHISPER_COMPUTE": "int8", "WHISPER_THREADS": str(workers),
            "FFMPEG_THREADS": str(workers),
        })
        return values
    if name == "quality":
        workers = min(8, profile.cpu_threads)
        model = "small" if (_ram_gb(profile) >= 16 and profile.cpu_threads >= 8) else "base"
        values = dict(adaptive)
        values.update(quality_defaults("highest"))
        values.update({
            "HOOKY_PROFILE": "quality", "QUALITY": "highest", "WHISPER_MODEL": model,
            "WHISPER_COMPUTE": "int8", "WHISPER_THREADS": str(workers),
            "FFMPEG_THREADS": str(workers),
        })
        return values
    raise ValueError(f"Unknown Hooky profile: {name}")


def default_settings(project_root: Path, profile: SystemProfile) -> dict[str, str]:
    """Build complete first-run values before user-specific answers are added."""
    values = profile_defaults(profile, "adaptive")
    values.update({
        "HOOKY_SETUP_COMPLETE": "1",
        "HOOKY_SETUP_VERSION": "1",
        "GEMINI_API_KEY": "",
        "GEMINI_MODEL": "gemini-3.8-flash",
        "GEMINI_MODEL_FALLBACKS": "gemini-3.1-flash-lite,gemini-flash-latest",
        "GEMINI_MIN_INTERVAL_SECONDS": "20",
        "GEMINI_SELECTION_CHUNK_CHARS": "18000",
        "YTDLP_COOKIES_FROM_BROWSER": "",
        "YTDLP_COOKIES_FILE": "",
        "SUBTITLE_SOURCE": "auto",
        "SUB_STYLE": "hormozi",
        "SUB_LANG": "",
        "OUTPUT_DIR": str(project_root / "output"),
        "LINKS_FILE": str(project_root / "links.txt"),
        "CHANNELS_FILE": str(project_root / "channels.txt"),
        "STATE_DIR": str(project_root / ".hooky-state"),
        "AUTO_CLIP_MODE": "content",
        "AUTO_MAX_CLIPS": "40",
        "AUTO_CANDIDATES_PER_CHUNK": "10",
        "AUTO_CLIP_DENSITY_MINUTES": "10",
        "PARTS_TARGET_SECONDS": "60",
        "PARTS_BACKGROUND": "blur",
        "POST_HOURS": "17-0",
        "QUEUE_MIN_GAP_MINUTES": "60",
        "QUEUE_MAX_GAP_MINUTES": "120",
        "QUEUE_MAX_ATTEMPTS": "3",
        "QUEUE_MAX_PENDING": "12",
        "MAKE_MAX_SOURCES": "2",
        "CHANNEL_BACKLOG_MAX": "20",
        "CHANNEL_SCAN_MAX": "0",
        "MAKE_COVERS": "0",
        "AUDIO_NORMALIZATION": "1",
        # Open-source defaults are conservative: never delete user media
        # unless the user explicitly enables cleanup during setup.
        "DELETE_SOURCE_AFTER_CLIP": "0",
        "DELETE_POSTED_CLIPS": "0",
        "IG_USERNAME": "",
        "IG_PASSWORD": "",
        "IG_2FA_CODE": "",
        "IG_SESSION_FILE": str(project_root / ".ig_session"),
    })
    return values


def bool_value(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def env_bool(value: bool) -> str:
    return "1" if value else "0"


def path_value(value: str, project_root: Path) -> str:
    """Expand user paths and make wizard-created paths stable across cwd changes."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = project_root / path
    return str(path)


__all__ = [
    "MANAGED_KEYS", "PROFILE_NAMES", "QUALITY_NAMES", "SECRET_KEYS",
    "SUBTITLE_SOURCES", "SUBTITLE_STYLES", "WHISPER_COMPUTE_TYPES",
    "WHISPER_MODELS", "YOUTUBE_BROWSERS", "bool_value", "default_settings",
    "env_bool", "path_value", "profile_defaults", "quality_defaults",
    "recommended_profile",
]

