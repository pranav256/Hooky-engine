"""Offline coverage for shared media utilities."""
import pytest

from hooky.core.utils import validate_video_url


@pytest.mark.parametrize("url", [
    "https://:443/video.mp4",
    "http://:80",
    "https://@/video.mp4",
    "https://user@/video.mp4",
    "https://user:password@:443/video.mp4",
])
def test_video_url_rejects_authority_without_hostname(url):
    assert validate_video_url(url) is False


@pytest.mark.parametrize("url", [
    "https://example.com/video.mp4",
    "http://localhost:8000/video.mp4",
    "https://127.0.0.1:443/video.mp4",
    "http://[::1]:8000/video.mp4",
    "https://user:password@example.com/video.mp4",
    "  https://example.com/video.mp4  ",
])
def test_video_url_accepts_http_sources_with_hostname(url):
    assert validate_video_url(url) is True
