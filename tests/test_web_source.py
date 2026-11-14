"""Offline checks of Studio source reuse and concurrent preparation."""
import asyncio
import json
import threading
from unittest.mock import AsyncMock, Mock

import pytest

from hooky.web import analyzer


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setattr(analyzer, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(analyzer.os, "environ", {})
    monkeypatch.setattr(analyzer, "yt_dlp_auth_args", lambda: [])
    monkeypatch.setattr(analyzer, "WHISPER_MODEL", "tiny")
    monkeypatch.setattr(analyzer, "WHISPER_COMPUTE", "int8")
    metadata = Mock(return_value={"title": "Fixture", "duration": 30, "id": "fixture0001"})
    monkeypatch.setattr(analyzer, "get_video_metadata", metadata)

    async def captions(url, prefix, log):
        path = prefix.with_suffix(".en.vtt")
        path.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nUseful source dialogue\n", encoding="utf-8")
        return path

    download = AsyncMock(side_effect=captions)
    monkeypatch.setattr(analyzer, "download_captions", download)
    return metadata, download


def prepare(tmp_path, name="first", url="https://example.com/video", job=None):
    return asyncio.run(analyzer.prepare_source(url, tmp_path / name, job if job is not None else {}))


def test_source_reused_across_workers_without_network_or_transcription(tmp_path, source):
    first = prepare(tmp_path)
    job = {}
    assert prepare(tmp_path, "second", job=job) == first
    assert job["progress"] == 35 and job["step_number"] == 2
    assert "Reusing" in job["step"]
    source[0].assert_called_once()
    source[1].assert_awaited_once()
    assert not list((tmp_path / "second").iterdir())


@pytest.mark.parametrize("setting,value", [
    ("SOURCE_SUB_LANGS", "fr"), ("SUBTITLE_SOURCE", "source"), ("SUB_LANG", "fr"),
    ("WHISPER_MODEL", "small"), ("WHISPER_COMPUTE", "float32"), ("url", "https://example.com/other"),
    ("auth", "chrome"),
])
def test_cache_separates_sources_and_transcription_settings(tmp_path, monkeypatch, source, setting, value):
    prepare(tmp_path)
    if setting.startswith("WHISPER_"):
        monkeypatch.setattr(analyzer, setting, value)
    elif setting == "auth":
        monkeypatch.setattr(analyzer, "yt_dlp_auth_args", lambda: ["--cookies-from-browser", value])
    elif setting != "url":
        monkeypatch.setenv(setting, value)
    prepare(tmp_path, "second", url=value if setting == "url" else "https://example.com/video")
    assert source[0].call_count == source[1].await_count == 2
    assert len(list((analyzer.OUTPUT_DIR / "web_cache").glob("prepared_*.json"))) == 2


@pytest.mark.parametrize("damage", ["invalid_json", "expired", "future", "empty", "wrong_signature", "metadata", "cue", "nan", "overflow"])
def test_bad_or_expired_cache_is_recomputed(tmp_path, source, damage):
    expected = prepare(tmp_path)
    path = next((analyzer.OUTPUT_DIR / "web_cache").glob("prepared_*.json"))
    cached = json.loads(path.read_text(encoding="utf-8"))
    value = cached["value"]
    if damage == "invalid_json":
        path.write_text("broken", encoding="utf-8")
    else:
        if damage == "expired":
            value["checked"] -= 86401
        elif damage == "future":
            value["checked"] += 3600
        elif damage == "empty":
            value["cues"] = []
        elif damage == "wrong_signature":
            cached["signature"]["version"] = 0
        elif damage == "metadata":
            value["metadata"]["duration"] = "unknown"
        elif damage == "cue":
            value["cues"] = [{"start": 3, "end": 1, "text": "Bad timing"}]
        elif damage == "nan":
            value["cues"][0]["start"] = float("nan")
        elif damage == "overflow":
            value["metadata"]["duration"] = 10 ** 1000
        path.write_text(json.dumps(cached), encoding="utf-8")
    assert prepare(tmp_path, "second") == expected
    assert source[0].call_count == source[1].await_count == 2


def test_empty_source_is_not_cached_as_success(tmp_path, monkeypatch, source):
    monkeypatch.setenv("SUBTITLE_SOURCE", "source")
    source[1].side_effect = None
    source[1].return_value = None
    assert prepare(tmp_path)[1] == []
    assert prepare(tmp_path, "second")[1] == []
    assert source[1].await_count == 2
    assert not list(analyzer.OUTPUT_DIR.rglob("prepared_*.json"))


def test_whisper_fallback_reused_and_language_respected(tmp_path, monkeypatch, source):
    from hooky.core import subtitles
    monkeypatch.setenv("SUBTITLE_SOURCE", "whisper")
    monkeypatch.setenv("SUB_LANG", "fr")
    transcribe = Mock(return_value=[{"start": 1, "end": 2, "word": "Bonjour."}])
    monkeypatch.setattr(subtitles, "transcribe", transcribe)

    async def audio(*args, **kwargs):
        from pathlib import Path
        Path(args[args.index("-o") + 1]).write_bytes(b"audio" * 3000)
        return Mock(returncode=0, communicate=AsyncMock(return_value=(b"", b"")))

    download = AsyncMock(side_effect=audio)
    monkeypatch.setattr(analyzer.asyncio, "create_subprocess_exec", download)
    assert prepare(tmp_path)[1] == [{"start": 1, "end": 2, "text": "Bonjour."}]
    assert prepare(tmp_path, "second")[1][0]["text"] == "Bonjour."
    assert transcribe.call_args.kwargs["language"] == "fr"
    transcribe.assert_called_once()
    download.assert_awaited_once()
    source[1].assert_not_awaited()


def test_cache_save_failure_preserves_prepared_source(tmp_path, monkeypatch, source):
    monkeypatch.setattr(analyzer, "save_checkpoint", Mock(side_effect=OSError("Disk full")))
    assert prepare(tmp_path)[1][0]["text"] == "Useful source dialogue"


def test_metadata_and_captions_start_concurrently(tmp_path, monkeypatch, source):
    captions_started = threading.Event()
    metadata = source[0].return_value

    def probe(url):
        assert captions_started.wait(2), "Metadata blocked caption retrieval"
        return metadata

    original_captions = source[1].side_effect

    async def captions(*args):
        captions_started.set()
        return await original_captions(*args)

    monkeypatch.setattr(analyzer, "get_video_metadata", probe)
    source[1].side_effect = captions
    assert prepare(tmp_path)[1]


def test_normalized_caption_track_keeps_language_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(analyzer, "yt_dlp_auth_args", lambda: [])

    async def download(*args, **kwargs):
        from pathlib import Path
        prefix = Path(args[args.index("-o") + 1])
        prefix.with_suffix(".vtt").write_text(prefix.name, encoding="utf-8")
        return Mock(returncode=0, communicate=AsyncMock(return_value=(b"", b"")))

    process = AsyncMock(side_effect=download)
    monkeypatch.setattr(analyzer.asyncio, "create_subprocess_exec", process)
    url = "https://www.youtube.com/watch?v=fixture0001"
    first = asyncio.run(analyzer.download_captions(url, tmp_path / "english"))
    second = asyncio.run(analyzer.download_captions(url, tmp_path / "french"))
    assert first != second
    assert first.read_text() == "english" and second.read_text() == "french"
    assert asyncio.run(analyzer.download_captions(url, tmp_path / "french")) == second
    assert process.await_count == 2
