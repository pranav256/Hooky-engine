"""Explicit disposable-media cleanup with conservative path guards."""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..config import (CHANNELS_FILE, DELETE_POSTED_CLIPS,
                      DELETE_SOURCE_AFTER_CLIP, LINKS_FILE, PROJECT_ROOT,
                      SESSION_FILE)


def _protected_paths() -> set[Path]:
    state_dir = Path(os.environ.get("STATE_DIR") or PROJECT_ROOT / ".hooky-state")
    return {
        PROJECT_ROOT / ".env",
        PROJECT_ROOT / ".git",
        LINKS_FILE.resolve(),
        CHANNELS_FILE.resolve(),
        SESSION_FILE.resolve(),
        state_dir.resolve(),
    }


def _can_remove(path: Path) -> bool:
    """Prevent cleanup from targeting project control files or directories."""
    protected = _protected_paths()
    if path in protected or path.resolve() in protected:
        return False
    return path.is_file() or path.is_symlink()


def _unlink(path: Path, removed: list[Path]) -> None:
    if not _can_remove(path):
        return
    path.unlink()
    removed.append(path)


def _existing_removable(paths: list[Path]) -> list[Path]:
    existing: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        if _can_remove(path):
            existing.append(path)
    return existing


def clip_artifact_candidates(clip_path: Path) -> list[Path]:
    """Return existing generated files that a posted clip cleanup may remove."""
    clip_path = Path(clip_path)
    return _existing_removable([
        clip_path,
        clip_path.with_suffix(".ass"),
        clip_path.with_name(clip_path.stem + ".caption.txt"),
        clip_path.with_name(clip_path.stem + "_cover.jpg"),
    ])


def source_artifact_candidates(video_path: Path) -> list[Path]:
    """Return existing disposable files associated with a processed source."""
    video_path = Path(video_path)
    candidates = [video_path, video_path.with_name(video_path.stem + "_analysis.mp4")]
    if video_path.parent.name == "downloads":
        metadata_path = video_path.with_suffix(".json")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            value = metadata.get("source_captions", "") if isinstance(metadata, dict) else ""
            if value:
                caption_path = Path(value)
                if caption_path.parent == video_path.parent:
                    candidates.append(caption_path)
        except (OSError, ValueError, TypeError):
            pass
        candidates.append(metadata_path)
    return _existing_removable(candidates)


def delete_clip_artifacts(clip_path: Path, *, enabled: bool = DELETE_POSTED_CLIPS) -> list[Path]:
    """Remove a confirmed-post clip and its generated sidecars.

    The queue must be durably marked posted before this function is called.
    It never removes manifests, checkpoints, queue history, or directories.
    """
    if not enabled:
        return []
    removed: list[Path] = []
    for path in clip_artifact_candidates(clip_path):
        _unlink(path, removed)
    return removed


def delete_source_artifacts(video_path: Path, *, enabled: bool = DELETE_SOURCE_AFTER_CLIP) -> list[Path]:
    """Remove a successfully processed source and disposable download siblings."""
    if not enabled:
        return []
    removed: list[Path] = []
    for path in source_artifact_candidates(video_path):
        _unlink(path, removed)
    return removed

