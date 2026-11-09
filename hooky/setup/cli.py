"""Interactive and non-interactive first-run configuration for Hooky."""
from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from .. import config
from .profiler import SystemProfile, collect_system_profile
from .prompts import PromptSession, SetupInteractionError
from .settings import (
    MANAGED_KEYS,
    PROFILE_NAMES,
    QUALITY_NAMES,
    SUBTITLE_SOURCES,
    SUBTITLE_STYLES,
    WHISPER_COMPUTE_TYPES,
    WHISPER_MODELS,
    YOUTUBE_BROWSERS,
    bool_value,
    default_settings,
    env_bool,
    path_value,
    profile_defaults,
    quality_defaults,
)
from .storage import (
    SetupStorageError,
    initialize_source_files,
    read_env,
    settings_for_display,
    validate_cookie_file_metadata,
    write_env,
)


class SetupValidationError(ValueError):
    """User-provided setup data is not valid or locally usable."""


def build_parser(prog: str = "hooky setup") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Configure Hooky locally using a hardware-aware recommended profile.",
    )
    parser.add_argument("--redo", action="store_true", help="run the wizard again")
    parser.add_argument(
        "--advanced", action="store_true",
        help="show every tuning option instead of using adaptive defaults",
    )
    parser.add_argument("--show", action="store_true", help="show saved settings with secrets hidden")
    parser.add_argument(
        "--non-interactive", "--yes", dest="non_interactive", action="store_true",
        help="save from HOOKY_SETUP_* environment values and/or --set overrides",
    )
    parser.add_argument(
        "--profile", choices=PROFILE_NAMES, default=None,
        help="profile for non-interactive setup (default: adaptive)",
    )
    parser.add_argument(
        "--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE",
        help="set a configuration value without prompting; may be repeated",
    )
    return parser


def _existing_values(path: Path) -> dict[str, str]:
    values = read_env(path)
    # The process environment intentionally wins over the file. This makes
    # `HOOKY_SETUP_*` and ordinary CI-provided credentials useful without printing
    # them or copying unrelated environment variables into .env.
    for key in MANAGED_KEYS:
        if key in os.environ:
            values[key] = os.environ[key]
    return values


def _parse_overrides(items: Sequence[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in items:
        key, separator, value = item.partition("=")
        key = key.strip().upper()
        if not separator or key not in MANAGED_KEYS:
            raise SetupValidationError(
                "--set accepts known Hooky settings in KEY=VALUE form"
            )
        values[key] = value
    return values


def _setup_environment_overrides() -> dict[str, str]:
    values: dict[str, str] = {}
    for key in MANAGED_KEYS:
        name = f"HOOKY_SETUP_{key}"
        if name in os.environ:
            values[key] = os.environ[name]
    # Friendly aliases keep shell/CI configuration readable.
    auth = os.environ.get("HOOKY_SETUP_YOUTUBE_AUTH", "").strip().lower()
    if auth:
        if auth == "anonymous":
            values["YTDLP_COOKIES_FROM_BROWSER"] = ""
            values["YTDLP_COOKIES_FILE"] = ""
        elif auth == "browser":
            browser = os.environ.get("HOOKY_SETUP_YOUTUBE_BROWSER", "firefox").strip()
            profile = os.environ.get("HOOKY_SETUP_YOUTUBE_PROFILE", "").strip()
            values["YTDLP_COOKIES_FROM_BROWSER"] = f"{browser}:{profile}" if profile else browser
            values["YTDLP_COOKIES_FILE"] = ""
        elif auth == "file":
            values["YTDLP_COOKIES_FILE"] = os.environ.get("HOOKY_SETUP_YOUTUBE_COOKIE_FILE", "")
            values["YTDLP_COOKIES_FROM_BROWSER"] = ""
        else:
            raise SetupValidationError("HOOKY_SETUP_YOUTUBE_AUTH must be anonymous, browser, or file")
    return values


def _normalise_bool_settings(values: dict[str, str]) -> None:
    for key in ("DELETE_SOURCE_AFTER_CLIP", "DELETE_POSTED_CLIPS"):
        values[key] = env_bool(bool_value(values.get(key), False))


def _validate_values(values: dict[str, str]) -> None:
    from ..dotenv import validate_settings
    try:
        validate_settings(values)
    except ValueError as exc:
        raise SetupValidationError(str(exc)) from exc
    if values.get("HOOKY_PROFILE", "adaptive") not in PROFILE_NAMES:
        raise SetupValidationError("HOOKY_PROFILE must be adaptive, balanced, quality, or low")
    if values.get("QUALITY", "highest") not in {"highest", "high", "low"}:
        raise SetupValidationError("QUALITY must be highest, high, or low")
    if values.get("WHISPER_MODEL", "tiny") not in WHISPER_MODELS:
        raise SetupValidationError("WHISPER_MODEL must be tiny, base, or small")
    if values.get("WHISPER_COMPUTE", "int8") not in WHISPER_COMPUTE_TYPES:
        raise SetupValidationError("WHISPER_COMPUTE must be int8, float16, or float32")
    if values.get("SUBTITLE_SOURCE", "auto") not in SUBTITLE_SOURCES:
        raise SetupValidationError("SUBTITLE_SOURCE must be auto, source, or whisper")
    if values.get("SUB_STYLE", "hormozi") not in SUBTITLE_STYLES:
        raise SetupValidationError("SUB_STYLE must be a supported subtitle style")
    for key in ("WHISPER_THREADS", "FFMPEG_THREADS"):
        try:
            if int(values.get(key, "0")) < 1:
                raise ValueError
        except ValueError as exc:
            raise SetupValidationError(f"{key} must be a positive integer") from exc

    browser = values.get("YTDLP_COOKIES_FROM_BROWSER", "").strip()
    cookie_file = values.get("YTDLP_COOKIES_FILE", "").strip()
    if browser and cookie_file:
        raise SetupValidationError("Choose browser cookies or a cookie file, not both")
    if browser:
        browser_name = browser.split("::", 1)[0].split(":", 1)[0].split("+", 1)[0].lower()
        if browser_name not in YOUTUBE_BROWSERS:
            raise SetupValidationError("YTDLP_COOKIES_FROM_BROWSER has an unsupported browser")
    if cookie_file:
        valid, reason = validate_cookie_file_metadata(cookie_file)
        if not valid:
            raise SetupValidationError(f"YTDLP_COOKIES_FILE metadata check failed: {reason}")
    # Reuse the downloader's syntax validator. It checks browser metadata and
    # cookie-file metadata only; it never opens a browser database or cookie
    # file contents.
    from ..core.downloader import youtube_auth_status

    auth_status = youtube_auth_status({
        "YTDLP_COOKIES_FROM_BROWSER": browser,
        "YTDLP_COOKIES_FILE": cookie_file,
    })
    if not auth_status["ok"]:
        raise SetupValidationError(str(auth_status["message"]))


def _print_system_profile(profile: SystemProfile) -> None:
    ram = f"{profile.ram_gb:.1f} GiB" if profile.ram_gb is not None else "unknown"
    ffmpeg = "available" if profile.ffmpeg.available else "not found"
    ffprobe = "available" if profile.ffprobe.available else "not found"
    gpu = "; ".join(profile.gpu_hints) if profile.gpu_hints else "none detected"
    accel = ", ".join(profile.hardware_acceleration) if profile.hardware_acceleration else "none reported"
    gpu_recommendation = (
        "Keep the CPU renderer (GPU acceleration is detected but not enabled by Hooky's current encoder)."
        if profile.hardware_acceleration else
        "Use the CPU renderer; no usable FFmpeg hardware acceleration was detected."
    )
    print("System profile:")
    print(f"  OS: {profile.distro} ({profile.os_name} {profile.release}, {profile.machine})")
    print(f"  CPU threads: {profile.cpu_threads}")
    print(f"  RAM: {ram}")
    print(f"  GPU hints: {gpu}")
    print(f"  FFmpeg: {ffmpeg}; FFprobe: {ffprobe}")
    print(f"  Hardware acceleration: {accel}")
    print(f"  GPU recommendation: {gpu_recommendation}")


def _print_settings(values: dict[str, str]) -> None:
    print("\nExact settings to save (credential values are hidden):")
    for key, value in settings_for_display(values):
        print(f"  {key}={value}")


def _current_auth(values: dict[str, str]) -> str:
    if values.get("YTDLP_COOKIES_FROM_BROWSER", "").strip():
        return "browser"
    if values.get("YTDLP_COOKIES_FILE", "").strip():
        return "file"
    return "anonymous"


def _interactive_values(existing: dict[str, str], profile: SystemProfile) -> dict[str, str]:
    """Run the short first-run wizard; adaptive defaults cover the rest."""
    values = default_settings(config.PROJECT_ROOT, profile)
    values.update(existing)
    prompts = PromptSession()

    values["GEMINI_API_KEY"] = prompts.secret(
        "Gemini API key (required; hidden)", values.get("GEMINI_API_KEY", ""))

    existing_browser_value = values.get("YTDLP_COOKIES_FROM_BROWSER", "")
    existing_cookie_file = values.get("YTDLP_COOKIES_FILE", "")
    auth = prompts.choice("YouTube authentication", ("anonymous", "browser", "file"),
                          _current_auth(values))
    values["YTDLP_COOKIES_FROM_BROWSER"] = ""
    values["YTDLP_COOKIES_FILE"] = ""
    if auth == "browser":
        current_browser = existing_browser_value.split("+", 1)[0].split(":", 1)[0] \
            if existing_browser_value else "firefox"
        browser = prompts.choice("Browser", YOUTUBE_BROWSERS, current_browser.lower())
        current_profile = ""
        if ":" in existing_browser_value:
            current_profile = existing_browser_value.split(":", 1)[1].split("::", 1)[0]
        browser_profile = prompts.text("Browser profile (optional)", current_profile)
        values["YTDLP_COOKIES_FROM_BROWSER"] = (
            f"{browser}:{browser_profile}" if browser_profile else browser
        )
    elif auth == "file":
        while True:
            candidate = prompts.text("Netscape/Mozilla cookie file path", existing_cookie_file)
            candidate = path_value(candidate, config.PROJECT_ROOT)
            valid, reason = validate_cookie_file_metadata(candidate)
            if valid:
                values["YTDLP_COOKIES_FILE"] = candidate
                break
            print(f"Cookie file metadata check failed: {reason}. The file contents were not read.")
            existing_cookie_file = ""

    configure_instagram = prompts.boolean(
        "Configure optional Instagram credentials",
        bool(values.get("IG_USERNAME") and values.get("IG_PASSWORD")),
    )
    if configure_instagram:
        values["IG_USERNAME"] = prompts.text("Instagram username", values.get("IG_USERNAME", ""))
        values["IG_PASSWORD"] = prompts.secret(
            "Instagram password (hidden)", values.get("IG_PASSWORD", ""))
        values["IG_2FA_CODE"] = prompts.secret(
            "Instagram 2FA code (optional, hidden)", values.get("IG_2FA_CODE", ""))
    else:
        values["IG_USERNAME"] = values["IG_PASSWORD"] = values["IG_2FA_CODE"] = ""

    values["HOOKY_SETUP_COMPLETE"] = "1"
    values["HOOKY_SETUP_VERSION"] = "1"
    return values


def _interactive_advanced_values(
    existing: dict[str, str], profile: SystemProfile,
) -> dict[str, str]:
    values = default_settings(config.PROJECT_ROOT, profile)
    values.update(existing)
    prompts = PromptSession()

    adaptive = profile_defaults(profile, "adaptive")
    profile_name = prompts.choice(
        "Processing profile (adaptive is recommended)", PROFILE_NAMES,
        values.get("HOOKY_PROFILE", "adaptive") if values.get("HOOKY_PROFILE") in PROFILE_NAMES else "adaptive",
    )
    # Profile selection supplies the derived quality, thread, and Whisper values.
    values.update(profile_defaults(profile, profile_name))
    quality = prompts.choice("Quality tier", QUALITY_NAMES, values.get("QUALITY", "highest"))
    values.update(quality_defaults(quality))
    values["HOOKY_PROFILE"] = profile_name

    values["GEMINI_API_KEY"] = prompts.secret(
        "Gemini API key (optional; hidden)", values.get("GEMINI_API_KEY", ""))
    values["GEMINI_MODEL"] = prompts.text("Gemini model", values.get("GEMINI_MODEL", "gemini-3.8-flash"))
    values["GEMINI_MODEL_FALLBACKS"] = prompts.text(
        "Gemini fallback models (comma-separated)", values.get("GEMINI_MODEL_FALLBACKS", ""))
    values["GEMINI_MIN_INTERVAL_SECONDS"] = prompts.text(
        "Gemini request spacing in seconds", values.get("GEMINI_MIN_INTERVAL_SECONDS", "20"))
    values["GEMINI_SELECTION_CHUNK_CHARS"] = prompts.text(
        "Gemini transcript chunk characters", values.get("GEMINI_SELECTION_CHUNK_CHARS", "18000"))

    existing_browser_value = values.get("YTDLP_COOKIES_FROM_BROWSER", "")
    existing_cookie_file = values.get("YTDLP_COOKIES_FILE", "")
    auth = prompts.choice("YouTube authentication", ("anonymous", "browser", "file"), _current_auth(values))
    values["YTDLP_COOKIES_FROM_BROWSER"] = ""
    values["YTDLP_COOKIES_FILE"] = ""
    if auth == "browser":
        browser_value = existing_browser_value
        current_browser = browser_value.split("+", 1)[0].split(":", 1)[0] if browser_value else "firefox"
        browser = prompts.choice("Browser", YOUTUBE_BROWSERS, current_browser.lower())
        current_profile = ""
        if ":" in browser_value:
            current_profile = browser_value.split(":", 1)[1].split("::", 1)[0]
        browser_profile = prompts.text("Browser profile (optional)", current_profile)
        values["YTDLP_COOKIES_FROM_BROWSER"] = f"{browser}:{browser_profile}" if browser_profile else browser
    elif auth == "file":
        while True:
            candidate = prompts.text("Netscape/Mozilla cookie file path", existing_cookie_file)
            candidate = path_value(candidate, config.PROJECT_ROOT)
            valid, reason = validate_cookie_file_metadata(candidate)
            if valid:
                values["YTDLP_COOKIES_FILE"] = candidate
                break
            print(f"Cookie file metadata check failed: {reason}. The file contents were not read.")
            values["YTDLP_COOKIES_FILE"] = ""

    values["WHISPER_MODEL"] = prompts.choice(
        "Whisper model", WHISPER_MODELS, values.get("WHISPER_MODEL", adaptive["WHISPER_MODEL"]))
    values["WHISPER_COMPUTE"] = prompts.choice(
        "Whisper compute type", WHISPER_COMPUTE_TYPES,
        values.get("WHISPER_COMPUTE", adaptive["WHISPER_COMPUTE"]))
    values["SUBTITLE_SOURCE"] = prompts.choice(
        "Subtitle source", SUBTITLE_SOURCES, values.get("SUBTITLE_SOURCE", "auto"))
    values["SUB_STYLE"] = prompts.choice(
        "Subtitle style", SUBTITLE_STYLES, values.get("SUB_STYLE", "hormozi"))
    values["SUB_LANG"] = prompts.text(
        "Whisper language (blank for auto)", values.get("SUB_LANG", ""))

    values["AUTO_CLIP_MODE"] = prompts.choice(
        "Automatic clip count mode", ("content", "duration"),
        values.get("AUTO_CLIP_MODE", "content"))
    values["AUTO_MAX_CLIPS"] = prompts.text(
        "Maximum automatic clips", values.get("AUTO_MAX_CLIPS", "40"))
    values["AUTO_CANDIDATES_PER_CHUNK"] = prompts.text(
        "Candidates per transcript chunk", values.get("AUTO_CANDIDATES_PER_CHUNK", "10"))
    values["AUTO_CLIP_DENSITY_MINUTES"] = prompts.text(
        "Duration-mode clip density in minutes", values.get("AUTO_CLIP_DENSITY_MINUTES", "10"))
    values["PARTS_TARGET_SECONDS"] = prompts.text(
        "Parts target seconds", values.get("PARTS_TARGET_SECONDS", "60"))
    values["PARTS_BACKGROUND"] = prompts.choice(
        "Parts background", ("blur", "plain"), values.get("PARTS_BACKGROUND", "blur"))

    values["OUTPUT_DIR"] = path_value(
        prompts.text("Output directory", values.get("OUTPUT_DIR", str(config.PROJECT_ROOT / "output"))),
        config.PROJECT_ROOT)
    values["LINKS_FILE"] = path_value(
        prompts.text("Links file", values.get("LINKS_FILE", str(config.PROJECT_ROOT / "links.txt"))),
        config.PROJECT_ROOT)
    values["CHANNELS_FILE"] = path_value(
        prompts.text("Channels file", values.get("CHANNELS_FILE", str(config.PROJECT_ROOT / "channels.txt"))),
        config.PROJECT_ROOT)
    values["STATE_DIR"] = path_value(
        prompts.text("State directory", values.get("STATE_DIR", str(config.PROJECT_ROOT / ".hooky-state"))),
        config.PROJECT_ROOT)

    values["POST_HOURS"] = prompts.text("Posting hours (for example 17-0)", values.get("POST_HOURS", "17-0"))
    values["QUEUE_MIN_GAP_MINUTES"] = prompts.text(
        "Minimum minutes between posts", values.get("QUEUE_MIN_GAP_MINUTES", "60"))
    values["QUEUE_MAX_GAP_MINUTES"] = prompts.text(
        "Maximum minutes between posts", values.get("QUEUE_MAX_GAP_MINUTES", "120"))
    values["QUEUE_MAX_ATTEMPTS"] = prompts.text(
        "Maximum post attempts", values.get("QUEUE_MAX_ATTEMPTS", "3"))
    values["QUEUE_MAX_PENDING"] = prompts.text(
        "Maximum pending queue clips", values.get("QUEUE_MAX_PENDING", "12"))
    values["MAKE_MAX_SOURCES"] = prompts.text(
        "Maximum sources per make command", values.get("MAKE_MAX_SOURCES", "2"))
    values["CHANNEL_BACKLOG_MAX"] = prompts.text(
        "Maximum discovered backlog", values.get("CHANNEL_BACKLOG_MAX", "20"))
    values["CHANNEL_SCAN_MAX"] = prompts.text(
        "Channel scan maximum (0 means all)", values.get("CHANNEL_SCAN_MAX", "0"))
    values["MAKE_COVERS"] = env_bool(prompts.boolean(
        "Generate cover images", bool_value(values.get("MAKE_COVERS"), False)))
    values["AUDIO_NORMALIZATION"] = env_bool(prompts.boolean(
        "Normalize audio", bool_value(values.get("AUDIO_NORMALIZATION"), True)))

    values["DELETE_SOURCE_AFTER_CLIP"] = env_bool(
        prompts.boolean("Delete downloaded source after successful clipping", bool_value(
            values.get("DELETE_SOURCE_AFTER_CLIP"), False)))
    values["DELETE_POSTED_CLIPS"] = env_bool(
        prompts.boolean("Delete local clip files after confirmed Instagram posting", bool_value(
            values.get("DELETE_POSTED_CLIPS"), False)))

    configure_instagram = prompts.boolean(
        "Configure optional Instagram credentials", bool(values.get("IG_USERNAME") and values.get("IG_PASSWORD")))
    if configure_instagram:
        values["IG_USERNAME"] = prompts.text("Instagram username", values.get("IG_USERNAME", ""))
        values["IG_PASSWORD"] = prompts.secret("Instagram password (hidden)", values.get("IG_PASSWORD", ""))
        values["IG_2FA_CODE"] = prompts.secret(
            "Instagram 2FA code (optional, hidden)", values.get("IG_2FA_CODE", ""))
    else:
        values["IG_USERNAME"] = values["IG_PASSWORD"] = values["IG_2FA_CODE"] = ""
    values["HOOKY_SETUP_COMPLETE"] = "1"
    values["HOOKY_SETUP_VERSION"] = "1"
    return values


def _noninteractive_values(
    existing: dict[str, str], profile: SystemProfile, args: argparse.Namespace,
) -> dict[str, str]:
    values = default_settings(config.PROJECT_ROOT, profile)
    values.update(existing)
    overrides = _setup_environment_overrides()
    overrides.update(_parse_overrides(args.overrides))
    if args.profile:
        overrides["HOOKY_PROFILE"] = args.profile

    profile_name = overrides.get("HOOKY_PROFILE", values.get("HOOKY_PROFILE", "adaptive")) or "adaptive"
    values.update(profile_defaults(profile, profile_name))
    values.update(overrides)
    if "QUALITY" in overrides:
        derived_quality = quality_defaults(values["QUALITY"])
        for key, value in derived_quality.items():
            if key not in overrides:
                values[key] = value
    for key in ("OUTPUT_DIR", "LINKS_FILE", "CHANNELS_FILE", "IG_SESSION_FILE"):
        if values.get(key):
            values[key] = path_value(values[key], config.PROJECT_ROOT)
    # A non-interactive run must not accidentally retain both auth mechanisms.
    if "YTDLP_COOKIES_FROM_BROWSER" in overrides and "YTDLP_COOKIES_FILE" not in overrides:
        values["YTDLP_COOKIES_FILE"] = ""
    if "YTDLP_COOKIES_FILE" in overrides and "YTDLP_COOKIES_FROM_BROWSER" not in overrides:
        values["YTDLP_COOKIES_FROM_BROWSER"] = ""
    values["HOOKY_SETUP_COMPLETE"] = "1"
    values["HOOKY_SETUP_VERSION"] = "1"
    _normalise_bool_settings(values)
    _validate_values(values)
    return values


def _show(path: Path) -> int:
    values = read_env(path)
    if not values.get("HOOKY_SETUP_COMPLETE"):
        print("Hooky has no completed local setup. Run `Hooky setup` to configure it.")
        return 0
    print(f"Saved Hooky settings: {path}")
    _print_settings(values)
    return 0


def main(argv: Sequence[str] | None = None, *, prog: str = "hooky setup") -> int:
    parser = build_parser(prog)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.show and (args.redo or args.advanced or args.non_interactive
                      or args.overrides or args.profile):
        parser.error("--show cannot be combined with setup-writing options")

    path = Path(config.ENV_FILE)
    try:
        existing = _existing_values(path)
        if args.show:
            return _show(path)
        if existing.get("HOOKY_SETUP_COMPLETE") == "1" and not (
            args.redo or args.advanced or args.non_interactive
            or args.overrides or args.profile
        ):
            initialize_source_files(existing)
            print("Hooky is already configured. Use `Hooky setup --redo` to run setup again.")
            return _show(path)

        profile = collect_system_profile()
        _print_system_profile(profile)
        if args.non_interactive or args.overrides or args.profile:
            values = _noninteractive_values(existing, profile, args)
        else:
            values = (
                _interactive_advanced_values(existing, profile)
                if args.advanced else _interactive_values(existing, profile)
            )
            _normalise_bool_settings(values)
            _validate_values(values)

        _print_settings(values)
        if not args.non_interactive and not args.overrides and not args.profile:
            answer = PromptSession().boolean("Save these settings", True)
            if not answer:
                print("Setup cancelled; no configuration was saved.")
                return 1
        saved = write_env({**existing, **values}, path)
        initialize_source_files(values)
        print(f"Saved Hooky configuration to {saved} (owner-only permissions requested).")
        return 0
    except (SetupStorageError, SetupValidationError, SetupInteractionError) as exc:
        print(f"[hooky setup] {exc}", file=sys.stderr)
        return 2


__all__ = ["build_parser", "main"]

