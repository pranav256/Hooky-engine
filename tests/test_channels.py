"""Offline discovery tests: every mutable path is temporary, all network mocked."""
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from hooky.core import channels

_REAL_ENRICH = channels._enrich_shortlist
_REAL_FETCH = channels._fetch_channel_entries


@pytest.fixture(autouse=True)
def isolated_discovery(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(channels, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(channels, "SEEN_FILE", tmp_path / "output" / "channel_history.json")
    monkeypatch.setattr(channels, "CHANNEL_SCAN_MAX", 0)
    monkeypatch.setenv("CHANNEL_CACHE_HOURS", "24")
    monkeypatch.setattr(channels, "_fetch_channel_entries",
                        lambda *args: pytest.fail("unexpected channel network request"))
    monkeypatch.setattr(channels, "_enrich_shortlist", lambda entries: entries)


def video(video_id, views=1000, title=None, **metadata):
    return {"id": video_id, "title": title or video_id, "view_count": views, **metadata}


def setup_scan(tmp_path, text="@example | limit=5\n", links=""):
    channels_file, links_file = tmp_path / "channels.txt", tmp_path / "links.txt"
    channels_file.write_text(text, encoding="utf-8")
    links_file.write_text(links, encoding="utf-8")
    return channels_file, links_file


def fake_ydl(monkeypatch, extract):
    options, calls = [], []

    class YDL:
        def __init__(self, opts):
            options.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download):
            calls.append((url, download))
            return extract(url)

    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=YDL))
    monkeypatch.setattr(channels, "yt_dlp_auth_opts", lambda: {})
    return options, calls


def test_parse_channel_options(tmp_path: Path) -> None:
    path = tmp_path / "channels.txt"
    path.write_text(
        "# comment\n"
        "@veritasium | limit=3 | min_views=100000\n"
        "https://www.youtube.com/@lexfridman | scan=25\n",
        encoding="utf-8",
    )
    assert channels.parse_channels_file(path) == [
        {"line_no": 2, "url": "@veritasium", "limit": 3,
         "min_views": 100000, "scan": 0},
        {"line_no": 3, "url": "https://www.youtube.com/@lexfridman",
         "limit": 5, "min_views": 0, "scan": 25},
    ]


def test_invalid_channel_options_are_safe(tmp_path):
    path, _ = setup_scan(tmp_path, "@example | limit=inf | scan=nan | views=-1\n")
    spec = channels.parse_channels_file(path)[0]
    assert (spec["limit"], spec["scan"], spec["min_views"]) == (5, 0, 0)


def test_rank_entries_uses_views_not_upload_date() -> None:
    entries = [
        video("new", 1000, "Why the surprising secret", upload_date="20260101"),
        video("old", 9000000, "Evergreen", upload_date="20150101"),
        video("middle", 500000, "Middle", upload_date="20200101"),
    ]
    ranked = channels.rank_entries(entries, limit=2)
    assert [entry["video_id"] for entry in ranked] == ["old", "middle"]
    assert ranked[0]["ranking_signals"]["channel_relative_views"] == 18
    assert "clip_potential_title_heuristic" in ranked[0]["ranking_signals"]


def test_title_heuristic_breaks_view_ties():
    ranked = channels.rank_entries([
        video("plain", 1000, "A conversation"),
        video("hook", 1000, "Why the hidden truth"),
    ])
    assert ranked[0]["video_id"] == "hook"


def test_topic_variety_defers_repeated_topics_without_dropping_them():
    entries = [video("one", 10000, "Why quantum gravity matters part 1"),
               video("two", 9900, "Why quantum gravity matters part 2"),
               video("three", 9000, "Ocean exploration adventures")]
    assert [e["video_id"] for e in channels.rank_entries(entries, limit=2)] == ["one", "three"]
    assert len(channels.rank_entries(entries, limit=3)) == 3


@pytest.mark.parametrize("value", [None, "broken", "NaN", float("inf"), -5, True, {}, []])
def test_invalid_views_and_duration_remain_unknown(value):
    ranked = channels.rank_entries([video("unknown", value, duration=value)])
    assert len(ranked) == 1
    assert ranked[0]["view_count"] is None
    assert ranked[0]["ranking_signals"]["channel_relative_views"] is None
    assert channels.rank_entries([video("unknown", value)], min_views=1) == []


def test_zero_duration_is_known_too_short_but_zero_views_valid():
    assert channels.rank_entries([video("short", 1000, duration=0)]) == []
    ranked = channels.rank_entries([video("zero", "0", duration="180")])
    assert ranked[0]["view_count"] == 0
    assert channels.rank_entries([video("one")], limit=0) == []


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=same&list=abc&t=10",
    "https://youtu.be/same?si=tracking",
    "https://m.youtube.com/shorts/same?feature=share",
    "https://www.youtube.com/embed/same#fragment",
    "https://www.youtube.com/live/same",
])
def test_canonical_ids_and_urls(url):
    assert channels.channel_video_id(url) == "same"
    assert channels.channel_video_url({"webpage_url": url}) == "https://www.youtube.com/watch?v=same"


def test_duplicate_ids_cannot_consume_slots():
    ranked = channels.rank_entries([
        video("same", 1000),
        {"url": "https://youtu.be/same?si=tag", "view_count": 2000},
        video("other", 1500),
        {"url": "https://www.youtube.com/@channel"},
        {"url": "https://not-youtube.test/watch?v=bad"},
        {"url": "https://[broken/watch?v=bad"},
    ], limit=3)
    assert [entry["video_id"] for entry in ranked] == ["same", "other"]


@pytest.mark.parametrize("metadata", [
    {"is_live": True}, {"is_upcoming": True}, {"live_status": "is_live"},
    {"live_status": "is_upcoming"}, {"live_status": "post_live"},
    {"availability": "private"}, {"availability": "unavailable"},
    {"availability": "subscriber_only"}, {"availability": "needs_auth"},
    {"title": "[Deleted video]"}, {"duration": 29}, {"has_audio": False},
    {"has_video": False}, {"acodec": "none"}, {"height": 480},
    {"formats": [{"height": 360, "acodec": "aac", "vcodec": "h264"}]},
    {"formats": [{"height": 1080, "acodec": "none", "vcodec": "h264"}]},
])
def test_known_unsuitable_entries_are_filtered(metadata):
    assert channels.rank_entries([video("bad", 1000000, **metadata)]) == []


def test_adaptive_formats_and_archived_live_are_suitable():
    entry = video("archive", 1000, live_status="was_live", height=360,
                  duration=20000, acodec="none", formats=[
                      {"height": 1080, "vcodec": "av1", "acodec": "none"},
                      {"vcodec": "none", "acodec": "aac"},
                  ])
    ranked = channels.rank_entries([entry])
    assert ranked[0]["has_audio"] is True
    assert ranked[0]["height"] == 1080
    assert len(channels.rank_entries([video("unknown")])) == 1


def test_scan_adds_unseen_top_videos_and_remembers_ids(tmp_path, monkeypatch):
    channels_file, links_file = setup_scan(
        tmp_path, "@example | limit=2\n", "https://www.youtube.com/watch?v=already\n")
    monkeypatch.setattr(channels, "_fetch_channel_entries", lambda url, scan: [
        video("already", 999999), video("best", 900000), video("second", 800000)])
    selected = channels.scan_channels(channels_file, links_file)
    assert [entry["video_id"] for entry in selected] == ["best", "second"]
    assert links_file.read_text().splitlines() == [
        "https://www.youtube.com/watch?v=already",
        "https://www.youtube.com/watch?v=best",
        "https://www.youtube.com/watch?v=second",
    ]
    assert set(json.loads(channels.SEEN_FILE.read_text())) == {"best", "second"}
    assert channels.scan_channels(channels_file, links_file) == []


@pytest.mark.parametrize("count", [3, 5])
def test_full_or_excess_backlog_skips_all_metadata_and_preserves_files(tmp_path, monkeypatch, count):
    original = "".join(f"https://youtu.be/existing{i}\n" for i in range(count))
    spec, links = setup_scan(tmp_path, links=original)
    monkeypatch.setattr(channels, "_enrich_shortlist",
                        lambda *args: pytest.fail("full backlog must not enrich"))
    monkeypatch.setattr(channels, "_read_cache",
                        lambda *args: pytest.fail("full backlog must not read metadata"))
    assert channels.scan_channels(spec, links, backlog_max=3) == []
    assert links.read_text() == original
    assert not channels.SEEN_FILE.exists()


def test_pending_renders_use_backlog_capacity(tmp_path, monkeypatch):
    spec, links = setup_scan(tmp_path, links="https://youtu.be/existing\n")
    assert channels.scan_channels(spec, links, backlog_max=3, pending_render_count=2) == []
    monkeypatch.setattr(channels, "_fetch_channel_entries",
                        lambda *args: [video("one"), video("two")])
    selected = channels.scan_channels(spec, links, backlog_max=3, pending_render_count=1)
    assert len(selected) == 1
    assert channels._active_link_count(links) == 2


def test_round_robin_capacity_and_channel_limits(tmp_path, monkeypatch):
    spec, links = setup_scan(tmp_path, "@large | limit=5\n@small | limit=1\n@third | limit=5\n")
    monkeypatch.setattr(channels, "_fetch_channel_entries", lambda url, scan: [
        video(f"{url[1:]}{i}", (1000000 if url == "@large" else 10000) - i)
        for i in range(8)])
    selected = channels.scan_channels(spec, links, backlog_max=5)
    assert [entry["source_channel"] for entry in selected] == [
        "@large", "@small", "@third", "@large", "@third"]
    assert channels._active_link_count(links) == 5


def test_duplicate_ids_across_channels_dont_waste_capacity(tmp_path, monkeypatch):
    spec, links = setup_scan(tmp_path, "@one | limit=2\n@two | limit=2\n")
    monkeypatch.setattr(channels, "_fetch_channel_entries", lambda url, scan: [
        video("shared", 10000), video(url[1:], 1000)])
    selected = channels.scan_channels(spec, links, backlog_max=3)
    assert len(selected) == 3
    assert {entry["video_id"] for entry in selected} == {"shared", "one", "two"}


def test_comments_done_and_history_urls_are_seen_and_original_bytes_preserved(tmp_path, monkeypatch):
    original = ("# manually disabled https://youtu.be/manual?si=x\n"
                "DONE https://www.youtube.com/shorts/done\n"
                "# DONE https://www.youtube.com/watch?v=commented&x=1\n"
                "https://youtu.be/inline # DONE\n"
                "# user note without trailing newline: café")
    spec, links = setup_scan(tmp_path, links=original)
    old_history = {"legacy-key": {"url": "https://youtu.be/historical", "custom": ["keep"]}}
    channels.SEEN_FILE.parent.mkdir(parents=True)
    channels.SEEN_FILE.write_text(json.dumps(old_history))
    monkeypatch.setattr(channels, "_fetch_channel_entries", lambda *args: [
        video(key, 10000 - i) for i, key in enumerate(
            ["manual", "done", "commented", "inline", "historical", "fresh"])])
    selected = channels.scan_channels(spec, links, backlog_max=1)
    assert [entry["video_id"] for entry in selected] == ["fresh"]
    assert links.read_text(encoding="utf-8") == original + "\nhttps://www.youtube.com/watch?v=fresh\n"
    assert json.loads(channels.SEEN_FILE.read_text(encoding="utf-8"))["legacy-key"] == old_history["legacy-key"]


def test_cache_ttl_key_and_compact_fields(tmp_path, monkeypatch):
    calls, now = [], [100000.0]
    monkeypatch.setattr(channels.time, "time", lambda: now[0])

    def fetch(url, scan):
        calls.append((url, scan))
        return [video("old", 9000000, upload_date="20100101", subtitles={"en": "large"},
                      formats=[{"vcodec": "av1", "acodec": "aac", "height": 1080,
                                "url": "https://private-signed-media.invalid/secret"}])]

    monkeypatch.setattr(channels, "_fetch_channel_entries", fetch)
    first = channels._load_channel_entries("@example", 0)
    assert first[0]["view_count"] == 9000000
    cache = channels._cache_path("@example", 0)
    text = cache.read_text()
    assert "formats" not in text and "subtitles" not in text and "secret" not in text
    assert "upload_date" not in text
    now[0] += 86399
    assert channels._load_channel_entries("https://www.youtube.com/@example/videos", 0) == first
    assert len(calls) == 1
    now[0] += 1
    channels._load_channel_entries("@example", 0)
    assert len(calls) == 2
    channels._load_channel_entries("@example", 5)
    assert len(calls) == 3
    assert channels._cache_path("@example", 5) != cache


@pytest.mark.parametrize("hours", ["0", "0.001"])
def test_cache_ttl_environment_is_respected(monkeypatch, hours):
    calls, now = [], [100000.0]
    monkeypatch.setenv("CHANNEL_CACHE_HOURS", hours)
    monkeypatch.setattr(channels.time, "time", lambda: now[0])
    monkeypatch.setattr(channels, "_fetch_channel_entries",
                        lambda *args: calls.append(args) or [video("one")])
    channels._load_channel_entries("@example", 0)
    now[0] += 4
    channels._load_channel_entries("@example", 0)
    assert len(calls) == 2


def test_corrupt_cache_refreshes_and_write_failure_is_nonfatal(monkeypatch):
    cache = channels._cache_path("@example", 0)
    cache.parent.mkdir(parents=True)
    cache.write_text("broken")
    monkeypatch.setattr(channels, "_fetch_channel_entries", lambda *args: [video("one")])
    monkeypatch.setattr(channels, "_atomic_json", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    assert channels._load_channel_entries("@example", 0)[0]["id"] == "one"
    assert cache.read_text() == "broken"


def test_flat_listing_is_metadata_only_and_scan_is_explicit(monkeypatch):
    options, calls = fake_ydl(monkeypatch, lambda url: {"entries": [video("one"), None]})
    assert _REAL_FETCH("@example", 0) == [video("one")]
    _REAL_FETCH("@example", 10)
    assert options[0]["extract_flat"] == "in_playlist"
    assert "playlistend" not in options[0]
    assert options[1]["playlistend"] == 10
    assert all(opts["skip_download"] for opts in options)
    assert all(download is False for _, download in calls)


def test_unavailable_listing_does_not_create_empty_day_long_cache(monkeypatch):
    fake_ydl(monkeypatch, lambda url: None)
    monkeypatch.setattr(channels, "_fetch_channel_entries", _REAL_FETCH)
    with pytest.raises(RuntimeError, match="listing unavailable"):
        channels._load_channel_entries("@example", 0)
    assert not channels._cache_path("@example", 0).exists()


def test_enrichment_is_bounded_and_cached_across_scans(tmp_path, monkeypatch):
    spec, links = setup_scan(tmp_path, "@example | limit=5\n")
    monkeypatch.setattr(channels, "_fetch_channel_entries", lambda *args: [
        video(f"video{i}", 10000 - i) for i in range(100)])
    options, calls = fake_ydl(monkeypatch, lambda url: video(
        channels.channel_video_id(url), 10000, duration=300, height=1080,
        acodec="aac", vcodec="h264"))
    monkeypatch.setattr(channels, "_enrich_shortlist", _REAL_ENRICH)
    assert len(channels.scan_channels(spec, links, backlog_max=2)) == 2
    assert len(calls) == 4  # twice remaining slots, not all 100 listing entries
    assert options[0]["skip_download"] and options[0]["noplaylist"]
    assert all(download is False for _, download in calls)
    cached = channels._load_channel_entries("@example", 0)
    assert sum(entry.get("has_audio") is True for entry in cached) == 4
    ranked = channels.rank_entries(cached, limit=4)
    _REAL_ENRICH(ranked)
    assert len(calls) == 4  # cached suitability does not request full metadata again


def test_enrichment_only_fetches_missing_entries_and_does_not_truncate(monkeypatch):
    entries = channels.rank_entries([
        video("known", 5000, duration=60, height=1080, acodec="aac", vcodec="h264"),
        video("missing", None), video("later", 1000)], limit=3)
    options, calls = fake_ydl(monkeypatch, lambda url: video(channels.channel_video_id(url), 9000))
    enriched = _REAL_ENRICH(entries)
    assert len(enriched) == 3
    assert {channels.channel_video_id(url) for url, _ in calls} == {"missing", "later"}
    assert next(e for e in enriched if e["video_id"] == "missing")["view_count"] == 9000


def test_unknown_min_views_can_be_enriched_then_filtered(tmp_path, monkeypatch):
    spec, links = setup_scan(tmp_path, "@example | min_views=5000\n")
    monkeypatch.setattr(channels, "_fetch_channel_entries", lambda *args: [video("unknown", None)])
    fake_ydl(monkeypatch, lambda url: video("unknown", 10000))
    monkeypatch.setattr(channels, "_enrich_shortlist", _REAL_ENRICH)
    assert channels.scan_channels(spec, links, backlog_max=1)[0]["view_count"] == 10000


def test_enriched_unsuitable_candidate_is_replaced_from_bounded_reserve(tmp_path, monkeypatch):
    spec, links = setup_scan(tmp_path)
    monkeypatch.setattr(channels, "_fetch_channel_entries",
                        lambda *args: [video("bad", 10000), video("good", 9000)])
    fake_ydl(monkeypatch, lambda url: video(channels.channel_video_id(url),
                                          is_live=channels.channel_video_id(url) == "bad"))
    monkeypatch.setattr(channels, "_enrich_shortlist", _REAL_ENRICH)
    assert [e["video_id"] for e in channels.scan_channels(spec, links, backlog_max=1)] == ["good"]


def test_network_failures_are_isolated_and_unknown_facts_not_invented(tmp_path, monkeypatch):
    spec, links = setup_scan(tmp_path, "@broken\n@working\n")

    def fetch(url, scan):
        if url == "@broken":
            raise RuntimeError("listing failed")
        return [video("unknown", None)]

    monkeypatch.setattr(channels, "_fetch_channel_entries", fetch)
    fake_ydl(monkeypatch, lambda url: (_ for _ in ()).throw(RuntimeError("metadata failed")))
    monkeypatch.setattr(channels, "_enrich_shortlist", _REAL_ENRICH)
    selected = channels.scan_channels(spec, links, backlog_max=1)
    assert selected[0]["view_count"] is None
    assert selected[0].get("height") is None
    assert selected[0].get("has_audio") is None
    assert json.loads(channels.SEEN_FILE.read_text())["unknown"]["views"] is None


def test_corrupt_history_is_preserved_and_stops_discovery(tmp_path):
    spec, links = setup_scan(tmp_path)
    channels.SEEN_FILE.parent.mkdir(parents=True)
    channels.SEEN_FILE.write_text("{incomplete history")
    assert channels.scan_channels(spec, links) == []
    assert channels.SEEN_FILE.read_text() == "{incomplete history"
    assert links.read_text() == ""


def test_history_save_merges_latest_entries(monkeypatch):
    channels._save_history({"old": {"custom": "preserved"}})
    channels._save_history({"concurrent": {"views": 1}})
    channels._save_history({"new": {"views": 2}})
    assert set(channels._load_history()) == {"old", "concurrent", "new"}


def test_new_links_during_fetch_are_preserved_and_rechecked_against_cap(tmp_path, monkeypatch):
    spec, links = setup_scan(tmp_path)

    def fetch(*args):
        links.write_text("https://youtu.be/manual\n")
        return [video("fresh")]

    monkeypatch.setattr(channels, "_fetch_channel_entries", fetch)
    assert channels.scan_channels(spec, links, backlog_max=1) == []
    assert links.read_text() == "https://youtu.be/manual\n"


def test_new_history_url_during_fetch_is_not_reselected(tmp_path, monkeypatch):
    spec, links = setup_scan(tmp_path)

    def fetch(*args):
        channels._save_history({"https://youtu.be/fresh": {"views": 1000}})
        return [video("fresh")]

    monkeypatch.setattr(channels, "_fetch_channel_entries", fetch)
    assert channels.scan_channels(spec, links) == []
    assert links.read_text() == ""


def test_append_failure_does_not_record_unwritten_links(tmp_path, monkeypatch):
    spec, links = setup_scan(tmp_path)
    monkeypatch.setattr(channels, "_fetch_channel_entries", lambda *args: [video("fresh")])
    original_open = Path.open

    def fail_append(path, mode="r", *args, **kwargs):
        if path == links and mode == "a+":
            raise OSError("read only")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_append)
    with pytest.raises(OSError, match="read only"):
        channels.scan_channels(spec, links)
    assert not channels.SEEN_FILE.exists()
    assert links.read_text() == ""


def test_concurrent_scans_recheck_cap_inside_commit_lock(tmp_path, monkeypatch):
    spec, links = setup_scan(tmp_path)
    barrier = threading.Barrier(2)

    def fetch(*args):
        barrier.wait(timeout=10)
        return [video("fresh")]

    monkeypatch.setattr(channels, "_fetch_channel_entries", fetch)
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(channels.scan_channels, spec, links, backlog_max=1)
                   for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]
    assert sum(map(len, results)) == 1
    assert links.read_text() == "https://www.youtube.com/watch?v=fresh\n"
    assert set(channels._load_history()) == {"fresh"}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows byte-range locking")
def test_empty_lock_file_serializes_two_handles_without_initialization_write(tmp_path, monkeypatch):
    lock = tmp_path / "empty.lock"
    role = threading.local()
    contender_opened, owner_locked = threading.Event(), threading.Event()
    release_owner, contender_locked = threading.Event(), threading.Event()
    original_open = Path.open

    def both_open_before_lock(path, mode="r", *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        if path == lock and mode == "a+b":
            if role.name == "owner":
                assert contender_opened.wait(5)
            else:
                contender_opened.set()
                assert owner_locked.wait(5)
        return handle

    monkeypatch.setattr(Path, "open", both_open_before_lock)

    def owner():
        role.name = "owner"
        with channels._file_lock(lock):
            owner_locked.set()
            assert release_owner.wait(5)

    def contender():
        role.name = "contender"
        with channels._file_lock(lock):
            contender_locked.set()

    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(owner)
        second = workers.submit(contender)
        try:
            assert owner_locked.wait(5)
            assert lock.stat().st_size == 0
            assert not contender_locked.wait(0.05)
            assert not second.done()
        finally:
            release_owner.set()
        first.result(timeout=5)
        second.result(timeout=5)
    assert contender_locked.is_set()
    assert lock.exists() and lock.stat().st_size == 0


def test_concurrent_history_saves_preserve_all_records():
    with ThreadPoolExecutor(max_workers=4) as workers:
        futures = [workers.submit(channels._save_history, {str(i): {"views": i}})
                   for i in range(12)]
        for future in futures:
            future.result(timeout=15)
    assert set(channels._load_history()) == {str(i) for i in range(12)}
    channels._save_history({"0": {"custom": "keep"}})
    assert channels._load_history()["0"] == {"views": 0, "custom": "keep"}


def test_failed_atomic_history_replace_preserves_original(monkeypatch):
    channels._save_history({"old": {"views": 5}})
    original = channels.SEEN_FILE.read_bytes()
    monkeypatch.setattr(channels.os, "replace",
                        lambda *args: (_ for _ in ()).throw(OSError("replace failed")))
    with pytest.raises(OSError, match="replace failed"):
        channels._save_history({"new": {"views": 6}})
    assert channels.SEEN_FILE.read_bytes() == original
    assert sorted(path.name for path in channels.SEEN_FILE.parent.iterdir()) == [
        "channel_history.json", "channel_history.json.lock"]


def test_relative_signal_uses_whole_channel_not_just_shortlist(tmp_path, monkeypatch):
    spec, links = setup_scan(tmp_path)
    monkeypatch.setattr(channels, "_fetch_channel_entries", lambda *args: [
        video("top", 100000), video("runnerup", 90000), video("typical1", 1000),
        video("typical2", 1000), video("typical3", 1000)])
    selected = channels.scan_channels(spec, links, backlog_max=1)
    assert selected[0]["ranking_signals"]["channel_relative_views"] == 100


def test_topic_variety_across_channels(tmp_path, monkeypatch):
    spec, links = setup_scan(tmp_path, "@one\n@two\n")
    entries = {
        "@one": [video("first", 10000, "Quantum gravity theory")],
        "@two": [video("same-topic", 10000, "Quantum gravity theory explained"),
                 video("varied", 9000, "Ocean exploration adventures")],
    }
    monkeypatch.setattr(channels, "_fetch_channel_entries", lambda url, scan: entries[url])
    selected = channels.scan_channels(spec, links, backlog_max=2)
    assert [entry["video_id"] for entry in selected] == ["first", "varied"]


def test_expired_cache_network_failure_keeps_cache_bytes(monkeypatch):
    monkeypatch.setattr(channels, "_fetch_channel_entries", lambda *args: [video("one")])
    channels._load_channel_entries("@example", 0)
    cache = channels._cache_path("@example", 0)
    original = cache.read_bytes()
    monkeypatch.setenv("CHANNEL_CACHE_HOURS", "0")
    monkeypatch.setattr(channels, "_fetch_channel_entries",
                        lambda *args: (_ for _ in ()).throw(RuntimeError("offline")))
    with pytest.raises(RuntimeError, match="offline"):
        channels._load_channel_entries("@example", 0)
    assert cache.read_bytes() == original


def test_history_write_failure_keeps_durable_links(tmp_path, monkeypatch):
    spec, links = setup_scan(tmp_path)
    monkeypatch.setattr(channels, "_fetch_channel_entries", lambda *args: [video("fresh")])
    monkeypatch.setattr(channels, "_save_history",
                        lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    assert len(channels.scan_channels(spec, links)) == 1
    assert links.read_text() == "https://www.youtube.com/watch?v=fresh\n"
    assert channels.scan_channels(spec, links) == []

