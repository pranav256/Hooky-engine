"""Shared source and encoder defaults for Hooky's three quality tiers."""
from __future__ import annotations

from typing import NamedTuple


class QualityPreset(NamedTuple):
    source_max_height_long: str
    source_max_height_short: str
    x264_preset: str
    x264_crf: str
    audio_bitrate: str


QUALITY_PRESETS = {
    "highest": QualityPreset("1440", "2160", "medium", "15", "256k"),
    "high": QualityPreset("1080", "1440", "medium", "17", "192k"),
    "low": QualityPreset("720", "1080", "veryfast", "21", "128k"),
}


def runtime_quality_preset(quality: str) -> QualityPreset:
    """Resolve a normalized runtime tier, retaining aliases and the low fallback."""
    if quality in {"maximum", "max"}:
        quality = "highest"
    return QUALITY_PRESETS.get(quality, QUALITY_PRESETS["low"])
