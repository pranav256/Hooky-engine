"""One non-executing dotenv grammar for startup, setup, and Studio settings."""
from __future__ import annotations

import math
import re

KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SECRET_KEYS = {"GEMINI_API_KEY", "IG_PASSWORD", "IG_2FA_CODE", "HOOKY_WEB_TOKEN"}
MASK = "••••••••"


def parse_value(raw: str) -> str:
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return ""
    if raw[0] in "\"'":
        quote = raw[0]
        value = []
        index = 1
        while index < len(raw):
            char = raw[index]
            if char == quote:
                tail = raw[index + 1:].strip()
                if tail and not tail.startswith("#"):
                    raise ValueError("Unexpected text after a quoted value.")
                return "".join(value)
            if quote == '"' and char == "\\" and index + 1 < len(raw) and raw[index + 1] in '\\"':
                index += 1
                char = raw[index]
            value.append(char)
            index += 1
        raise ValueError("Unclosed quoted value.")
    return re.split(r"\s+#", raw, maxsplit=1)[0].rstrip()


def parse_dotenv(raw: str) -> dict[str, str]:
    if raw.startswith("\ufeff"):
        raw = raw[1:]
    if "\0" in raw:
        raise ValueError("Settings cannot contain null characters.")
    values = {}
    for number, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not KEY.fullmatch(key):
            raise ValueError(f"Invalid setting on line {number}; use KEY=value.")
        try:
            values[key] = parse_value(value)
        except ValueError as exc:
            raise ValueError(f"Invalid setting on line {number}: {exc}") from exc
    return values


def quote_value(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:@+,-]*", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def dump_dotenv(values: dict[str, str]) -> str:
    return "".join(f"{key}={quote_value(value)}\n" for key, value in sorted(values.items()))


def validate_settings(values: dict[str, str]) -> None:
    for key, value in values.items():
        if not isinstance(key, str) or not KEY.fullmatch(key):
            raise ValueError("Settings must use valid variable names.")
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string.")
        if any(char in value for char in "\r\n\0"):
            raise ValueError(f"{key} must be a single-line value.")
    choices = {
        "QUALITY": {"highest", "maximum", "max", "high", "low", "fast"},
        "SUB_STYLE": {"hormozi", "clean", "yellow", "gold", "titanium", "minimal", "emerald", "crimson"},
        "SUBTITLE_SOURCE": {"auto", "source", "whisper"},
        "AUTO_CLIP_MODE": {"content", "duration"},
        "WHISPER_COMPUTE": {"int8", "float16", "float32"},
        "PARTS_MODE": {"story", "number-only"},
        "PARTS_BACKGROUND": {"blur", "plain"},
        "X264_PRESET": {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow", "placebo"},
    }
    for key, options in choices.items():
        if key not in values:
            continue
        value = values[key].strip().lower()
        if not value and key not in {"PARTS_MODE", "PARTS_BACKGROUND", "X264_PRESET"}:
            continue
        if value not in options:
            raise ValueError(f"{key} must be one of: {', '.join(sorted(options))}.")
    integer_minimums = {
        "FFMPEG_THREADS": 1, "WHISPER_THREADS": 1, "CHANNEL_BACKLOG_MAX": 0,
        "CHANNEL_SCAN_MAX": 0, "QUEUE_MAX_ATTEMPTS": 1, "ANALYSIS_MAX_FRAMES": 4,
        "GEMINI_SELECTION_CHUNK_CHARS": 4000, "SOURCE_MAX_HEIGHT": 1,
        "SOURCE_MAX_HEIGHT_LONG": 1, "SOURCE_MAX_HEIGHT_SHORT": 1,
        "MAKE_MAX_SOURCES": 1, "QUEUE_MAX_PENDING": 1,
        "AUTO_MAX_CLIPS": 1, "AUTO_CANDIDATES_PER_CHUNK": 1,
        "SUB_FONT_SIZE": 0, "SUB_MAX_WORDS": 0,
    }
    optional_integers = {"SOURCE_MAX_HEIGHT", "AUTO_MAX_CLIPS", "AUTO_CANDIDATES_PER_CHUNK", "SUB_FONT_SIZE", "SUB_MAX_WORDS"}
    for key, minimum in integer_minimums.items():
        if key in values and (values[key].strip() or key not in optional_integers):
            try:
                if int(values[key]) < minimum:
                    raise ValueError
            except ValueError:
                raise ValueError(f"{key} must be an integer >= {minimum}.") from None
    for key in ("GEMINI_MIN_INTERVAL_SECONDS", "SOURCE_LONG_THRESHOLD_MINUTES", "QUEUE_MIN_GAP_MINUTES", "QUEUE_MAX_GAP_MINUTES", "CHANNEL_CACHE_HOURS"):
        if key in values:
            try:
                value = float(values[key])
                if not math.isfinite(value) or value < 0:
                    raise ValueError
            except ValueError:
                raise ValueError(f"{key} must be a finite nonnegative number.") from None
    if values.get("AUTO_CLIP_DENSITY_MINUTES", "").strip():
        try:
            density = float(values["AUTO_CLIP_DENSITY_MINUTES"])
            if not math.isfinite(density) or density <= 0:
                raise ValueError
        except ValueError:
            raise ValueError("AUTO_CLIP_DENSITY_MINUTES must be a finite positive number.") from None
    if "PARTS_TARGET_SECONDS" in values:
        try:
            duration = float(values["PARTS_TARGET_SECONDS"])
            if not math.isfinite(duration) or not 1 <= duration <= 90:
                raise ValueError
        except ValueError:
            raise ValueError("PARTS_TARGET_SECONDS must be a finite number from 1 to 90.") from None
    for key in ("MAKE_COVERS", "AUDIO_NORMALIZATION", "DELETE_SOURCE_AFTER_CLIP", "DELETE_POSTED_CLIPS", "DESKTOP_NOTIFICATIONS"):
        if key in values and values[key].strip().lower() not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
            raise ValueError(f"{key} must be true or false.")
    if values.get("YTDLP_COOKIES_FILE") and values.get("YTDLP_COOKIES_FROM_BROWSER"):
        raise ValueError("Choose browser cookies or a cookie file, not both.")
