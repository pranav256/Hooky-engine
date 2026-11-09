"""Render selected windows as Instagram-ready vertical clips.

The lean renderer deliberately has one visual path: every clip is cut from the
master, center-cropped to 1080x1920, and encoded as high-quality H.264 with
AAC audio. The optional ASS file is burned in during that same encode.
"""
from __future__ import annotations

import json
import subprocess
import sys
from uuid import uuid4
from pathlib import Path

from ..config import AUDIO_NORMALIZATION, CLIP_HEIGHT, CLIP_WIDTH, OUTPUT_DIR, TARGET_FPS
from .lowspec import (AUDIO_BITRATE, X264_CRF, X264_PRESET,
                      lowspec_encode_args, lowspec_input_args)
from .subtitles import build_ass, FONT_OVERRIDE, STYLES
from .utils import format_timestamp, has_audio, vertical_crop_filter, get_duration
from .checkpoints import atomic_json, digest, file_revision, source_id, clip_id


def _subtitles_filter(ass_path: Path) -> str:
    """Return an ffmpeg subtitles filter with a safely escaped filename."""
    escaped = str(ass_path).replace("\\", "\\\\")
    for char in ("'", ":", ",", "[", "]"):
        escaped = escaped.replace(char, "\\" + char)
    return f"subtitles=filename='{escaped}'"


_VIDEO_ARGS = [
    "-r", str(TARGET_FPS),
    "-c:v", "libx264",
    "-preset", X264_PRESET,
    "-crf", X264_CRF,
    "-pix_fmt", "yuv420p",
    "-profile:v", "high",
    "-level:v", "4.1",
    "-video_track_timescale", "15360",
    *lowspec_encode_args(),
]


def _ffmpeg_error(result: subprocess.CompletedProcess[str], action: str,
                  name: str) -> RuntimeError:
    detail = (result.stderr or "")[-2000:]
    print(f"[clip] ffmpeg error ({action}):\n{detail}", file=sys.stderr)
    return RuntimeError(f"ffmpeg failed for {name}")


def cut_clip(video_path: Path, start: float, end: float, out_path: Path,
              ass_path: Path | None = None, framing: str = "center",
              background_height: int = 0, label: str = "") -> Path:
    """Render ``[start, end]`` from the master as a centered 9:16 Reel."""
    if end <= start:
        raise ValueError("clip end must be greater than clip start")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    vf = vertical_crop_filter(framing, background_height)
    if ass_path is not None:
        vf += f",{_subtitles_filter(ass_path)}"
    if label:
        # The parts renderer supplies only a numeric series label, not source text.
        if not all(char.isdigit() or char in "Part /" for char in label):
            raise ValueError("Unsupported part label")
        vf += (f",drawtext=text='{label}':fontsize=64:fontcolor=white:"
               "borderw=3:bordercolor=black:x=(w-tw)/2:y=h*0.08")

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        *lowspec_input_args(),
        "-ss", f"{max(0.0, start):.3f}", "-i", str(video_path),
        "-t", f"{end - start:.3f}",
        "-map", "0:v:0", "-vf", vf,
        *_VIDEO_ARGS,
        "-movflags", "+faststart",
    ]
    if has_audio(video_path):
        cmd += [
            "-map", "0:a:0?",
            "-c:a", "aac", "-b:a", AUDIO_BITRATE,
            "-ar", "44100", "-ac", "2",
        ]
        if AUDIO_NORMALIZATION:
            cmd += ["-af", "loudnorm=I=-14:TP=-1.0:LRA=9"]
    temporary = out_path.with_name(f".{out_path.stem}.{uuid4().hex}.tmp.mp4")
    cmd.append(str(temporary))

    print(f"[clip] Cutting {format_timestamp(start)} -> "
          f"{format_timestamp(end)} ({end - start:.1f}s)"
          f"{' +subs' if ass_path else ''} ...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if result.returncode != 0:
            raise _ffmpeg_error(result, "render", out_path.name)
        if not temporary.exists() or not _valid_render(temporary, end - start):
            raise RuntimeError(f"ffmpeg produced incomplete output for {out_path.name}")
        temporary.replace(out_path)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"[clip] Saved {out_path.name} "
          f"({out_path.stat().st_size / (1024 * 1024):.1f} MB)")
    return out_path


def encoding_profile() -> dict:
    """Cache identity shared by single-window clips and multi-window parts."""
    return {"video_args": _VIDEO_ARGS, "audio_bitrate": AUDIO_BITRATE,
            "audio_normalization": AUDIO_NORMALIZATION, "font": FONT_OVERRIDE,
            "canvas": [CLIP_WIDTH, CLIP_HEIGHT]}


def make_cover(video_path: Path, timestamp: float, out_path: Path,
               framing: str = "center") -> Path:
    """Generate a 1080x1920 JPEG cover from the master."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        *lowspec_input_args(),
        "-ss", f"{max(0.0, timestamp):.3f}", "-i", str(video_path),
        "-frames:v", "1", "-vf", vertical_crop_filter(framing),
        "-q:v", "3", str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise _ffmpeg_error(result, "cover", out_path.name)
    return out_path


def _words_digest(words: list[dict] | None) -> str:
    if not words:
        return ""
    return digest(words)


def _load_render_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_render_state(path: Path, state: dict) -> None:
    atomic_json(path, state)


def _valid_render(path: Path, duration: float) -> bool:
    try:
        return path.stat().st_size > 0 and abs(get_duration(path) - duration) < 0.5
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return False


def cut_all_clips(video_path: Path, clips: list[dict], output_dir: Path | None = None,
                  make_covers: bool = True, words: list[dict] | None = None,
                  sub_style: str = "hormozi") -> list[dict]:
    """Render all selected windows from ``video_path`` using the master file.

    ``words`` are global source-time word timings. They are converted to a
    local ASS timeline for each selected window before being burned in.
    """
    output_dir = output_dir or OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    source_signature = file_revision(video_path)
    render_state_path = output_dir / "render_state.json"
    render_state = _load_render_state(render_state_path)

    for i, clip in enumerate(clips, start=1):
        start, end = float(clip["start"]), float(clip["end"])
        selected_words = [w for w in words or [] if w["start"] < end and w["end"] > start]
        identity = clip.get("clip_id") or clip_id(source_id(video_path), start, end)
        signature = {
            "version": 3, **source_signature,
            "start": round(start, 3), "end": round(end, 3),
            "framing": clip.get("framing", "center"),
            "sub_style": STYLES[sub_style], "font": FONT_OVERRIDE,
            "words": _words_digest(selected_words),
            **encoding_profile(),
        }
        stem = f"reel_{identity}_{digest(signature)[:8]}"
        clip_path = output_dir / f"{stem}.mp4"
        cover_path = output_dir / f"{stem}_cover.jpg"
        ass_path: Path | None = None
        if words:
            ass_text = build_ass(selected_words, start, end, sub_style)
            if ass_text:
                ass_path = output_dir / f"{stem}.ass"
                ass_path.write_text(ass_text, encoding="utf-8")

        cached = render_state.get(stem, {})
        can_resume = (cached.get("signature") == signature
                      and _valid_render(clip_path, end - start))
        if not can_resume:
            framing = clip.get("framing", "center")
            cut_kwargs = {"ass_path": ass_path, "framing": framing}
            if framing == "fit":
                cut_kwargs["background_height"] = 480
            cut_clip(video_path, start, end, clip_path, **cut_kwargs)
        else:
            print(f"[clip] Reusing completed {clip_path.name}")
        # Commit video completion before optional cover work so it can resume independently.
        render_state[stem] = {"signature": signature}
        _save_render_state(render_state_path, render_state)
        entry: dict = {
            "index": i, "clip_id": identity,
            "start": start,
            "end": end,
            "reason": clip.get("reason", ""),
            "framing": clip.get("framing", "center"),
            "path": clip_path,
            "subtitled": bool(ass_path),
        }

        if make_covers:
            try:
                if not (can_resume and cover_path.exists() and cover_path.stat().st_size > 0):
                    make_cover(video_path, start + 0.4, cover_path,
                               framing=clip.get("framing", "center"))
                entry["cover"] = cover_path
            except RuntimeError as exc:
                print(f"[cover] Skipping cover: {exc}", file=sys.stderr)
                entry["cover"] = None
        else:
            entry["cover"] = None
        if make_covers:
            render_state[stem] = {"signature": signature,
                                  "cover": bool(entry.get("cover"))}
            _save_render_state(render_state_path, render_state)
        results.append(entry)
    return results

