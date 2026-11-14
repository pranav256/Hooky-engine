from __future__ import annotations

import os
import re
import sys
import json
import secrets
import hmac
import asyncio
import math
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from hooky.config import PROJECT_ROOT, ENV_FILE, DOTENV_KEYS
from hooky import __version__
from hooky.dotenv import parse_dotenv, dump_dotenv, validate_settings, SECRET_KEYS, MASK
from hooky.setup.storage import write_env, SetupStorageError
from hooky.web.jobs import JobManager, prune_cache
from hooky.doctor import dependency_checks
from hooky.desktop import open_path
from hooky.core.subtitles import STYLES
from hooky.core.utils import validate_video_url
from hooky.core.source_text import validate_caption_edits

def _load_or_create_web_token() -> str:
    env_token = os.environ.get("HOOKY_WEB_TOKEN", "").strip()
    if env_token:
        return env_token
    state_dir = Path(os.environ.get("STATE_DIR") or PROJECT_ROOT / ".hooky-state")
    token_file = state_dir / ".web_token"
    try:
        if token_file.exists():
            saved = token_file.read_text(encoding="utf-8").strip()
            if len(saved) >= 16:
                os.environ["HOOKY_WEB_TOKEN"] = saved
                return saved
        state_dir.mkdir(parents=True, exist_ok=True)
        new_token = secrets.token_urlsafe(32)
        token_file.write_text(new_token, encoding="utf-8")
        try:
            token_file.chmod(0o600)
        except OSError:
            pass
        os.environ["HOOKY_WEB_TOKEN"] = new_token
        return new_token
    except Exception:
        new_token = secrets.token_urlsafe(32)
        os.environ["HOOKY_WEB_TOKEN"] = new_token
        return new_token


_WEB_TOKEN = _load_or_create_web_token()


def get_web_token() -> str:
    """Return active secret token required for Web Studio API calls."""
    return _WEB_TOKEN


@asynccontextmanager
async def lifespan(app):
    manager = _job_manager()
    try:
        yield
    finally:
        await manager.shutdown()


app = FastAPI(title="Hooky Web Studio & Control Portal", version=__version__, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    """Enforce loopback Host header to prevent DNS rebinding, and authenticate API calls."""
    # 1. Host header validation
    host_header = request.headers.get("host", "")
    if not re.fullmatch(r"(?:127\.0\.0\.1|localhost|\[::1\]|testserver)(?::\d+)?", host_header, re.IGNORECASE):
        return JSONResponse(
            status_code=403,
            content={"detail": f"Forbidden: Host header '{host_header}' is not allowed"},
        )

    # 2. Token authentication for /api/* routes (exempt CORS OPTIONS preflight)
    if request.url.path.startswith("/api/") and request.method != "OPTIONS":
        token = (
            request.headers.get("x-hooky-token")
            or request.cookies.get("hooky_token")
            or request.query_params.get("token")
        )
        if not token or not hmac.compare_digest(token, _WEB_TOKEN):
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized: Missing or invalid X-Hooky-Token"},
            )

    return await call_next(request)

STATIC_DIR = Path(__file__).parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_JOBS: dict[str, dict[str, Any]] = {}
_MANAGER: JobManager | None = None
_STABLE_SETTINGS = {key: os.environ.get(key, "") for key in ("STATE_DIR", "HOOKY_WEB_TOKEN", "IG_SESSION_FILE")}


def _job_manager() -> JobManager:
    global _MANAGER
    state = Path(_STABLE_SETTINGS["STATE_DIR"] or PROJECT_ROOT / ".hooky-state")
    state = state if state.is_absolute() else PROJECT_ROOT / state
    directory = state / "web_jobs"
    if _MANAGER is None or _MANAGER.directory != directory:
        _JOBS.clear()
        _MANAGER = JobManager(directory, _JOBS)
        roots = {_configured_path("OUTPUT_DIR")} | {Path(job["output_root"]) for job in _MANAGER.jobs.values() if job.get("output_root")}
        for root in roots:
            try:
                prune_cache(root / "web_cache")
            except OSError:
                pass
    return _MANAGER


def _runtime_settings() -> dict[str, str]:
    values = {key: value for key, value in os.environ.items() if key not in DOTENV_KEYS}
    if ENV_FILE.exists():
        try:
            saved = parse_dotenv(ENV_FILE.read_text(encoding="utf-8"))
            validate_settings(saved)
            values.update(saved)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Saved settings need repair: {exc}") from exc
    return values


def _configured_path(key: str) -> Path:
    raw = _runtime_settings().get(key, "").strip()
    if not raw:
        return PROJECT_ROOT / {"OUTPUT_DIR": "output", "CHANNELS_FILE": "channels.txt", "LINKS_FILE": "links.txt"}[key]
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _job_response(job: dict) -> dict:
    manager = _job_manager()
    safe = manager._redact(job, manager.secrets.get(job["id"], ()))
    result = {key: value for key, value in safe.items() if key != "output_root"}
    if job.get("output_file"):
        result["output_url"] = f"/api/outputs/{quote(job['output_file'])}?job_id={job['id']}"
    return result


async def _launch(kind: str, payload: dict) -> dict:
    try:
        environment = _runtime_settings()
        # Preserve test/deployment configured paths even when no file override exists.
        for key in ("OUTPUT_DIR", "CHANNELS_FILE", "LINKS_FILE"):
            environment[key] = str(_configured_path(key))
        job = await _job_manager().start(kind, payload, environment, PROJECT_ROOT)
    except ValueError as exc:
        raise HTTPException(status_code=429 if "Two jobs" in str(exc) else 400, detail=str(exc)) from exc
    return {"job_id": job["id"], "status": "started"}


async def _request_object(request: Request) -> dict[str, Any]:
    body = await request.body()
    if len(body) > 1024 * 1024:
        raise HTTPException(status_code=413, detail="Request body exceeds 1 MiB.")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Request body must be valid JSON.")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
    return data


async def _source_lines(request: Request, field: str) -> list[str]:
    data = await _request_object(request)
    lines = data.get(field)
    if not isinstance(lines, list) or any(
        not isinstance(line, str) or any(char in line for char in "\r\n\0")
        for line in lines
    ):
        raise HTTPException(status_code=400, detail=f"'{field}' must be a list of single-line strings.")
    return [line.strip() for line in lines if line.strip()]


def _write_source_lines(path: Path, lines: list[str]) -> None:
    """Replace a source list only after the complete new file is on disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write("\n".join(lines) + ("\n" if lines else ""))
            output.flush()
            os.fsync(output.fileno())
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


def _video_url(data: dict[str, Any]) -> str:
    url = data.get("url")
    if not isinstance(url, str) or any(char in url for char in "\r\n\0") or not validate_video_url(url.strip()):
        raise HTTPException(status_code=400, detail="Enter a valid http(s) video URL.")
    return url.strip()


def _request_text(data: dict[str, Any], field: str, default: str = "") -> str:
    value = data.get(field, default)
    if not isinstance(value, str) or "\0" in value:
        raise HTTPException(status_code=400, detail=f"'{field}' must be a string without null characters.")
    return value.strip()


def _render_options(data: dict[str, Any]) -> tuple[str, str]:
    style = _request_text(data, "style", (_runtime_settings().get("SUB_STYLE") or "").strip() or "gold").lower()
    framing = _request_text(data, "framing", "center").lower()
    if style not in STYLES:
        raise HTTPException(status_code=400, detail="Unknown subtitle style.")
    if framing not in {"center", "fit"}:
        raise HTTPException(status_code=400, detail="Framing must be 'center' or 'fit'.")
    return style, framing


def _analysis_payload(data: dict) -> dict:
    payload = {"url": _video_url(data)}
    topic = _request_text(data, "topic")
    if len(topic) > 500:
        raise HTTPException(status_code=400, detail="Topic must be at most 500 characters.")
    if topic:
        payload["topic"] = topic
    return payload


@app.get("/api/doctor")
async def get_doctor_status():
    """Return diagnostic checks from Hooky's doctor module."""
    raw_checks = await asyncio.to_thread(dependency_checks, _runtime_settings(), PROJECT_ROOT)
    checks = [
        {"name": c.name, "ok": c.ok, "detail": c.detail, "required": c.required}
        for c in raw_checks
    ]
    gpu_type = "CPU (H.264 / libx264)"

    return {
        "status": "online",
        "gpu": gpu_type,
        "checks": checks,
        "all_required_ok": all(c.ok for c in raw_checks if c.required),
        "subtitle_styles": list(STYLES.keys()),
    }


@app.get("/api/channels")
async def get_channels():
    """Return active lines from channels.txt."""
    path = _configured_path("CHANNELS_FILE")
    if not path.exists():
        return {"channels": [], "count": 0}
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return {"channels": lines, "count": len(lines)}


@app.post("/api/channels")
async def save_channels(request: Request):
    """Save updated channels list to channels.txt."""
    new_channels = await _source_lines(request, "channels")
    _write_source_lines(_configured_path("CHANNELS_FILE"), new_channels)
    return {"status": "saved", "count": len(new_channels)}


@app.get("/api/links")
async def get_links():
    """Return active lines from links.txt."""
    path = _configured_path("LINKS_FILE")
    if not path.exists():
        return {"links": [], "count": 0}
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return {"links": lines, "count": len(lines)}


@app.post("/api/links")
async def save_links(request: Request):
    """Save updated links list to links.txt."""
    new_links = await _source_lines(request, "links")
    _write_source_lines(_configured_path("LINKS_FILE"), new_links)
    return {"status": "saved", "count": len(new_links)}


def _sanitized_settings(values: dict[str, str]) -> dict:
    sanitized = {key: MASK if key in SECRET_KEYS and value else value for key, value in values.items()}
    return {"raw": dump_dotenv(sanitized), "values": sanitized}


def _settings_document(data: dict, current: dict | None = None) -> tuple[dict[str, str], list[str]]:
    existing = current if current is not None else parse_dotenv(ENV_FILE.read_text(encoding="utf-8")) if ENV_FILE.exists() else {}
    if "draft" in data:
        operations = data["draft"]
        if not isinstance(operations, list) or not 1 <= len(operations) <= 100 or any(not isinstance(op, dict) or "draft" in op for op in operations):
            raise ValueError("draft must contain 1 to 100 raw/form edits; save or discard before adding more.")
        if set(data) != {"draft"}:
            raise ValueError("Provide draft edits separately from raw or values.")
        submitted = set()
        values = existing
        for operation in operations:
            values, keys = _settings_document(operation, values)
            submitted.update(keys)
        return values, sorted(submitted)
    if not {"raw", "values"}.intersection(data):
        raise ValueError("Provide raw or values.")
    submitted = {}
    if "raw" in data:
        if not isinstance(data["raw"], str):
            raise ValueError("raw must be a string.")
        base = parse_dotenv(data["raw"])
        submitted.update(base)
    else:
        base = existing.copy()
    prior = {key: base.get(key, existing.get(key, "")) if base.get(key) != MASK else existing.get(key, "") for key in SECRET_KEYS}
    values = base.copy()
    if "values" in data:
        if not isinstance(data["values"], dict):
            raise ValueError("values must be a JSON object.")
        validate_settings(data["values"])
        values.update(data["values"])
        submitted.update(data["values"])
    for key in SECRET_KEYS:
        if values.get(key) == MASK:
            values[key] = prior.get(key, "")
    validate_settings(values)
    return values, sorted(key for key in SECRET_KEYS if key in submitted and submitted[key] != MASK)


@app.get("/api/env")
async def get_env_config():
    try:
        values = parse_dotenv(ENV_FILE.read_text(encoding="utf-8")) if ENV_FILE.exists() else {}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _sanitized_settings(values)


@app.post("/api/env/preview")
async def preview_env_config(request: Request):
    try:
        values, submitted = _settings_document(await _request_object(request))
        return {**_sanitized_settings(values), "submitted_secret_keys": submitted}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/env")
async def save_env_config(request: Request):
    if "application/json" not in request.headers.get("content-type", "").lower():
        raise HTTPException(status_code=415, detail="Content-Type must be application/json")
    try:
        values, submitted = _settings_document(await _request_object(request))
        inherited = {key: os.environ.get(key, "") if key not in DOTENV_KEYS else "" for key in _STABLE_SETTINGS}
        effective = {**inherited, **values}
        restart = [key for key in _STABLE_SETTINGS if effective.get(key, "") != _STABLE_SETTINGS[key]]
        write_env(values, ENV_FILE)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SetupStorageError as exc:
        raise HTTPException(status_code=500, detail="Could not save settings; previous settings were kept. Check file permissions and retry.") from exc
    return {"status": "saved", "message": "Saved for new jobs; running jobs keep their settings.",
            **_sanitized_settings(values), "restart_required": restart, "submitted_secret_keys": submitted}


def _output_roots() -> dict[Path, str | None]:
    roots = {_configured_path("OUTPUT_DIR").resolve(): None}
    for job in _job_manager().jobs.values():
        if job.get("output_root") and job.get("output_file"):
            roots.setdefault(Path(job["output_root"]).resolve(), job["id"])
    return roots


def _public_output(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
        return (path.suffix.lower() == ".mp4" and path.resolve().is_relative_to(root.resolve())
                and not any(part in {"web_cache", "downloads", ".checkpoints", ".cache"} or part.startswith(".") for part in relative.parts)
                and not path.name.startswith("snippet_") and not path.name.endswith(".tmp.mp4")
                and path.is_file() and path.stat().st_size > 1000)
    except (OSError, ValueError):
        return False


@app.get("/api/outputs")
async def list_outputs():
    roots = _output_roots()
    def scan():
        files = []
        folders = {}
        for root, job_id in roots.items():
            for path in root.glob("**/*.mp4"):
                if not _public_output(path, root):
                    continue
                relative = path.relative_to(root).as_posix()
                stat = path.stat()
                folder = path.parent.name if path.parent != root else "Standalone Reels"
                item = {"name": path.name, "folder": folder, "folder_key": folder, "rel_path": relative,
                        "size_mb": round(stat.st_size / (1024 * 1024), 2), "mtime": stat.st_mtime,
                        "url": f"/api/outputs/{quote(relative)}" + (f"?job_id={job_id}" if job_id else "")}
                files.append(item)
                entry = folders.setdefault((root, path.parent), {"name": folder, "folder_key": folder, "files": [], "latest_mtime": 0})
                entry["files"].append(item)
                entry["latest_mtime"] = max(entry["latest_mtime"], stat.st_mtime)
        files.sort(key=lambda item: item["mtime"], reverse=True)
        grouped = sorted(folders.values(), key=lambda item: item["latest_mtime"], reverse=True)
        for folder in grouped:
            folder["count"] = len(folder["files"])
        return {"files": files, "folders": grouped, "count": len(files)}
    return await asyncio.to_thread(scan)


def _output_root(job_id: str | None = None) -> Path:
    if job_id:
        job = _job_manager().jobs.get(job_id)
        if not job or not job.get("output_root"):
            raise HTTPException(status_code=404, detail="Output job not found")
        return Path(job["output_root"]).resolve()
    return _configured_path("OUTPUT_DIR").resolve()


@app.post("/api/outputs/open-folder")
async def open_output_folder(filepath: str | None = None, job_id: str | None = None):
    root = _output_root(job_id)
    if filepath:
        path = root / filepath
        if not _public_output(path, root):
            raise HTTPException(status_code=404, detail="Clip not found")
        folder = path.resolve().parent
    else:
        folder = root
    if not folder.is_dir():
        raise HTTPException(status_code=404, detail="No output folder yet. Export a clip first.")
    if not await asyncio.to_thread(open_path, folder):
        raise HTTPException(status_code=500, detail="Could not open the output folder. Check your desktop file manager.")
    return {"status": "opened"}


@app.get("/api/outputs/download-all")
async def download_all_outputs():
    roots = _output_roots()
    def archive():
        files = [(index, root, path) for index, root in enumerate(roots, 1)
                 for path in root.glob("**/*.mp4") if _public_output(path, root)]
        if not files:
            raise HTTPException(status_code=404, detail="No finished clips to download")
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as temporary:
            destination = Path(temporary.name)
        try:
            # MP4s are already compressed; store them without wasting CPU on recompression.
            with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as bundle:
                for index, root, path in files:
                    bundle.write(path, f"output-{index}/{path.relative_to(root).as_posix()}")
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return destination
    destination = await asyncio.to_thread(archive)
    return FileResponse(destination, media_type="application/zip", filename="hooky-clips.zip",
                        background=BackgroundTask(destination.unlink, missing_ok=True))


@app.get("/api/outputs/{filepath:path}")
async def serve_output_file(filepath: str, job_id: str | None = None):
    root = _output_root(job_id)
    path = root / filepath
    if _public_output(path, root):
        return FileResponse(path, media_type="video/mp4")
    raise HTTPException(status_code=404, detail="File not found")


@app.post("/api/analyze/start")
async def start_analyze_job(request: Request):
    data = await _request_object(request)
    return await _launch("analyze", _analysis_payload(data))


@app.get("/api/analyze/status/{job_id}")
async def get_analyze_status(job_id: str):
    return await get_job_status(job_id)


@app.post("/api/analyze")
async def analyze_video(request: Request):
    data = await _request_object(request)
    launched = await _launch("analyze", _analysis_payload(data))
    job_id = launched["job_id"]
    manager = _job_manager()
    task = manager.tasks.get(job_id)
    if task:
        await asyncio.shield(task)
    job = manager.jobs[job_id]
    safe = _job_response(job)
    return safe.get("result") or safe


@app.post("/api/jobs/render_clip")
@app.post("/api/jobs/render")
@app.post("/api/render")
async def render_single_clip(request: Request):
    data = await _request_object(request)
    url = _video_url(data)
    try:
        if isinstance(data.get("start"), bool) or isinstance(data.get("end"), bool):
            raise ValueError
        start, end = float(data.get("start", 0)), float(data.get("end", 0))
    except (ValueError, TypeError, OverflowError):
        raise HTTPException(status_code=400, detail="Start and end timestamps must be numeric.") from None
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
        raise HTTPException(status_code=400, detail="Start must be >= 0 and end must be greater than start.")
    style, framing = _render_options(data)
    title = _request_text(data, "title", "Hooky Reel")
    video_title, source_title = _request_text(data, "video_title"), _request_text(data, "source_title")
    payload = {"url": url, "start": start, "end": end, "style": style, "framing": framing,
               "title": title, "video_title": video_title or source_title}
    if "caption_edits" in data:
        try:
            payload["caption_edits"] = validate_caption_edits(data["caption_edits"])
        except (ValueError, OverflowError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
    return await _launch("render", payload)


@app.post("/api/jobs/make")
@app.post("/api/make")
async def launch_make_job(request: Request):
    data = await _request_object(request)
    target = _request_text(data, "target")
    if "url" in data:
        _video_url(data)
    if target.startswith("-") or any(char in target for char in "\r\n") or (target and not (validate_video_url(target) or target.startswith("@") or Path(target).is_file())):
        raise HTTPException(status_code=400, detail="Target must be a valid URL, @channel, or local file.")
    style, framing = _render_options(data)
    return await _launch("make", {"target": target, "style": style, "framing": framing})


@app.get("/api/jobs")
async def list_jobs():
    return {"jobs": [_job_response(job) for job in sorted(_job_manager().jobs.values(), key=lambda job: job["created_at"], reverse=True)]}


@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str):
    job = _job_manager().jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_response(job)


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    manager = _job_manager()
    if job_id not in manager.jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_response(await manager.cancel(job_id))


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """Serve the single-page visual studio dashboard with auth token injected."""
    if INDEX_HTML.exists():
        content = INDEX_HTML.read_text(encoding="utf-8")
        content = content.replace("HOOKY_AUTH_TOKEN_PLACEHOLDER", _WEB_TOKEN)
        response = HTMLResponse(content)
        response.set_cookie(
            key="hooky_token",
            value=_WEB_TOKEN,
            httponly=False,
            samesite="lax",
            path="/",
        )
        return response
    return HTMLResponse("<h1>Hooky Portal Static File Missing</h1>")


def run_server(args: list[str] | None = None) -> int:
    """Entry point to launch the web portal server."""
    import uvicorn
    import webbrowser

    arg_list = args or []
    if "--setup" in arg_list:
        from ..setup.cli import main as setup_main
        setup_main(["--redo"])
    elif (
        os.environ.get("HOOKY_LAUNCHER_PROMPTED") != "1"
        and sys.stdin
        and sys.stdin.isatty()
        and not any(a in arg_list for a in ("--no-prompt", "--skip-setup", "--headless", "-y"))
    ):
        try:
            ans = input("[hooky] Would you like to review or update .env choices before launching? [y/N]: ").strip().lower()
            if ans in ("y", "yes"):
                from ..setup.cli import main as setup_main
                setup_main(["--redo"])
        except (EOFError, KeyboardInterrupt):
            pass

    port = 8000
    host = "127.0.0.1"
    url = f"http://{host}:{port}"
    portal_url = f"{url}/?token={_WEB_TOKEN}"
    try:
        print(f"\n[hooky] Launching Hooky Web Portal at {portal_url}")
    except Exception:
        pass

    try:
        webbrowser.open(portal_url)
    except Exception:
        pass

    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0
