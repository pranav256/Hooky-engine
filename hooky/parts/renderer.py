"""Sequential fragment rendering and stream-copy assembly of each story part."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

from ..config import REELS_MAX_DURATION
from ..core.checkpoints import digest, file_revision, load_checkpoint, save_checkpoint
from ..core.cutter import cut_clip, encoding_profile
from ..core.lowspec import lowspec_input_args
from ..core.subtitles import STYLES, build_ass
from ..core.utils import get_duration, has_audio
from ..options import PartsOptions


def valid_part(path: Path, expected: float) -> bool:
    try:
        actual = get_duration(path)
        return path.stat().st_size > 0 and actual <= REELS_MAX_DURATION and abs(actual - expected) < 0.5
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return False


def _concat_line(path: Path) -> str:
    escaped = str(path.resolve()).replace("'", "'\\''")
    return f"file '{escaped}'\n"


def render_part(video: Path, part: dict, words: list[dict], output_dir: Path,
                 options: PartsOptions, total_parts: int) -> Path:
    """Encode each retained fragment once; never open all source ranges together."""
    output_dir.mkdir(parents=True, exist_ok=True)
    if options.mode == "number-only":
        label = str(part["index"])
    else:
        label = f"Part {part['index']} / {total_parts}" if options.labels else ""
    signature = {"version": 1, "source": file_revision(video), "part": part,
                 "words": digest(words), "style": STYLES[options.sub_style],
                 "background": options.background, "label": label, **encoding_profile()}
    path = output_dir / f"part_{part['index']:03d}.mp4"
    checkpoint = output_dir / ".checkpoints" / f"part_{part['index']:03d}.json"
    fragments_dir = output_dir / ".fragments" / digest(signature)
    if load_checkpoint(checkpoint, signature) and valid_part(path, part["duration"]):
        print(f"[parts] Reusing completed {path.name}")
        if fragments_dir.exists():
            shutil.rmtree(fragments_dir)
        return path
    fragments_dir.mkdir(parents=True, exist_ok=True)
    fragments = []
    for index, fragment in enumerate(part["segments"], 1):
        start, end = fragment["start"], fragment["end"]
        fragment_signature = {"part": signature, "fragment": fragment}
        rendered = fragments_dir / f"fragment_{index:03d}.mp4"
        state = fragments_dir / f"fragment_{index:03d}.json"
        if not (load_checkpoint(state, fragment_signature) and valid_part(rendered, end - start)):
            selected = [word for word in words if word["start"] < end and word["end"] > start]
            ass = None
            if selected:
                ass = rendered.with_suffix(".ass")
                ass.write_text(build_ass(selected, start, end, options.sub_style), encoding="utf-8")
            cut_clip(video, start, end, rendered, ass_path=ass,
                     framing="fit" if options.background == "blur" else "pad",
                     background_height=480 if options.background == "blur" else 0, label=label)
            save_checkpoint(state, fragment_signature, {"complete": True})
        fragments.append(rendered)
    concat = fragments_dir / "concat.txt"
    concat.write_text("ffconcat version 1.0\n" + "".join(_concat_line(p) for p in fragments),
                      encoding="utf-8")
    temporary = path.with_name(f".{path.stem}.{uuid4().hex}.tmp.mp4")
    try:
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error", *lowspec_input_args(),
            "-f", "concat", "-safe", "0", "-i", str(concat),
            "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
            "-movflags", "+faststart", str(temporary),
        ], check=True, capture_output=True, timeout=300)
        if not valid_part(temporary, part["duration"]):
            raise RuntimeError(f"Incomplete or over-length output for {path.name}")
        if has_audio(video) and not has_audio(temporary):
            raise RuntimeError(f"Missing source audio in {path.name}")
        temporary.replace(path)
        save_checkpoint(checkpoint, signature, {"complete": True})
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr or b""
        detail = detail.decode(errors="replace") if isinstance(detail, bytes) else str(detail)
        raise RuntimeError(f"Part assembly failed: {detail[-2000:]}") from exc
    finally:
        temporary.unlink(missing_ok=True)
    shutil.rmtree(fragments_dir)
    print(f"[parts] Saved {path.name} ({get_duration(path):.1f}s)")
    return path

