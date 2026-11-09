import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from hooky.web import rendering


@pytest.fixture
def media_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(rendering, "OUTPUT_DIR", tmp_path / "output")
    commands = []
    async def download(*args, **kwargs):
        commands.append(args)
        Path(args[args.index("-o") + 1]).write_bytes(b"source" * 500)
        process = Mock(returncode=0)
        process.communicate = AsyncMock(return_value=(b"", b""))
        return process
    monkeypatch.setattr(rendering.asyncio, "create_subprocess_exec", download)
    encoder = Mock(side_effect=lambda **kwargs: kwargs["out_path"].write_bytes(b"encoded" * 500))
    monkeypatch.setattr(rendering, "cut_clip", encoder)
    return commands, encoder


def clip_payload():
    return {"url": "https://example.com/video", "start": 10.35, "end": 13.6,
            "style": "gold", "framing": "fit", "title": "Shared Title", "video_title": "Source"}


def test_fractional_offsets_overlap_captions_and_unique_atomic_outputs(tmp_path, monkeypatch, media_boundary):
    commands, encoder = media_boundary
    ass = Mock(return_value="ASS test")
    words = Mock(return_value=[{"word": "test", "start": 0, "end": 1}])
    monkeypatch.setattr(rendering, "caption_words", words)
    monkeypatch.setattr(rendering, "build_ass", ass)
    cues = [{"start": 9, "end": 11, "text": "leading"}, {"start": 12, "end": 15, "text": "trailing"}]
    first = asyncio.run(rendering.render_clip(clip_payload(), {"id": "render_a"}, tmp_path / "cache-a", cues))
    second = asyncio.run(rendering.render_clip(clip_payload(), {"id": "render_b"}, tmp_path / "cache-b", cues))
    assert first != second
    assert (rendering.OUTPUT_DIR / first).exists() and (rendering.OUTPUT_DIR / second).exists()
    assert commands[0][commands[0].index("--download-sections") + 1] == "*10-14.6"
    assert "--force-keyframes-at-cuts" in commands[0]
    assert encoder.call_args.kwargs["start"] == pytest.approx(0.35)
    assert encoder.call_args.kwargs["end"] == pytest.approx(3.6)
    assert encoder.call_args.kwargs["out_path"].is_relative_to(tmp_path / "cache-b")
    relative = words.call_args.args[0]
    assert relative[0]["start"] == 0 and relative[0]["end"] == pytest.approx(0.65)
    assert relative[1]["start"] == pytest.approx(1.65) and relative[1]["end"] == pytest.approx(3.25)


@pytest.mark.parametrize("failure", ["download", "encode", "empty"])
def test_render_failure_does_not_publish_output(tmp_path, monkeypatch, media_boundary, failure):
    if failure == "download":
        process = Mock(returncode=1)
        process.communicate = AsyncMock(return_value=(b"", b"download refused"))
        monkeypatch.setattr(rendering.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    elif failure == "encode":
        monkeypatch.setattr(rendering, "cut_clip", Mock(side_effect=RuntimeError("Encoder failed")))
    else:
        monkeypatch.setattr(rendering, "cut_clip", Mock())
    with pytest.raises(RuntimeError):
        asyncio.run(rendering.render_clip(clip_payload(), {"id": "render_failed"}, tmp_path / "cache", []))
    assert list(rendering.OUTPUT_DIR.rglob("*.mp4")) == []


def test_make_uses_same_render_path_without_paid_analysis(tmp_path, monkeypatch):
    prepare = AsyncMock(return_value=({"title": "Source", "duration": 50}, [{"start": 0, "end": 50, "text": "Source text"}]))
    monkeypatch.setattr(rendering, "prepare_source", prepare)
    monkeypatch.setattr(rendering, "extract_ranked_scenes", Mock(return_value=[{"title": "Chosen", "start": 1.25, "end": 20.5}]))
    render = AsyncMock(return_value="Source/reel.mp4")
    monkeypatch.setattr(rendering, "render_clip", render)
    job = {"id": "make_a", "status": "running"}
    outputs = asyncio.run(rendering.make_url({"target": "https://example.com/video", "style": "gold", "framing": "center"}, job, tmp_path))
    assert outputs == ["Source/reel.mp4"]
    assert render.call_args.args[0]["start"] == 1.25
    assert render.call_args.args[0]["source_duration"] == 50
    assert job["status"] == "running"
    monkeypatch.setattr(rendering, "extract_ranked_scenes", Mock(return_value=[]))
    with pytest.raises(RuntimeError, match="No usable"):
        asyncio.run(rendering.make_url({"target": "https://example.com/empty"}, job, tmp_path))


def test_manual_caption_track_is_downloaded_without_playlist_traversal(tmp_path, monkeypatch):
    from hooky.web import analyzer
    monkeypatch.setattr(analyzer, "yt_dlp_auth_args", lambda: [])
    commands = []
    async def manual_captions(*args, **kwargs):
        commands.append(args)
        prefix = Path(args[args.index("-o") + 1])
        prefix.with_name(prefix.name + ".en.vtt").write_text("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nManual dialogue\n", encoding="utf-8")
        process = Mock(returncode=0)
        process.communicate = AsyncMock(return_value=(b"", b""))
        return process
    monkeypatch.setattr(analyzer.asyncio, "create_subprocess_exec", manual_captions)
    path = asyncio.run(analyzer.download_captions("https://example.com/video?list=playlist", tmp_path / "manual"))
    assert "Manual dialogue" in path.read_text(encoding="utf-8")
    assert "--write-subs" in commands[0] and "--write-auto-sub" in commands[0] and "--no-playlist" in commands[0]
