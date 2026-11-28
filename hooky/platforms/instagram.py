"""Instagram Reel upload via instagrapi."""
from __future__ import annotations

import re
import sys
from pathlib import Path

from ..config import IG_2FA_CODE, IG_PASSWORD, IG_USERNAME, SESSION_FILE


class UploadUncertainError(RuntimeError):
    """The request may have reached Instagram but returned no confirmation."""


class UploadNotStartedError(RuntimeError):
    """A local prerequisite failed before clip_upload could be called."""


def preflight(video_path: Path, client) -> None:
    """Check local prerequisites without sending anything to Instagram."""
    if client is None or getattr(client, "user_id", True) in (None, 0, ""):
        raise UploadNotStartedError("Instagram client is not logged in")
    try:
        if not Path(video_path).is_file() or Path(video_path).stat().st_size <= 0:
            raise UploadNotStartedError(f"Missing or empty clip: {video_path}")
        with Path(video_path).open("rb"):
            pass
    except OSError as exc:
        raise UploadNotStartedError(f"Cannot read clip: {video_path}") from exc


def is_configured() -> bool:
    return bool(IG_USERNAME and IG_PASSWORD)


def not_ready_hint() -> str:
    return "IG_USERNAME/IG_PASSWORD missing in .env"


def login(client=None, interactive_2fa: bool = True):
    """Log in once and reuse the cached default Instagram session."""
    try:
        from instagrapi import Client
    except ImportError:
        print("[ig] instagrapi not installed. Run: uv sync --locked --extra all",
              file=sys.stderr)
        raise SystemExit(1)

    if client is None:
        client = Client()
    client.delay_range = [1, 3]

    if SESSION_FILE.exists():
        try:
            client.load_settings(SESSION_FILE)
            client.login(IG_USERNAME, IG_PASSWORD)
            client.get_timeline_feed()
            print("[ig] Session restored from cache.")
            return client
        except Exception:
            print("[ig] Cached session invalid, logging in fresh...")

    try:
        client.login(IG_USERNAME, IG_PASSWORD)
    except Exception as exc:
        name = type(exc).__name__.lower()
        if any(token in name for token in ("challenge", "2fa", "code")):
            code = IG_2FA_CODE
            if not code and interactive_2fa:
                try:
                    code = input("[ig] Enter the 6-digit code Instagram sent you: ").strip()
                except EOFError:
                    code = ""
            if code:
                client.login(IG_USERNAME, IG_PASSWORD, verification_code=code)
            else:
                raise
        else:
            raise

    client.dump_settings(SESSION_FILE)
    print("[ig] Logged in and session cached.")
    return client


def upload(video_path: Path, caption: str = "", cover: Path | None = None,
           client=None) -> str:
    """Upload one Reel and return its JSON-safe Instagram media code."""
    preflight(video_path, client)
    kwargs = {"caption": caption}
    if cover:
        try:
            if Path(cover).is_file():
                kwargs["thumbnail"] = str(cover)
        except OSError as exc:
            raise UploadNotStartedError(f"Cannot read cover: {cover}") from exc

    print(f"[ig] Uploading {Path(video_path).name} ...")
    try:
        media = client.clip_upload(str(video_path), **kwargs)
        code = getattr(media, "code", None)
        if (not isinstance(code, str) or code.lower() in {"none", "null"}
                or not re.fullmatch(r"[A-Za-z0-9_-]+", code)):
            raise ValueError("Instagram returned a media object without a valid code")
    except Exception as exc:
        raise UploadUncertainError(
            f"Instagram upload outcome is unknown for {Path(video_path).name}; "
            "check Instagram before retrying") from exc
    print(f"[ig] Uploaded! https://www.instagram.com/reel/{code}/")
    return code

