"""CLI for condensed-story parts. Exports files independently of posting."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from ..core.downloader import expand_and_download, is_url
from ..core.queue import QueueError
from ..core.source_text import load_caption_file
from ..config import SUB_LANG, SUB_STYLE
from ..core.subtitles import STYLES
from .options import PartsOptions
from .pipeline import PartsPipeline


def build_parser(prog: str = "Hooky-parts") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog, description="Export condensed-story or number-only chronological vertical parts.")
    parser.add_argument("source", help="Local video file or single video URL")
    parser.add_argument("--mode", choices=("story", "number-only"),
                        default=os.environ.get("PARTS_MODE", "story").strip().lower(),
                        help="story condenses with AI; number-only finds funny moments and burns only their numbers")
    parser.add_argument("--part-seconds", type=float,
                        default=float(os.environ.get("PARTS_TARGET_SECONDS", "60")),
                        help="Target part length, adjusted near speech/scene boundaries (default: 60)")
    parser.add_argument("--background", choices=("blur", "plain"),
                        default=os.environ.get("PARTS_BACKGROUND", "blur").strip().lower(),
                        help="Preserve full source picture over a blurred or plain background")
    parser.add_argument("--out", type=Path, help="Export directory (default: output/parts/SOURCE_ID)")
    parser.add_argument("--caption-file", type=Path, help="Explicit timestamped VTT/SRT evidence")
    parser.add_argument("--subtitle-source", choices=("auto", "source", "whisper"), default=None)
    parser.add_argument("--sub-style", choices=tuple(STYLES), default=SUB_STYLE)
    parser.add_argument("--sub-lang", default=SUB_LANG, help="Whisper language, for example en")
    parser.add_argument("--no-subs", action="store_true", help="Export without burned-in subtitles")
    parser.add_argument("--labels", action="store_true", help="Burn a Part N / Total label into the upper padding")
    parser.add_argument("--dry-run", action="store_true", help="Save story_plan.json without rendering")
    parser.add_argument("--profile", action="store_true", help="Save stage timings in profile.json")
    return parser


def main(argv: list[str] | None = None, *, prog: str = "Hooky-parts") -> int:
    try:
        args = build_parser(prog=prog).parse_args(argv)
        options = PartsOptions(mode=args.mode, target_seconds=args.part_seconds, background=args.background,
                               out=args.out, no_subs=args.no_subs, sub_style=args.sub_style,
                               sub_lang=args.sub_lang, labels=args.labels,
                               dry_run=args.dry_run, profile=args.profile)
        if args.subtitle_source:
            options.subtitle_source = args.subtitle_source
        transcript = None
        if args.caption_file:
            transcript = load_caption_file(args.caption_file)
            if not transcript:
                raise ValueError(f"No usable captions in {args.caption_file}")
        context, source_url = None, ""
        if is_url(args.source):
            entry = {"url": args.source, "line_no": 1, "clips": None, "min_len": None,
                     "max_len": None, "sub_style": None, "no_subs": None}
            videos, _ = expand_and_download(
                entry, {"clips": 0, "min": 15, "max": 60, "max_sources": 1, "make_analysis": False})
            if not videos:
                return 2
            source = videos[0]
            video, source_url = source["path"], source["url"] or args.source
            transcript = transcript or source["source_transcript"]
            context = {key: source[key] for key in ("title", "description", "uploader")}
        else:
            video = Path(args.source)
            if not video.is_file():
                raise ValueError(f"Video file not found: {video}")
        PartsPipeline(options).run(video, source_url=source_url, context=context,
                                   source_transcript=transcript)
        return 0
    except (ValueError, OSError, RuntimeError, QueueError, subprocess.SubprocessError) as exc:
        print(f"[parts] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

