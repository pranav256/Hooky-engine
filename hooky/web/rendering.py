"""The selective download/subtitle/render path shared by clip export and make."""
from __future__ import annotations

import asyncio
import hashlib
import math
import re
import sys
from pathlib import Path

from hooky.config import OUTPUT_DIR
from hooky.core.cutter import cut_clip
from hooky.core.downloader import _format_string, _pick_height, yt_dlp_auth_args
from hooky.core.source_text import caption_words, apply_caption_edits
from hooky.core.subtitles import build_ass
from hooky.core.utils import format_timestamp
from hooky.web.analyzer import extract_youtube_id, prepare_source, extract_ranked_scenes


def sanitize_folder_name(name: str, fallback: str = "video_reels") -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', '', str(name))
    return re.sub(r"\s+", " ", cleaned).strip(". ")[:60].rstrip(". ") or fallback


async def render_clip(payload: dict, job: dict, cache: Path, cues: list[dict] | None = None) -> str:
    url, start, end = payload["url"], payload["start"], payload["end"]
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
        raise ValueError("Invalid selected clip timestamps.")
    style, framing = payload["style"], payload["framing"]
    title = payload.get("title") or "Hooky Reel"
    source_title = payload.get("video_title") or payload.get("source_title")
    if cues is None:
        metadata, cues = await prepare_source(url, cache, job)
        source_title = source_title or metadata.get("title", "Video")
        payload = {**payload, "source_duration": metadata.get("duration")}
    source_title = source_title or "Video"
    if payload.get("caption_edits"):
        cues = apply_caption_edits(cues, payload["caption_edits"])
    clean_name = re.sub(r"[^\w-]", "_", title.lower())[:32].strip("_") or "clip"
    identity = hashlib.sha256(f"{url}:{start}:{end}".encode()).hexdigest()[:16]
    downloaded_start = math.floor(start)
    snippet = cache / f"snippet_{identity}.mp4"
    cache.mkdir(parents=True, exist_ok=True)
    job.update(progress=25, step="Downloading selected footage...")
    print(f"[downloader] Fetching {end - start:.2f}s section from {url}", flush=True)
    if not (snippet.exists() and snippet.stat().st_size > 1000):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "yt_dlp", *yt_dlp_auth_args(), "--download-sections", f"*{downloaded_start}-{end + 1}",
            "--no-playlist", "--force-keyframes-at-cuts", "-f", _format_string(_pick_height(payload.get("source_duration"))),
            "--merge-output-format", "mp4", "-o", str(snippet), "--", url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, error = await process.communicate()
        if process.returncode != 0 or not snippet.exists():
            raise RuntimeError(f"Section download failed: {error.decode('utf-8', errors='replace')[-500:]}")
    relative = [{"start": max(0.0, cue["start"] - start), "end": min(end - start, cue["end"] - start), "text": cue["text"]}
                for cue in cues if cue["end"] > start and cue["start"] < end]
    ass_path = None
    if relative:
        words = caption_words(relative)
        if words:
            ass_path = cache / f"subs_{identity}_{style}.ass"
            ass_path.write_text(build_ass(words, clip_start=0, clip_end=end - start, style_name=style), encoding="utf-8")
    folder = sanitize_folder_name(source_title)
    output_dir = OUTPUT_DIR / folder
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"reel_{clean_name}_{job['id']}_{identity}.mp4"
    output = output_dir / filename
    job.update(progress=75, step="Encoding vertical reel...")
    print(f"[ffmpeg] Encoding {filename} ({style}, {framing})", flush=True)
    staged = cache / filename
    offset = start - downloaded_start
    await asyncio.to_thread(cut_clip, video_path=snippet, start=offset, end=offset + end - start, out_path=staged, ass_path=ass_path, framing=framing)
    if not staged.exists() or staged.stat().st_size <= 1000:
        raise RuntimeError("FFmpeg render failed: no complete output file was created.")
    staged.replace(output)
    snippet.unlink(missing_ok=True)
    return f"{folder}/{filename}"


async def make_url(payload: dict, job: dict, cache: Path) -> list[str]:
    url = payload["target"]
    metadata, cues = await prepare_source(url, cache, job)
    duration = metadata.get("duration", 0)
    hooks = extract_ranked_scenes(cues, metadata.get("title", "Video"), extract_youtube_id(url), duration,
                                  max_clips=min(10, max(4, int(duration // 240) + 1)) if duration else 6)
    if not hooks:
        raise RuntimeError("No usable speech clips were found; analyze another source or select a clip manually.")
    job["result"] = {
        "status": "success", "url": url, "video_title": metadata.get("title", "Video"),
        "video_id": metadata.get("id") or extract_youtube_id(url), "duration_fmt": format_timestamp(duration),
        "recommended_framing": payload["framing"], "recommended_style": payload["style"],
        "hooks": [{**hook, "duration": f"{hook['end'] - hook['start']:.1f}s"} for hook in hooks],
    }
    outputs = []
    job["status"] = "running"
    for index, hook in enumerate(hooks, 1):
        job.update(step=f"Rendering ranked clip {index}/{len(hooks)}...", progress=20)
        print(f"[pipeline] Rank #{index}: {hook['title']}", flush=True)
        clip = {**payload, "url": url, "start": hook["start"], "end": hook["end"],
                "title": hook["title"], "video_title": metadata.get("title", "Video"), "source_duration": duration}
        outputs.append(await render_clip(clip, job, cache, cues))
        job["output_files"] = outputs[:]
        job["output_file"] = outputs[0]
    if not outputs:
        raise RuntimeError("No reels were rendered.")
    return outputs
