"""An export-only story workflow with durable plans and per-part resume."""
from __future__ import annotations

import math
from pathlib import Path

from ..config import GEMINI_FALLBACK_MODELS, GEMINI_MODEL, OUTPUT_DIR
from ..core.checkpoints import (atomic_json, digest, file_revision, load_checkpoint,
                                save_checkpoint, source_id)
from ..core.highlights import find_highlights
from ..core.lowspec import WHISPER_COMPUTE, WHISPER_MODEL
from ..core.logbook import PipelineProfile
from ..core.queue import file_lock
from ..core.source_text import (caption_words, load_caption_file,
                                 sidecar_caption_file)
from ..core.subtitles import build_ass, transcribe, transcribe_source
from ..core.utils import get_duration
from ..options import PartsOptions
from .planner import normalize_story, plan_parts, remap_words, select_story
from .renderer import render_part


class PartsPipeline:
    def __init__(self, options: PartsOptions | None = None):
        self.options = options or PartsOptions()
        self.state = {"select_story": select_story, "render_part": render_part,
                      "find_highlights": find_highlights,
                      "transcribe_source": transcribe_source, "transcribe": transcribe}

    def run(self, video: Path, *, source_url: str = "", context: dict | None = None,
            source_transcript: list[dict] | None = None) -> dict:
        video = Path(video)
        directory = self.options.out or OUTPUT_DIR / "parts" / source_id(video, source_url)
        with file_lock(directory / ".parts.lock", timeout=0):
            return self._run(video, directory, source_url, context, source_transcript)

    def _run(self, video: Path, directory: Path, source_url: str, context: dict | None,
             source_transcript: list[dict] | None) -> dict:
        options = self.options
        profile = PipelineProfile(options.profile)
        with profile.stage("probe"):
            duration = get_duration(video)
        if not math.isfinite(duration) or duration < 1:
            raise ValueError("Source must have at least one second of usable video")
        if options.mode == "number-only":
            return self._run_funny_number_only(
                video, directory, source_url, duration, profile, context,
                source_transcript)
        cache = directory / ".checkpoints"
        original_words = []
        transcript = source_transcript or load_caption_file(sidecar_caption_file(video))
        evidence = "source captions"
        with profile.stage("transcript"):
            if not transcript:
                if options.subtitle_source == "source" and not options.no_subs:
                    raise ValueError("Source captions were requested but none were available")
                print("[parts] No source captions; transcribing speech in cached five-minute windows")
                transcript, original_words = self.state["transcribe_source"](
                    video, duration, cache / "speech", language=options.sub_lang)
                evidence = "Whisper transcript"
        signature = {"version": 1, "source": file_revision(video), "transcript": digest(transcript),
                     "context": context, "model": GEMINI_MODEL, "fallbacks": GEMINI_FALLBACK_MODELS}
        selection_cache = cache / digest(signature)
        with profile.stage("story_selection"):
            story = load_checkpoint(selection_cache / "story.json", signature)
            if not isinstance(story, list) or not story:
                story = self.state["select_story"](transcript, video, duration,
                                                   selection_cache, context=context)
                story = normalize_story(story, duration)
                save_checkpoint(selection_cache / "story.json", signature, story)
            else:
                print("[parts] Reusing selected story")
                story = normalize_story(story, duration)
        with profile.stage("subtitle_timing"):
            if options.subtitle_source == "whisper" and not original_words and not options.no_subs:
                timing_signature = {"source": signature["source"], "story": story,
                                    "model": WHISPER_MODEL, "compute": WHISPER_COMPUTE,
                                    "language": options.sub_lang}
                timing_path = selection_cache / "story_words.json"
                original_words = load_checkpoint(timing_path, timing_signature)
                if not isinstance(original_words, list):
                    original_words = self.state["transcribe"](
                        video, language=options.sub_lang,
                        clip_timestamps=[t for segment in story for t in (segment["start"], segment["end"])])
                    save_checkpoint(timing_path, timing_signature, original_words)
            if not original_words:
                original_words = caption_words(transcript)
            edited_words = remap_words(original_words, story)
        with profile.stage("parts_plan"):
            parts = plan_parts(story, edited_words, options.target_seconds)
        plan = {"version": 1, "source": str(video), "source_url": source_url,
                "source_duration": duration, "selection_evidence": evidence,
                "story_segments": story, "retained_seconds": sum(s["end"] - s["start"] for s in story),
                "target_seconds": options.target_seconds, "background": options.background,
                "parts": parts}
        atomic_json(directory / "story_plan.json", plan)
        print(f"[parts] Retaining {plan['retained_seconds']:.1f}s of {duration:.1f}s "
              f"as {len(parts)} chronological parts")
        if options.dry_run:
            profile.save(directory / "profile.json")
            return plan
        if not options.no_subs and not edited_words:
            raise ValueError("No subtitle words in the retained story; review captions or use --no-subs")
        return self._export(video, directory, plan, original_words, edited_words, profile)

    def _export(self, video: Path, directory: Path, plan: dict,
                original_words: list[dict], edited_words: list[dict],
                profile: PipelineProfile) -> dict:
        options = self.options
        parts = plan["parts"]
        manifest = {**plan, "complete": False, "exports": []}
        atomic_json(directory / "manifest.json", manifest)
        with profile.stage("render"):
            for part in parts:
                path = self.state["render_part"](
                    video, part, [] if options.no_subs else original_words,
                    directory, options, len(parts))
                if not options.no_subs:
                    ass = build_ass(edited_words, part["start"], part["end"], options.sub_style)
                    if ass:
                        path.with_suffix(".ass").write_text(ass, encoding="utf-8")
                manifest["exports"].append({"index": part["index"], "path": str(path),
                                            "duration": part["duration"], "segments": part["segments"]})
                atomic_json(directory / "manifest.json", manifest)
        manifest["complete"] = True
        atomic_json(directory / "manifest.json", manifest)
        profile.save(directory / "profile.json")
        print(f"[parts] Exported {len(parts)} parts to {directory}")
        return manifest

    def _run_funny_number_only(self, video: Path, directory: Path, source_url: str,
                               duration: float, profile: PipelineProfile,
                               context: dict | None,
                               source_transcript: list[dict] | None) -> dict:
        """Gemini selects funny moments; only numeric part labels are rendered."""
        options = self.options
        transcript = source_transcript or load_caption_file(sidecar_caption_file(video))
        evidence = "source captions + Gemini funny-moment selection" if transcript \
            else "visual/audio evidence + Gemini funny-moment selection"
        cache = directory / ".checkpoints"
        selection_signature = {
            "version": 2,
            "source": file_revision(video),
            "transcript": digest(transcript),
            "target_seconds": options.target_seconds,
            "focus": "funny, surprising, absurd, reaction, visual gag, or punchline moments",
        }
        selection_cache = cache / digest(selection_signature)
        with profile.stage("funny_selection"):
            selected = load_checkpoint(selection_cache / "highlights.json", selection_signature)
            if not isinstance(selected, list) or not selected:
                selected = self.state["find_highlights"](
                    video,
                    n_clips=0,
                    clip_min=max(8, min(20, int(options.target_seconds * 0.35))),
                    clip_max=max(10, int(options.target_seconds)),
                    context=context,
                    transcript=transcript,
                    cache_dir=selection_cache,
                    auto_mode="content",
                    focus=selection_signature["focus"],
                )
                if not selected:
                    raise RuntimeError("Gemini found no funny moments to export")
                save_checkpoint(selection_cache / "highlights.json",
                                selection_signature, selected)
            else:
                print("[parts] Reusing Gemini funny-moment selection")
        story = normalize_story([
            {"start": item["start"], "end": item["end"], "role": "funny moment",
             "summary": item.get("reason", "")}
            for item in selected
            if isinstance(item, dict)
        ], duration)
        parts = plan_parts(story, [], options.target_seconds)
        plan = {
            "version": 1,
            "mode": "number-only",
            "source": str(video),
            "source_url": source_url,
            "source_duration": duration,
            "selection_evidence": evidence,
            "story_segments": story,
            "retained_seconds": sum(segment["end"] - segment["start"] for segment in story),
            "target_seconds": options.target_seconds,
            "background": options.background,
            "labels": "numeric part number only",
            "parts": parts,
        }
        directory.mkdir(parents=True, exist_ok=True)
        atomic_json(directory / "story_plan.json", plan)
        print(f"[parts] NUMBER-ONLY: Gemini selected {plan['retained_seconds']:.1f}s "
              f"of funny moments as {len(parts)} parts")
        if options.dry_run:
            profile.save(directory / "profile.json")
            return plan

        return self._export(video, directory, plan, [], [], profile)

