"""Options dataclass — every knob of the pipeline in one plain object.

Both the CLI and library users build this; `PipelineOptions.from_args(args)`
bridges from the argparse namespace.
"""
from __future__ import annotations

import math
from argparse import Namespace
from dataclasses import dataclass
from pathlib import Path

from .config import (AUTO_CLIP_MODE, MAKE_COVERS, REELS_MAX_DURATION, SUB_LANG,
                     SUB_STYLE, SUBTITLE_SOURCE)
from .core.subtitles import STYLES


@dataclass
class PipelineOptions:
    """The remaining knobs for lean Instagram clip production."""
    # clips=0 lets the content determine the count; duration mode is optional.
    clips: int = 0
    auto_mode: str = AUTO_CLIP_MODE
    min_len: int = 15
    max_len: int = 60
    # --- output ---
    out: Path | None = None
    # --- behaviour toggles ---
    no_upload: bool = False
    dry_run: bool = False
    no_captions: bool = False
    no_subs: bool = False
    sub_style: str = SUB_STYLE
    framing: str = "auto"
    sub_lang: str | None = SUB_LANG
    subtitle_source: str = SUBTITLE_SOURCE
    make_covers: bool = MAKE_COVERS
    profile: bool = False

    def __post_init__(self) -> None:
        if self.auto_mode not in {"content", "duration"}:
            raise ValueError("auto_mode must be content or duration")
        if self.sub_style not in STYLES:
            raise ValueError(
                f"Unknown sub_style '{self.sub_style}'. Options: {', '.join(STYLES)}")
        if self.framing not in {"auto", "center", "fit"}:
            raise ValueError("framing must be auto, center, or fit")
        if self.subtitle_source not in {"auto", "source", "whisper"}:
            raise ValueError("subtitle_source must be auto, source, or whisper")

    @classmethod
    def from_args(cls, args: Namespace) -> "PipelineOptions":
        """Build options from the CLI argparse namespace."""
        return cls(
            clips=0 if str(args.clips).lower() in ("auto", "0") else args.clips,
            auto_mode=getattr(args, "auto_mode", AUTO_CLIP_MODE),
            min_len=args.min_len,
            max_len=args.max_len,
            out=args.out,
            no_upload=args.no_upload,
            dry_run=args.dry_run,
            no_captions=getattr(args, "no_captions", False),
            no_subs=getattr(args, "no_subs", False),
            sub_style=getattr(args, "sub_style", SUB_STYLE),
            framing=getattr(args, "framing", "auto"),
            sub_lang=getattr(args, "sub_lang", SUB_LANG) or SUB_LANG,
            subtitle_source=(getattr(args, "subtitle_source", None) or SUBTITLE_SOURCE),
            make_covers=(MAKE_COVERS if getattr(args, "make_covers", None) is None
                         else args.make_covers),
            profile=getattr(args, "profile", False),
        )


@dataclass
class PartsOptions:
    """Options for export-only chronological story parts."""

    mode: str = "story"
    target_seconds: float = 60
    background: str = "blur"
    subtitle_source: str = SUBTITLE_SOURCE
    sub_style: str = "hormozi"
    sub_lang: str | None = None
    no_subs: bool = False
    labels: bool = False
    dry_run: bool = False
    profile: bool = False
    out: Path | None = None

    def __post_init__(self):
        if self.mode not in {"story", "number-only"}:
            raise ValueError("Parts mode must be story or number-only")
        if not math.isfinite(self.target_seconds) or not 1 <= self.target_seconds <= REELS_MAX_DURATION:
            raise ValueError(f"Part length must be between 1 and {REELS_MAX_DURATION} seconds")
        if self.background not in {"blur", "plain"}:
            raise ValueError("Parts background must be blur or plain")
        if self.subtitle_source not in {"auto", "source", "whisper"}:
            raise ValueError("Subtitle source must be auto, source, or whisper")
        if self.sub_style not in STYLES:
            raise ValueError(f"Unknown subtitle style: {self.sub_style}")
        if self.mode == "number-only":
            # Gemini still selects the moments; this controls the output layer:
            # no Whisper, subtitles, or generated text—only a numeric marker.
            self.no_subs = True
            self.labels = True

