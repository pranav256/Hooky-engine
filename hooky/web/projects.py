"""Named Studio snapshots, independent of disposable media and job history."""
from __future__ import annotations

import json
import math
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from hooky.core.source_text import apply_caption_edits, validate_caption_edits
from hooky.core.subtitles import STYLES
from hooky.core.utils import format_timestamp, validate_video_url
from hooky.web.jobs import atomic_json, _redirected

_WRITE_LOCK = threading.Lock()
MAX_PROJECT_BYTES = 1024 * 1024


class ProjectConflict(ValueError):
    pass


def _text(data: dict, key: str, limit: int, default: str = "") -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or len(value) > limit or "\0" in value:
        raise ValueError(f"'{key}' must be text of at most {limit} characters.")
    return value.strip()


def _range(data: dict) -> tuple[float, float]:
    start, end = data.get("start"), data.get("end")
    if (type(start) not in {int, float} or type(end) not in {int, float}
            or not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end):
        raise ValueError("Each clip and caption needs valid start and end times.")
    return start, end


def validate_project(data: dict) -> dict:
    name = _text(data, "name", 100)
    if not name or any(ord(char) < 32 for char in name):
        raise ValueError("Enter a project name of 1–100 characters.")
    studio = data.get("studio")
    if not isinstance(studio, dict):
        raise ValueError("Project must contain a Studio snapshot.")
    url = _text(studio, "url", 4096)
    if (not validate_video_url(url) or any(char in url for char in "\r\n")
            or urlsplit(url).username or urlsplit(url).password):
        raise ValueError("Project source must be an http(s) video URL without embedded credentials.")
    options = {"framing": {"auto", "fit", "center"}, "subtitle": {"auto", *STYLES},
               "recommended_framing": {"fit", "center"}, "recommended_style": set(STYLES)}
    defaults = {"framing": "auto", "subtitle": "auto", "recommended_framing": "fit", "recommended_style": "gold"}
    choices = {}
    for key, allowed in options.items():
        choices[key] = _text(studio, key, 30, defaults[key])
        if choices[key] not in allowed:
            raise ValueError(f"Unknown project '{key}'.")
    raw_hooks = studio.get("hooks")
    if not isinstance(raw_hooks, list) or not 1 <= len(raw_hooks) <= 100:
        raise ValueError("Save a project with 1–100 analyzed moments.")
    hooks, ids = [], set()
    for raw in raw_hooks:
        if not isinstance(raw, dict):
            raise ValueError("Each project moment must be an object.")
        hook_id = _text(raw, "id", 100)
        if not hook_id or hook_id in ids:
            raise ValueError("Project moments need distinct IDs.")
        ids.add(hook_id)
        start, end = _range(raw)
        raw_cues = raw.get("cues", [])
        if not isinstance(raw_cues, list) or len(raw_cues) > 200:
            raise ValueError("Each moment can contain at most 200 caption phrases.")
        cues = validate_caption_edits(raw_cues)
        edits = validate_caption_edits(raw.get("caption_edits", []))
        apply_caption_edits(cues, edits)
        hook = {"id": hook_id, "start": start, "end": end,
                "start_fmt": format_timestamp(start), "end_fmt": format_timestamp(end),
                "duration": f"{end - start:.1f}s", "cues": cues, "caption_edits": edits,
                "dialogue": [{"time": format_timestamp(c["start"]), "text": c["text"]} for c in cues],
                "words": (cues[0]["text"].split() if cues else [])[:8]}
        for key, limit in {"title": 100, "category": 100, "hook_quote": 1000, "reason": 400}.items():
            hook[key] = _text(raw, key, limit)
        if raw.get("retention_score") is not None:
            score = raw["retention_score"]
            if type(score) not in {int, float} or not math.isfinite(score) or not 0 <= score <= 100:
                raise ValueError("Hook score must be between 0 and 100.")
            hook["retention_score"] = score
        hooks.append(hook)
    selected = _text(studio, "selected_hook", 100, hooks[0]["id"])
    if selected not in ids:
        raise ValueError("Selected moment must belong to this project.")
    snapshot = {"url": url, "topic": _text(studio, "topic", 500),
                "video_title": _text(studio, "video_title", 1000), "hooks": hooks,
                "selected_hook": selected, **choices, "notice": _text(studio, "notice", 1000)}
    return {"name": name, "studio": snapshot}


def _path(directory: Path, project_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", project_id):
        raise ValueError("Invalid project ID.")
    path = directory / f"{project_id}.json"
    if _redirected(directory) or _redirected(path):
        raise ValueError("Project path must stay in Studio's project directory.")
    return path


def read_project(directory: Path, project_id: str) -> dict:
    path = _path(directory, project_id)
    if path.stat().st_size > MAX_PROJECT_BYTES:
        raise ValueError("Project file is too large; the saved file was kept.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("version") != 1 or data.get("id") != project_id
            or type(data.get("revision")) is not int or data["revision"] < 1
            or any(type(data.get(key)) not in {int, float} or not math.isfinite(data[key])
                   for key in ("created_at", "updated_at"))):
        raise ValueError("Project file needs repair; the saved file was kept.")
    return {**validate_project(data), **{key: data[key] for key in ("id", "version", "revision", "created_at", "updated_at")}}


def list_projects(directory: Path) -> dict:
    projects, unreadable = [], 0
    if _redirected(directory):
        raise ValueError("Project path must stay in Studio's project directory.")
    for path in directory.glob("*.json"):
        try:
            project = read_project(directory, path.stem)
            projects.append({key: project[key] for key in ("id", "name", "revision", "updated_at")})
        except (OSError, ValueError, OverflowError):
            unreadable += 1
    return {"projects": sorted(projects, key=lambda item: item["updated_at"], reverse=True),
            "unreadable_count": unreadable}


def save_project(directory: Path, data: dict, project_id: str | None = None) -> dict:
    snapshot = validate_project(data)
    # shortcut: one Studio process; use an interprocess lock before supporting multiple web workers.
    with _WRITE_LOCK:
        previous = read_project(directory, project_id) if project_id else None
        if previous and (type(data.get("revision")) is not int or data["revision"] != previous["revision"]):
            raise ProjectConflict("This project changed in another tab. Open its saved version or Save as new to keep both.")
        now = time.time()
        project_id = project_id or uuid4().hex
        project = {"version": 1, "id": project_id, "revision": previous["revision"] + 1 if previous else 1,
                   "created_at": previous["created_at"] if previous else now, "updated_at": now, **snapshot}
        if len(json.dumps(project, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_PROJECT_BYTES:
            raise ValueError("Project exceeds 1 MiB. Shorten caption text or save fewer moments.")
        atomic_json(_path(directory, project_id), project)
        return project
