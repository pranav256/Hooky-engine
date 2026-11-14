"""Offline, opt-in check of the real Studio ASS/FFmpeg publishing path."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("HOOKY_MEDIA_SMOKE") != "1", reason="Set HOOKY_MEDIA_SMOKE=1 for synthetic FFmpeg checks"),
]


@pytest.fixture
def local_media(tmp_path, monkeypatch):
    if not all(shutil.which(binary) for binary in ("ffmpeg", "ffprobe")):
        pytest.skip("Synthetic media smoke needs FFmpeg and FFprobe on PATH")
    from hooky.core import subtitles

    monkeypatch.setattr(subtitles, "FONT_OVERRIDE", "")
    monkeypatch.delenv("SUB_FONT_SIZE", raising=False)
    monkeypatch.delenv("SUB_MAX_WORDS", raising=False)
    fixture = tmp_path / "local-source.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=640x360:r=30:d=3",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-vf",
        "drawbox=color=red:t=fill:enable='lt(t,0.5)'", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(fixture),
    ], check=True, capture_output=True, timeout=30)
    return fixture


def intercept_download(monkeypatch, rendering, fixture: Path, *, broken=False):
    class Download:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def local_download(*args, **kwargs):
        # Only replace the remote yt-dlp boundary; captions and final encoding stay real.
        assert "yt_dlp" in args
        destination = Path(args[args.index("-o") + 1])
        start, end = map(float, args[args.index("--download-sections") + 1].lstrip("*").split("-"))
        if broken:
            destination.write_bytes(b"invalid media" * 200)
        else:
            subprocess.run([
                "ffmpeg", "-y", "-v", "error", "-ss", str(start), "-i", str(fixture), "-t", str(end - start),
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(destination),
            ], check=True, capture_output=True, timeout=30)
        return Download()

    monkeypatch.setattr(rendering.asyncio, "create_subprocess_exec", local_download)


@pytest.mark.parametrize("framing", ["center", "fit"])
def test_real_selective_render_fractional_timing_and_captions(tmp_path, monkeypatch, local_media, framing):
    from hooky.web import rendering

    outputs = tmp_path / "output"
    monkeypatch.setattr(rendering, "OUTPUT_DIR", outputs)
    intercept_download(monkeypatch, rendering, local_media)
    payload = {"url": "https://www.youtube.com/watch?v=fixture0001", "start": 0.75, "end": 2.15,
               "style": "clean", "framing": framing, "title": "Synthetic clip", "video_title": "Local fixture"}
    cues = [{"start": 0.5, "end": 1.1, "text": "Overlapping start"},
            {"start": 1.9, "end": 2.5, "text": "Overlapping end"}]
    cache = tmp_path / "cache"
    relative = asyncio.run(rendering.render_clip(payload, {"id": f"smoke-{framing}"}, cache, cues))
    output = outputs / relative
    probe = json.loads(subprocess.run([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output),
    ], check=True, capture_output=True, text=True, timeout=15).stdout)
    video = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1080, 1920)
    assert abs(float(video["duration"]) - 1.4) <= 1 / 30 + 0.005
    # Before 0.5s the source is red. The requested 0.75s start must encode blue.
    rgb = subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(output), "-frames:v", "1", "-vf", "scale=1:1",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ], check=True, capture_output=True, timeout=15).stdout
    assert rgb[2] > rgb[0] + 50, "Fractional start encoded earlier source frames"
    # The clean preset fades in for 60ms, so inspect a visible caption frame.
    caption_pixels = subprocess.run([
        "ffmpeg", "-v", "error", "-ss", "0.1", "-i", str(output), "-frames:v", "1", "-vf", "crop=1080:400:0:1100,scale=270:100",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ], check=True, capture_output=True, timeout=15).stdout
    assert sum(red > 100 and green > 100 for red, green in zip(caption_pixels[::3], caption_pixels[1::3])) > 20, "Caption glyphs were not burned into the blue fixture"
    ass = next(cache.glob("*.ass")).read_text(encoding="utf-8")
    assert "Overlapping" in ass and "start" in ass and "end" in ass
    assert not list(outputs.rglob("*.tmp.mp4"))


def test_invalid_download_never_publishes_final_output(tmp_path, monkeypatch, local_media):
    from hooky.web import rendering

    outputs = tmp_path / "output"
    monkeypatch.setattr(rendering, "OUTPUT_DIR", outputs)
    intercept_download(monkeypatch, rendering, local_media, broken=True)
    payload = {"url": "https://www.youtube.com/watch?v=fixture0001", "start": 0.75, "end": 2.15,
               "style": "clean", "framing": "center", "title": "Broken clip", "video_title": "Local fixture"}
    with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
        asyncio.run(rendering.render_clip(payload, {"id": "smoke-failure"}, tmp_path / "cache", []))
    assert not list(outputs.rglob("*.mp4")), "Failed media must not be published"


def test_real_render_burns_caption_edits(tmp_path, monkeypatch, local_media):
    from hooky.web import rendering
    monkeypatch.setattr(rendering, "OUTPUT_DIR", tmp_path / "output")
    intercept_download(monkeypatch, rendering, local_media)
    cues = [{"start": 0.5, "end": 2.5, "text": "Original wording"}]
    payload = {"url": "https://www.youtube.com/watch?v=fixture0001", "start": 0.75, "end": 2.15,
               "style": "clean", "framing": "center", "title": "Edited clip", "video_title": "Local fixture",
               "caption_edits": [{"start": 0.5, "end": 2.5, "text": "Corrected caption"}]}
    cache = tmp_path / "cache"
    relative = asyncio.run(rendering.render_clip(payload, {"id": "smoke-edits"}, cache, cues))
    ass = next(cache.glob("*.ass")).read_text(encoding="utf-8")
    assert "Corrected" in ass and "Original" not in ass
    assert cues[0]["text"] == "Original wording"
    output = rendering.OUTPUT_DIR / relative
    assert output.exists() and output.stat().st_size > 1000


def test_cancelled_render_never_publishes_staged_output(tmp_path, monkeypatch, local_media):
    from hooky.web import rendering

    outputs = tmp_path / "output"
    monkeypatch.setattr(rendering, "OUTPUT_DIR", outputs)
    intercept_download(monkeypatch, rendering, local_media)
    encoded, release = threading.Event(), threading.Event()
    real_cut_clip = rendering.cut_clip

    def observe_completed_encode(**kwargs):
        result = real_cut_clip(**kwargs)
        encoded.set()
        assert release.wait(10), "Test did not release the completed encoder"
        return result

    monkeypatch.setattr(rendering, "cut_clip", observe_completed_encode)
    payload = {"url": "https://www.youtube.com/watch?v=fixture0001", "start": 0.75, "end": 2.15,
               "style": "clean", "framing": "center", "title": "Cancelled clip", "video_title": "Local fixture"}

    async def cancel_before_publish():
        task = asyncio.create_task(rendering.render_clip(payload, {"id": "smoke-cancel"}, tmp_path / "cache", []))
        try:
            assert await asyncio.to_thread(encoded.wait, 15), "Real encoder did not finish"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()

    asyncio.run(cancel_before_publish())
    assert list((tmp_path / "cache").glob("reel_*.mp4")), "The encoder must have produced a real staged artifact"
    assert not list(outputs.rglob("*.mp4")), "Cancelled work must not publish a completed staged artifact"
