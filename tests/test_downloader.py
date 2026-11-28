"""Downloader cache parsing regressions; no network required."""
import json
from pathlib import Path

from hooky.core import downloader


def test_parse_links_skips_both_done_markers(tmp_path: Path) -> None:
    links = tmp_path / "links.txt"
    links.write_text(
        "DONE https://example.test/old\n"
        "# DONE https://example.test/older\n"
        "https://example.test/new\n",
        encoding="utf-8",
    )

    entries = downloader.parse_links_file(links)

    assert [entry["url"] for entry in entries] == ["https://example.test/new"]


def test_auth_error_has_actionable_help() -> None:
    error = RuntimeError("Sign in to confirm you're not a bot; use --cookies-from-browser")
    assert downloader.is_youtube_auth_error(error)
    assert "YTDLP_COOKIES_FROM_BROWSER" in downloader._auth_failure(error).args[0]


def test_caption_sources_skip_full_analysis_proxy(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(downloader, "make_analysis_twin",
                        lambda path: (_ for _ in ()).throw(AssertionError("proxy")))
    videos = [{"path": video, "source_transcript": [{"start": 0, "end": 1,
                                                       "text": "hello"}]}]
    downloader._attach_analysis(videos)
    assert videos[0]["analysis"] is None


def test_cache_lookup_ignores_list_json(tmp_path: Path, monkeypatch) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    monkeypatch.setattr(downloader, "DOWNLOADS_DIR", downloads)

    # Transcript/tool files can be valid JSON lists and must not crash cache
    # lookup, even when they share the downloads directory.
    (downloads / "words.json").write_text(json.dumps([{"word": "hello"}]))
    assert downloader._already_downloaded("https://example.test/video") is None


def test_cache_lookup_rejects_lower_quality_cache(tmp_path: Path, monkeypatch) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    monkeypatch.setattr(downloader, "DOWNLOADS_DIR", downloads)
    video = downloads / "abc.mp4"
    video.write_bytes(b"video")
    (downloads / "abc.json").write_text(json.dumps({
        "url": "https://example.test/video",
        "file": str(video),
        "requested_height": 1080,
    }))

    assert downloader._already_downloaded(
        "https://example.test/video", min_requested_height=1440) is None
    assert downloader._already_downloaded(
        "https://example.test/video", min_requested_height=1080) == video


def test_cache_lookup_returns_matching_metadata(tmp_path: Path, monkeypatch) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    monkeypatch.setattr(downloader, "DOWNLOADS_DIR", downloads)
    video = downloads / "abc.mp4"
    video.write_bytes(b"video")
    (downloads / "abc.json").write_text(json.dumps({
        "url": "https://example.test/video",
        "file": str(video),
    }))

    assert downloader._already_downloaded("https://example.test/video") == video

