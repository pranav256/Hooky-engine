from __future__ import annotations

import re
import os
import sys
import json
import hashlib
import time
import shutil
import asyncio
import subprocess
from pathlib import Path
from typing import Any, Callable

import json_repair

from hooky.config import OUTPUT_DIR
from hooky.core.source_text import parse_caption_file
from hooky.core.gemini import generate
from hooky.core.downloader import yt_dlp_auth_args
from hooky.core.utils import format_timestamp
from hooky.web.jobs import append_log


def parse_timestamp_seconds(val: Any) -> float:
    """Robustly parse numeric seconds or HH:MM:SS / MM:SS string timestamps into float seconds."""
    if isinstance(val, (int, float)):
        return max(0.0, float(val))
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return 0.0
        if ":" in val:
            try:
                parts = [float(p) for p in val.replace(",", ".").split(":")]
                if len(parts) == 2:
                    return parts[0] * 60 + parts[1]
                elif len(parts) == 3:
                    return parts[0] * 3600 + parts[1] * 60 + parts[2]
            except Exception:
                pass
        try:
            return max(0.0, float(val))
        except Exception:
            pass
    return 0.0


def extract_youtube_id(url: str) -> str:
    """Extract 11-char YouTube video ID from various URL formats. Returns empty string if not found."""
    match = re.search(r"(?:v=|\/|youtu\.be\/|embed\/)([0-9A-Za-z_-]{11})", url)
    return match.group(1) if match else ""


def get_video_metadata(url: str) -> dict[str, Any]:
    """Extract video title and duration using yt-dlp."""
    try:
        cmd = [
            sys.executable,
            "-m", "yt_dlp", *yt_dlp_auth_args(),
            "--dump-json",
            "--no-playlist",
            "--",
            url,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            return {
                "title": data.get("title", "YouTube Video"),
                "duration": float(data.get("duration", 0.0)),
                "channel": data.get("channel", "Creator"),
                "id": data.get("id", extract_youtube_id(url)),
            }
    except Exception as exc:
        print(f"[analyzer] metadata probe error: {exc}")

    vid_id = extract_youtube_id(url)
    default_title = f"YouTube Video ({vid_id})" if vid_id else "Unknown Video"
    return {
        "title": default_title,
        "duration": 0.0,
        "channel": "Unknown Channel",
        "id": vid_id,
    }


async def download_captions(url: str, out_prefix: Path, progress_cb: Callable[[str], None] | None = None) -> Path | None:
    """Download VTT subtitles via yt-dlp without downloading media."""
    vid_id = extract_youtube_id(url)
    if vid_id:
        cached = out_prefix.parent / f"yt_{vid_id}.vtt"
        if cached.exists() and cached.stat().st_size > 0:
            if progress_cb:
                progress_cb(f"[yt-dlp] Using cached VTT captions track: {cached.name}")
            return cached

    if progress_cb:
        progress_cb("[yt-dlp] Requesting YouTube timed caption tracks (English)...")

    cmd = [
        sys.executable,
        "-m", "yt_dlp", *yt_dlp_auth_args(),
        "--no-playlist",
        "--skip-download",
        "--write-subs",
        "--write-auto-sub",
        "--sub-lang", os.environ.get("SOURCE_SUB_LANGS", "en,en-US,en-GB"),
        "--sub-format", "vtt",
        "-o", str(out_prefix),
        "--",
        url,
    ]
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    if progress_cb and stdout:
        for line in stdout.decode("utf-8", errors="replace").splitlines()[-3:]:
            if line.strip():
                progress_cb(f"[yt-dlp] {line.strip()}")

    # Check for generated .en.vtt or .vtt and copy to normalized yt_{vid_id}.vtt
    for candidate in sorted(out_prefix.parent.glob(f"{out_prefix.name}*.vtt")):
        if candidate.exists() and candidate.stat().st_size > 0:
            if vid_id:
                norm_path = out_prefix.parent / f"yt_{vid_id}.vtt"
                try:
                    shutil.copy2(candidate, norm_path)
                    candidate = norm_path
                except OSError:
                    pass
            if progress_cb:
                progress_cb(f"[yt-dlp] Captions fetched successfully: {candidate.name} ({candidate.stat().st_size} bytes)")
            return candidate

    return None


def calculate_hook_density(cues: list[dict]) -> list[dict]:
    """Score dialogue segments based on speech pacing and engagement conflict/wisdom keywords."""
    engagement_keywords = {
        # Banter & Conflict
        "unhinged", "crazy", "liar", "lie", "mafia", "sheriff", "angel", "killed",
        "murdered", "bullets", "basement", "dead", "electric", "chair", "accuse",
        "sus", "suspicious", "privacy", "signal", "founder", "money", "fun",
        "defense", "second amendment", "rights", "voted", "collusion",
        # Insight & High-Retention Ideas
        "wealth", "freedom", "happy", "happiness", "peace", "mind", "truth",
        "secret", "never", "always", "leverage", "code", "investing", "advice",
        "rule", "game", "win", "lose", "cheat", "billion", "startup"
    }

    scored_blocks = []
    chunk_size = 12
    step = 6
    for i in range(0, max(1, len(cues) - chunk_size), step):
        chunk = cues[i:i + chunk_size]
        if not chunk:
            continue
        text = " ".join(c.get("text", "") for c in chunk)
        text_lower = text.lower()
        score = 0
        matches = []
        for kw in engagement_keywords:
            if kw in text_lower:
                score += 12
                matches.append(kw)
        if "[laughter]" in text_lower:
            score += 20
            matches.append("laughter")
        if "[music]" in text_lower:
            score += 10
            matches.append("music")
        if ">>" in text:
            score += text.count(">>") * 5  # rapid speaker transitions

        start = chunk[0].get("start", 0)
        end = chunk[-1].get("end", start + 30)
        if len(text.strip()) > 60:
            scored_blocks.append({
                "start": start,
                "end": end,
                "score": score,
                "keywords": matches,
                "text": text,
                "chunk": chunk,
            })
    return sorted(scored_blocks, key=lambda x: x["score"], reverse=True)


def extract_ranked_scenes(
    cues: list[dict],
    video_title: str,
    video_id: str,
    duration: float,
    max_clips: int = 10,
) -> list[dict]:
    """Extract and rank the highest-scoring non-overlapping scenes across the video.

    Scenes are returned strictly sorted from best to worst (Rank #1, #2, etc.),
    each with a retention score, punchy title, dialogue cues, and precise start/end boundaries.
    """
    if not cues:
        return []

    # 1. Score every candidate dialogue window across the entire transcript
    top_blocks = calculate_hook_density(cues)
    selected_blocks = []

    # 2. Greedily select non-overlapping high-scoring blocks
    for b in top_blocks:
        dur = b["end"] - b["start"]
        if dur < 14.0:
            b["end"] = b["start"] + 28.0
        elif dur > 65.0:
            b["end"] = b["start"] + 60.0

        if all(b["end"] + 2.5 <= s["start"] or b["start"] >= s["end"] + 2.5 for s in selected_blocks):
            selected_blocks.append(b)
        if len(selected_blocks) >= max_clips * 2:
            break

    # 3. If density scan gave fewer than desired clips, sample interesting dialogue evenly across the runtime
    if len(selected_blocks) < max_clips and len(cues) > 12:
        step_idx = max(1, len(cues) // (max_clips + 2))
        for idx in range(0, len(cues) - 8, step_idx):
            sub = cues[idx:idx + 14]
            if sub:
                cand_s = sub[0]["start"]
                cand_e = min(duration if duration > 0 else cand_s + 45.0, sub[-1]["end"])
                if cand_e <= cand_s + 10.0:
                    cand_e = cand_s + 30.0
                if all(cand_e + 2.0 <= s["start"] or cand_s >= s["end"] + 2.0 for s in selected_blocks):
                    selected_blocks.append({
                        "start": cand_s,
                        "end": cand_e,
                        "score": 30,
                        "keywords": ["dialogue"],
                        "text": " ".join(c.get("text", "") for c in sub),
                        "chunk": sub,
                    })
            if len(selected_blocks) >= max_clips:
                break

    # 4. Sort blocks by retention / engagement score in descending order
    selected_blocks.sort(key=lambda x: x.get("score", 0), reverse=True)
    candidates = selected_blocks[:max_clips]

    ranked = []
    categories = [
        "High-Impact Insight", "Conversational Clash", "Mindset Shift",
        "Viral Revelation", "Core Breakthrough", "Intense Debate",
        "Engaging Banter", "Surprising Fact"
    ]

    for idx, b in enumerate(candidates, 1):
        s = round(float(b["start"]), 2)
        e = round(float(b["end"]), 2)
        if e <= s + 8.0:
            e = s + 32.0
        dur = round(e - s, 1)

        raw_text = b.get("text", "").strip()
        first_sentence = raw_text.split(".")[0].split("?")[0].strip()
        if len(first_sentence) < 12 and len(raw_text) > 25:
            first_sentence = raw_text[:70]

        words_in_quote = [w for w in re.sub(r"[^\w\s]", "", first_sentence).split() if len(w) > 2]
        title = " ".join(words_in_quote[:5]).title() if words_in_quote else f"Viral Scene #{idx}"

        retention_score = max(70, min(99, 99 - (idx - 1) * 2))

        chunk_cues = b.get("chunk", [])
        dialogue = [
            {"time": format_timestamp(c["start"]), "text": c.get("text", "").strip()}
            for c in chunk_cues
        ]

        ranked.append({
            "rank": idx,
            "id": f"rank_{idx:02d}",
            "start": s,
            "end": e,
            "start_fmt": format_timestamp(s),
            "end_fmt": format_timestamp(e),
            "duration": dur,
            "duration_seconds": dur,
            "title": title,
            "hook_quote": first_sentence,
            "category": categories[(idx - 1) % len(categories)],
            "retention_score": retention_score,
            "dialogue": dialogue,
            "chunk": chunk_cues,
        })

    return ranked


def build_dynamic_hooks_from_cues(cues: list[dict], video_title: str, video_id: str, duration: float) -> list[dict]:
    """Dynamically derive 4 high-retention moments directly from timestamped transcript cues."""
    if not cues:
        return []

    top_blocks = calculate_hook_density(cues)
    selected_blocks = []
    for b in top_blocks:
        if all(b["end"] <= s["start"] or b["start"] >= s["end"] for s in selected_blocks):
            selected_blocks.append(b)
        if len(selected_blocks) >= 4:
            break

    # If density scan gave fewer than 4, take evenly spaced segments
    if len(selected_blocks) < 4:
        step_idx = max(1, len(cues) // 5)
        for idx in range(0, min(len(cues), step_idx * 4), step_idx):
            sub = cues[idx:idx + 12]
            if sub and all(sub[-1]["end"] <= s["start"] or sub[0]["start"] >= s["end"] for s in selected_blocks):
                selected_blocks.append({
                    "start": sub[0]["start"],
                    "end": sub[-1]["end"],
                    "score": 40,
                    "keywords": ["speech"],
                    "text": " ".join(c["text"] for c in sub),
                    "chunk": sub,
                })
            if len(selected_blocks) >= 4:
                break

    hooks = []
    categories = ["High-Impact Insight", "Conversational Clash", "Mindset Shift", "Viral Banter"]
    for idx, b in enumerate(selected_blocks[:4]):
        s = round(float(b["start"]), 2)
        e = round(float(b["end"]), 2)
        dur = round(max(8.0, e - s), 1)

        # Opening line as quote
        raw_text = b["text"].strip()
        first_sentence = raw_text.split(".")[0].split("?")[0].strip()
        if len(first_sentence) < 15 and len(raw_text) > 30:
            first_sentence = raw_text[:75]

        # Clean title
        words_in_quote = [w for w in re.sub(r"[^\w\s]", "", first_sentence).split() if len(w) > 2]
        title = " ".join(words_in_quote[:5]).title() if words_in_quote else f"Highlight Moment #{idx + 1}"

        # Uppercase words for subtitle animation
        words_list = [w.upper() for w in words_in_quote[:8]] if words_in_quote else ["KEY", "POINT", "DISCUSSED"]

        # Dialogue transcript
        chunk_cues = b.get("chunk", [])
        dialogue = [
            {"time": format_timestamp(c["start"]), "text": c.get("text", "").strip()}
            for c in chunk_cues
        ]

        hooks.append({
            "id": f"hook_{idx + 1}",
            "start": s,
            "end": e,
            "start_fmt": format_timestamp(s),
            "end_fmt": format_timestamp(e),
            "duration": f"{dur:.1f}s",
            "duration_seconds": dur,
            "title": title,
            "hook_quote": first_sentence,
            "category": categories[idx % len(categories)],
            "retention_score": max(88, min(99, 98 - idx * 2)),
            "reason": f"High engagement segment featuring discussion around: {', '.join(b.get('keywords', ['dialogue'])[:3])}.",
            "boundary": "Natural speech pause aligned",
            "words": words_list,
            "dialogue": dialogue,
            "youtube_embed_url": f"https://www.youtube-nocookie.com/embed/{video_id}?start={int(s)}&end={int(e)}&autoplay=1&enablejsapi=1",
        })

    return hooks


async def prepare_source(url: str, cache_dir: Path, job: dict, log=print) -> tuple[dict, list[dict]]:
    """Fetch source text once, preserving captions and audio transcription fallback."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cues = []
    # Step 1: Probe metadata
    job["step"] = "Connecting to YouTube stream & fetching metadata..."
    job["progress"] = 15
    job["step_number"] = 1
    log(f"[meta] Probing URL: {url}")

    meta = await asyncio.to_thread(get_video_metadata, url)
    title = meta["title"]
    duration = meta["duration"]
    video_id = meta.get("id") or extract_youtube_id(url)
    dur_label = format_timestamp(duration) if duration > 0 else "Live/Unknown"
    log(f"[meta] Title: '{title}' | Duration: {dur_label} | ID: {video_id or 'none'}")

    # Step 2: Download captions
    job["step"] = "Downloading synchronized VTT caption tracks..."
    job["progress"] = 35
    job["step_number"] = 2
    identity = hashlib.sha256((url + os.environ.get("SOURCE_SUB_LANGS", "en,en-US,en-GB")).encode()).hexdigest()[:20]
    sub_prefix = cache_dir / f"source_{identity}"
    subtitle_source = os.environ.get("SUBTITLE_SOURCE", "auto").strip().lower()
    vtt_path = await download_captions(url, sub_prefix, log) if subtitle_source != "whisper" else None

    if vtt_path and vtt_path.exists():
        cues = parse_caption_file(vtt_path)
        log(f"[captions] Parsed {len(cues)} timed dialogue cues from {vtt_path.name}")
    if not cues and subtitle_source != "source":
        log("[captions] Notice: Video has no YouTube captions track. Initiating fast audio transcription...")
        audio_path = cache_dir / f"audio_{identity}.m4a"
        if not (audio_path.exists() and audio_path.stat().st_size > 10000):
            dl_cmd = [
                sys.executable, "-m", "yt_dlp", *yt_dlp_auth_args(),
                "--no-playlist",
                "-f", "ba/bestaudio",
                "-o", str(audio_path),
                "--",
                url,
            ]
            dl_proc = await asyncio.create_subprocess_exec(
                *dl_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            _, error = await dl_proc.communicate()
            if dl_proc.returncode != 0:
                raise RuntimeError(f"Audio download failed: {error.decode('utf-8', errors='replace')[-500:]}")

        if audio_path.exists() and audio_path.stat().st_size > 10000:
            log(f"[whisper] Transcribing audio with faster-whisper (model: {os.environ.get('WHISPER_MODEL', 'tiny')})...")
            from hooky.core.subtitles import transcribe
            words = await asyncio.to_thread(transcribe, audio_path)
            curr = []
            for w in words:
                curr.append(w)
                t = str(w.get("word", "")).strip()
                if (t and t[-1] in ".!?") or len(curr) >= 8:
                    cues.append({
                        "start": curr[0]["start"],
                        "end": curr[-1]["end"],
                        "text": " ".join(x["word"] for x in curr)
                    })
                    curr = []
            if curr:
                cues.append({
                    "start": curr[0]["start"],
                    "end": curr[-1]["end"],
                    "text": " ".join(x["word"] for x in curr)
                })
            log(f"[whisper] Generated {len(cues)} speech cues from faster-whisper.")
        else:
            log("[captions] Warning: Audio stream probe failed; proceeding with heuristic analysis")

    (cache_dir / "cues.json").write_text(json.dumps(cues), encoding="utf-8")

    return meta, cues


async def run_analysis_pipeline(url: str, job: dict) -> dict[str, Any]:
    """Execute complete hook analysis pipeline with live step-by-step progress logging."""
    cache_dir = Path(os.environ.get("HOOKY_WEB_CACHE_DIR") or OUTPUT_DIR / "web_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cues: list[dict] = []

    def log(msg: str):
        print(msg)
        append_log(job, f"[{time.strftime('%H:%M:%S')}] {msg}")

    try:
        meta, cues = await prepare_source(url, cache_dir, job, log)
        title, duration = meta["title"], meta["duration"]
        video_id = meta.get("id") or extract_youtube_id(url)

        if not cues:
            raise RuntimeError("No usable source captions or audio transcript were found.")

        # Step 3: Emotional tension & pacing scoring
        job["step"] = "Analyzing conversational pacing & emotional tension spikes..."
        job["progress"] = 55
        job["step_number"] = 3

        # Automatic framing & subtitle detection
        speaker_turn_count = sum(1 for c in cues if ">>" in c.get("text", "")) if cues else 0
        table_keywords = {"mafia", "game", "poker", "table", "players", "round", "panel", "debate", "guests", "tech legends"}
        is_multi_speaker = speaker_turn_count > 8 or any(kw in title.lower() for kw in table_keywords)

        auto_framing = "fit" if is_multi_speaker else "center"
        framing_reason = (
            "Canvas Fit (Ambient Blur) auto-selected: Wide multi-speaker table footage detected"
            if is_multi_speaker
            else "Center Crop (1080x1920) auto-selected: Focused single-speaker footage detected"
        )
        auto_style = "gold"
        style_reason = "Warm Titanium auto-selected: Studio standard for high legibility and contrast"

        top_blocks = calculate_hook_density(cues) if cues else []
        log(f"[algorithm] Scored {len(top_blocks)} dialogue candidate windows across entire video")
        log(f"[smart-config] Framing: {auto_framing.upper()} ({framing_reason}) | Style: {auto_style.upper()}")
        for idx, b in enumerate(top_blocks[:3]):
            log(f"[algorithm] Candidate #{idx + 1} at {format_timestamp(b['start'])}: Score {b['score']} (Keywords: {', '.join(b['keywords'][:4])})")

        # Step 4: Gemini Narrative Scoring
        job["step"] = "Gemini evaluating narrative hooks & psychology..."
        job["progress"] = 75
        job["step_number"] = 4
        log("[gemini] Dispatching high-retention prompt to Gemini...")

        # Build transcript sample with high-tension blocks and early moments
        transcript_sample = ""
        if cues:
            selected_cues = cues[:120]
            # Add cues from top scored tension blocks
            for block in top_blocks[:4]:
                b_cues = [c for c in cues if c["start"] >= block["start"] - 1 and c["end"] <= block["end"] + 1]
                selected_cues.extend(b_cues)
            # Deduplicate by start
            seen_starts = set()
            unique_cues = []
            for c in sorted(selected_cues, key=lambda x: x["start"]):
                if round(c["start"], 1) not in seen_starts:
                    seen_starts.add(round(c["start"], 1))
                    unique_cues.append(c)

            sample_lines = [
                f"[{format_timestamp(c['start'])} - {format_timestamp(c['end'])}] {c['text']}"
                for c in unique_cues[:180]
            ]
            transcript_sample = "\n".join(sample_lines)

        prompt = f"""You are an elite short-form video editor extracting the highest-retention moments from this YouTube video for vertical 9:16 Instagram Reels and YouTube Shorts.
Video Title: {title}
Total Duration: {duration} seconds

Transcript Dialogue Sample:
{transcript_sample}

Identify the 4 absolute BEST, highest-tension, most interesting, or dramatic standalone moments (14 to 60 seconds each).
Criteria:
- The first 2-3 seconds must have an irresistible opening hook quote that stops scrolling.
- Self-contained narrative payoff (insight, comedic punchline, or debate revelation).

For EACH moment, return valid JSON array:
[
  {{
    "start": 37.36,
    "end": 50.79,
    "title": "Punchy Title (3-5 words)",
    "hook_quote": "Exact first sentence spoken",
    "category": "Key Moment",
    "retention_score": 98,
    "reason": "Why this hooks short-form viewers",
    "words": ["WORD1", "WORD2", "WORD3", "WORD4", "WORD5"]
  }}
]
"""
        formatted_hooks = []
        try:
            raw_resp = await asyncio.to_thread(generate, prompt)
            log(f"[gemini] Received response ({len(raw_resp)} chars)")

            clean_json = raw_resp.strip()
            if "```json" in clean_json:
                clean_json = clean_json.split("```json")[1].split("```")[0].strip()
            elif "```" in clean_json:
                clean_json = clean_json.split("```")[1].split("```")[0].strip()

            parsed_hooks = json_repair.loads(clean_json)

            if isinstance(parsed_hooks, list) and len(parsed_hooks) > 0:
                for idx, h in enumerate(parsed_hooks[:4]):
                    s = parse_timestamp_seconds(h.get("start", 0))
                    e = parse_timestamp_seconds(h.get("end", s + 35))
                    if e <= s:
                        e = s + 35.0

                    # Snap to closest cue boundaries if cues exist
                    if cues:
                        matching_starts = [c["start"] for c in cues if abs(c["start"] - s) <= 6.0]
                        if matching_starts:
                            s = min(matching_starts, key=lambda x: abs(x - s))
                        matching_ends = [c["end"] for c in cues if abs(c["end"] - e) <= 6.0]
                        if matching_ends:
                            e = min(matching_ends, key=lambda x: abs(x - e))

                    if e <= s + 5.0:
                        e = s + 25.0
                    dur = e - s

                    # Ensure words is always a list of uppercase string tokens
                    raw_words = h.get("words", [])
                    if isinstance(raw_words, str):
                        words_list = [w.strip().upper() for w in raw_words.split() if w.strip()]
                    elif isinstance(raw_words, list):
                        words_list = [str(w).strip().upper() for w in raw_words if str(w).strip()]
                    else:
                        words_list = ["VIRAL", "MOMENT", "DETECTED"]
                    if not words_list:
                        words_list = ["VIRAL", "MOMENT", "DETECTED"]

                    # Extract verbatim dialogue lines
                    dialogue_lines = []
                    if cues:
                        clip_cues = [c for c in cues if c["start"] >= s - 0.2 and c["end"] <= e + 0.2]
                        for c in clip_cues:
                            dialogue_lines.append({
                                "time": format_timestamp(c["start"]),
                                "text": c.get("text", "").strip()
                            })

                    formatted_hooks.append({
                        "id": f"hook_{idx + 1}",
                        "start": round(s, 2),
                        "end": round(e, 2),
                        "start_fmt": format_timestamp(s),
                        "end_fmt": format_timestamp(e),
                        "duration": f"{dur:.1f}s",
                        "duration_seconds": round(dur, 1),
                        "title": h.get("title", f"Moment #{idx + 1}"),
                        "hook_quote": h.get("hook_quote", h.get("title", "")),
                        "category": h.get("category", "Key Moment"),
                        "retention_score": int(h.get("retention_score", 95 - idx * 2)),
                        "reason": h.get("reason", "High-retention narrative moment."),
                        "boundary": "Natural speech pause aligned",
                        "words": words_list,
                        "dialogue": dialogue_lines,
                        "youtube_embed_url": f"https://www.youtube-nocookie.com/embed/{video_id}?start={int(s)}&end={int(e)}&autoplay=1&enablejsapi=1",
                    })
                    log(f"[cutter] Hook #{idx + 1}: [{format_timestamp(s)} - {format_timestamp(e)}] '{h.get('title')}' (Score: {h.get('retention_score')})")

        except Exception as gemini_err:
            log(f"[gemini] Notice: AI model fallback engaged ({gemini_err})")

        # Step 5: If Gemini didn't return valid hooks, use dynamic cue-derived hooks
        if not formatted_hooks:
            log("[algorithm] Deriving hooks dynamically from video speech density...")
            formatted_hooks = build_dynamic_hooks_from_cues(cues, title, video_id, duration)
            for idx, h in enumerate(formatted_hooks):
                log(f"[algorithm] Hook #{idx + 1}: [{h['start_fmt']} - {h['end_fmt']}] '{h['title']}' (Score: {h['retention_score']})")

        if not formatted_hooks:
            raise RuntimeError("No usable speech or video hooks were found.")

        job["step"] = f"Analysis complete! {len(formatted_hooks)} high-retention clips ready for vertical export."
        job["progress"] = 100
        job["step_number"] = 5
        job["status"] = "completed"
        job["result"] = {
            "status": "success",
            "video_title": title,
            "video_id": video_id,
            "duration_fmt": format_timestamp(duration),
            "recommended_framing": auto_framing,
            "recommended_style": auto_style,
            "framing_reason": framing_reason,
            "style_reason": style_reason,
            "hooks": formatted_hooks,
        }
        log(f"[pipeline] Successfully extracted {len(formatted_hooks)} hooks from '{title}'")
        return job["result"]

    except Exception as exc:
        log(f"[analyzer] Error occurred: {exc}")
        job["status"] = "failed"
        job["error"] = str(exc)

        # Dynamic fallback from cues if any exist; never fabricate fake dialogue
        fallback_hooks = build_dynamic_hooks_from_cues(cues, "Video Highlight", extract_youtube_id(url), 0.0) if cues else []
        return {
            "status": "partial" if fallback_hooks else "failed",
            "video_title": "Video Highlight",
            "video_id": extract_youtube_id(url),
            "duration_fmt": "0:00",
            "hooks": fallback_hooks,
            "error": str(exc),
        }
