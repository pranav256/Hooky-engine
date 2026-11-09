"""Low-spec friendly constants and ffmpeg presets for an i3 / 4GB RAM machine."""
from __future__ import annotations

import os

# --- Whisper (speech-to-text) ---------------------------------------------
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "tiny")       # tiny|base|small
WHISPER_COMPUTE = os.environ.get("WHISPER_COMPUTE", "int8").strip().lower() or "int8"   # int8 = fast on old CPUs

# --- ffmpeg CPU/RAM friendly settings ---------------------------------------
NGROUPS = 48            # cap GOP memory usage
MAX_MUX_QUEUE = "4M"    # keep muxing RAM small
try:
    FFMPEG_THREADS = max(1, int(os.environ.get("FFMPEG_THREADS", "2")))
except ValueError:
    FFMPEG_THREADS = 2
try:
    WHISPER_THREADS = max(1, int(os.environ.get("WHISPER_THREADS", "2")))
except ValueError:
    WHISPER_THREADS = 2

# --- Output quality tier -----------------------------------------------------
# QUALITY=highest -> crf 15 / medium preset / 256k audio (Instagram master)
# QUALITY=high    -> crf 17 / medium preset / 192k audio
# QUALITY=low     -> crf 21 / veryfast / 128k (low-spec fallback)
# X264_PRESET env var still overrides the preset in every tier.  Instagram
# will transcode the upload, so the goal here is to give it a clean
# 1080x1920 H.264 source with as little avoidable loss as practical.
QUALITY = os.environ.get("QUALITY", "highest").strip().lower()

if QUALITY in {"highest", "maximum", "max"}:
    X264_CRF = "15"
    # At a fixed CRF, slow mainly saves a little file size at a large time
    # cost. Medium keeps nearly the same visual quality and is much faster.
    X264_PRESET = os.environ.get("X264_PRESET", "medium").strip().lower()
    AUDIO_BITRATE = "256k"
elif QUALITY == "high":
    X264_CRF = "17"
    X264_PRESET = os.environ.get("X264_PRESET", "medium").strip().lower()
    AUDIO_BITRATE = "192k"
else:
    X264_CRF = "21"
    X264_PRESET = os.environ.get("X264_PRESET", "veryfast").strip().lower()
    AUDIO_BITRATE = "128k"


def lowspec_encode_args() -> list[str]:
    """Extra ffmpeg args tuned for 4GB RAM / old i3: bounded threads, small queues."""
    return [
        "-threads", str(FFMPEG_THREADS),
        "-x264-params", f"scenecut=0:keyint={NGROUPS}",
        "-max_muxing_queue_size", MAX_MUX_QUEUE,
    ]


def lowspec_input_args() -> list[str]:
    """Place before -i: bound decoder and filter pools as well as the encoder."""
    return ["-threads", str(FFMPEG_THREADS), "-filter_threads", str(FFMPEG_THREADS),
            "-filter_complex_threads", str(FFMPEG_THREADS)]

