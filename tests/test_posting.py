"""Posting/CLI boundaries with fake network/time and exclusively temporary IO."""
import json
import os
import subprocess
import sys
import time as real_time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hooky import config, pipeline_cli as cli
from hooky.core import queue as post_queue
from hooky.platforms import instagram


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(post_queue, "QUEUE_FILE", tmp_path / "state" / "queue.json")
    monkeypatch.setattr(post_queue, "POST_HOURS", "")
    monkeypatch.setattr(post_queue, "IGNORE_WINDOW", False)
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(cli, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(cli, "LINKS_FILE", tmp_path / "links.txt")
    monkeypatch.setattr(instagram, "SESSION_FILE", tmp_path / "session")
    monkeypatch.setenv("MAKE_MAX_SOURCES", "2")
    monkeypatch.setenv("QUEUE_MAX_PENDING", "12")
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "is_configured", lambda: True)
    monkeypatch.setattr(cli, "login", lambda: SimpleNamespace(user_id=1))
    monkeypatch.setattr(cli, "upload", lambda *a, **kw: pytest.fail("unexpected network upload"))
    clock = SimpleNamespace(now=10000.0, sleeps=[])
    queue_time = SimpleNamespace(
        time=lambda: clock.now, monotonic=real_time.monotonic,
        sleep=real_time.sleep, localtime=real_time.localtime,
        strftime=real_time.strftime,
    )
    monkeypatch.setattr(post_queue, "time", queue_time)
    monkeypatch.setattr(post_queue, "force_gap", lambda: 120.0)

    def sleep(seconds):
        assert seconds >= 120
        clock.sleeps.append(seconds)
        clock.now += seconds
    monkeypatch.setattr(cli, "time", SimpleNamespace(sleep=sleep))
    return clock


def queued(tmp_path, count=1):
    clips = [tmp_path / f"clip-{n}.mp4" for n in range(count)]
    for clip in clips:
        clip.write_bytes(b"rendered")
    return clips, post_queue.enqueue_clips(clips)


def test_force_drain_logs_in_once_and_paces_every_upload(tmp_path, monkeypatch, isolated):
    clips, state = queued(tmp_path, 3)
    logins, attempts = [], []
    client = SimpleNamespace(user_id=1)
    monkeypatch.setattr(cli, "login", lambda: logins.append(1) or client)

    def upload(clip, **kwargs):
        assert kwargs["client"] is client
        disk = post_queue.load_queue(state.path)
        assert str(clip) in disk["in_flight"]  # durable before the network call
        attempts.append(isolated.now)
        return "IGcode"
    monkeypatch.setattr(cli, "upload", upload)
    stats = cli.upload_due(state, force=True)
    assert stats["posted"] == 3 and stats["remaining"] == 0
    assert len(logins) == 1
    assert attempts == [10000.0, 10120.0, 10240.0]
    assert isolated.sleeps == [120, 120]
    assert not post_queue.load_queue()["in_flight"]
    assert len(post_queue.load_queue()["posted"]) == len(clips)


def test_post_due_only_posts_one_and_force_resume_keeps_safety_gap(tmp_path, monkeypatch, isolated):
    _, state = queued(tmp_path, 2)
    monkeypatch.setattr(cli, "upload", lambda *a, **kw: "Code")
    assert cli.upload_due(state)["posted"] == 1
    assert cli.upload_due(state)["posted"] == 0
    assert cli.post_now() == 0
    assert isolated.sleeps == [120]


def test_delay_runs_after_completion_not_only_after_start(tmp_path, monkeypatch, isolated):
    _, state = queued(tmp_path, 2)
    attempts = []

    def upload(*args, **kwargs):
        attempts.append(isolated.now)
        isolated.now += 400  # slow upload must not consume the next safety gap
        return "Code"
    monkeypatch.setattr(cli, "upload", upload)
    cli.upload_due(state, force=True)
    assert attempts == [10000, 10520]
    assert isolated.sleeps == [120]


@pytest.mark.parametrize("failure", [RuntimeError("transport broke"), TimeoutError("lost response"),
                                     instagram.UploadUncertainError("unknown")])
def test_every_exception_after_begin_stops_pass_and_requires_review(tmp_path, monkeypatch, failure):
    clips, state = queued(tmp_path, 2)
    calls = []

    def upload(clip, **kwargs):
        calls.append(clip)
        raise failure
    monkeypatch.setattr(cli, "upload", upload)
    stats = cli.upload_due(state, force=True)
    assert stats["blocked"] == 1 and stats["failed"] == 1
    assert calls == [clips[0]]
    disk = post_queue.load_queue()
    assert disk["in_flight"][str(clips[0])]["status"] == "unknown"
    assert disk["failed"][str(clips[0])]["retryable"] is False
    assert cli.upload_due(disk, force=True)["blocked"] == 1
    assert cli.post_now() == 2
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), SystemExit(4)])
def test_interrupt_after_journaling_is_blocked_on_restart(tmp_path, monkeypatch, failure):
    clips, state = queued(tmp_path, 2)
    monkeypatch.setattr(cli, "upload", lambda *a, **kw: (_ for _ in ()).throw(failure))
    with pytest.raises(type(failure)):
        cli.upload_due(state, force=True)
    assert str(clips[0]) in post_queue.review_holds(post_queue.load_queue())
    assert cli.upload_due(post_queue.load_queue(), force=True)["blocked"]


@pytest.mark.parametrize("result", [None, "", "None", 123, {}, "not a code", SimpleNamespace(code=None)])
def test_invalid_confirmation_is_uncertain(tmp_path, monkeypatch, result):
    # Literal 'None' looks syntactically like a shortcode but is a known absent
    # value accidentally stringified by old clients; it must not confirm a post.
    _, state = queued(tmp_path)
    monkeypatch.setattr(cli, "upload", lambda *a, **kw: result)
    assert cli.upload_due(state, force=True)["blocked"]
    assert post_queue.review_holds(post_queue.load_queue())


def test_preupload_journal_write_failure_never_calls_network(tmp_path, monkeypatch):
    _, state = queued(tmp_path)
    monkeypatch.setattr(post_queue, "_atomic_write", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        cli.upload_due(state, force=True)
    assert not post_queue.load_queue()["in_flight"]


def test_success_record_failure_retains_preupload_hold(tmp_path, monkeypatch):
    clips, state = queued(tmp_path)
    monkeypatch.setattr(cli, "upload", lambda *a, **kw: "RemoteSuccess")
    monkeypatch.setattr(post_queue, "mark_posted", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(post_queue, "mark_failed", lambda *a, **kw: (_ for _ in ()).throw(OSError("still full")))
    assert cli.upload_due(state, force=True)["blocked"]
    assert post_queue.load_queue()["in_flight"][str(clips[0])]["status"] == "in_flight"


@pytest.mark.parametrize("failure", [RuntimeError("login failed"), SystemExit(1)])
def test_login_failure_is_retryable_and_not_an_unknown_upload(tmp_path, monkeypatch, failure):
    clips, state = queued(tmp_path, 2)
    calls = []

    def login():
        calls.append(1)
        raise failure
    monkeypatch.setattr(cli, "login", login)
    stats = cli.upload_due(state, force=True)
    assert stats["failed"] == 1 and not stats["blocked"]
    assert len(calls) == 1
    assert not post_queue.review_holds(state)
    assert state["failed"][str(clips[0])]["retryable"] is True


def test_missing_file_and_unlogged_client_never_make_uncertain_hold(tmp_path, monkeypatch):
    clips, state = queued(tmp_path)
    clips[0].unlink()
    assert cli.upload_due(state, force=True)["skipped"] == 1
    assert not post_queue.review_holds(state)
    clips[0].write_bytes(b"recovered")
    monkeypatch.setattr(cli, "login", lambda: None)
    assert cli.upload_due(state, force=True)["failed"] == 1
    assert not post_queue.review_holds(state)


def test_local_preflight_race_can_release_hold_without_remote_send(tmp_path, monkeypatch):
    _, state = queued(tmp_path)
    monkeypatch.setattr(cli, "upload", lambda *a, **kw: (_ for _ in ()).throw(
        instagram.UploadNotStartedError("file removed before send")))
    assert cli.upload_due(state, force=True)["failed"] == 1
    assert not post_queue.review_holds(post_queue.load_queue())


def test_maker_can_enqueue_during_upload_without_losing_history(tmp_path, monkeypatch):
    clips, state = queued(tmp_path)
    stale_maker = post_queue.load_queue()
    later = tmp_path / "later.mp4"
    later.write_bytes(b"later")

    def upload(*a, **kw):
        post_queue.enqueue_clips([later], state=stale_maker)
        return "RemoteCode"
    monkeypatch.setattr(cli, "upload", upload)
    stats = cli.upload_due(state, force=True)
    assert stats["posted"] == 1 and stats["remaining"] == 1
    fresh = post_queue.load_queue()
    assert post_queue.is_posted(clips[0], fresh)
    assert post_queue.is_pending(later, fresh)


def test_concurrent_poster_and_review_actions_are_refused(tmp_path):
    clips, state = queued(tmp_path)
    post_queue.begin_upload(clips[0], state)
    with post_queue.poster_lock(state):
        with pytest.raises(post_queue.QueueBusyError):
            cli.upload_due(state, force=True)
        assert cli.main(["--retry-after-review", str(clips[0])]) == 2
        assert cli.main(["--confirm-posted", str(clips[0]), "Code"]) == 2
    assert post_queue.review_holds(post_queue.load_queue())


def test_explicit_review_cli_never_posts_by_itself(tmp_path):
    clips, state = queued(tmp_path)
    post_queue.begin_upload(clips[0], state)
    assert cli.main(["--retry-after-review", str(clips[0])]) == 0
    assert not post_queue.review_holds(post_queue.load_queue())
    assert post_queue.is_pending(clips[0], post_queue.load_queue())
    # Confirmation also performs no network call and deduplicates future make.
    state = post_queue.load_queue()
    state["upload_not_before"] = 0
    # Use fake time advancement rather than bypassing the persisted safety gap.
    original = post_queue.time.time()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(post_queue.time, "time", lambda: original + 500)
        post_queue.begin_upload(clips[0], state)
    assert cli.main(["--confirm-posted", str(clips[0]), "VerifiedCode"]) == 0
    assert post_queue.is_posted(clips[0], post_queue.load_queue())
    # Open-source defaults retain local media; cleanup is opt-in through setup.
    assert clips[0].exists()


def test_corrupt_cli_queue_returns_error_without_reset():
    qpath = post_queue.queue_path()
    qpath.parent.mkdir()
    qpath.write_text("corrupt")
    assert cli.main(["--post-now"]) == 2
    assert qpath.read_text() == "corrupt"


def test_hard_crash_leaves_hold_and_os_releases_poster_lock(tmp_path):
    clips, state = queued(tmp_path)
    script = """
import os, sys
from pathlib import Path
from types import SimpleNamespace
from hooky import pipeline_cli as cli
from hooky.core import queue as q
q.QUEUE_FILE = Path(sys.argv[1])
q.POST_HOURS = ''
cli.is_configured = lambda: True
cli.login = lambda: SimpleNamespace(user_id=1)
cli.upload = lambda *a, **kw: os._exit(17)
cli.post_now()
"""
    env = {**os.environ, "STATE_DIR": str(tmp_path / "state"),
           "OUTPUT_DIR": str(tmp_path / "output"), "LINKS_FILE": str(tmp_path / "links.txt"),
           "IG_SESSION_FILE": str(tmp_path / "session")}
    result = subprocess.run([sys.executable, "-c", script, str(state.path)],
                            env=env, timeout=15, capture_output=True)
    assert result.returncode == 17, result.stderr
    fresh = post_queue.load_queue()
    assert str(clips[0]) in fresh["in_flight"]
    with post_queue.poster_lock(fresh):
        pass
    assert cli.upload_due(fresh, force=True)["blocked"]


@pytest.mark.parametrize("code", [None, "", 123, "bad code"])
def test_instagram_missing_invalid_code_is_ambiguous(tmp_path, code):
    clip = tmp_path / "reel.mp4"
    clip.write_bytes(b"video")
    client = SimpleNamespace(user_id=1, clip_upload=lambda *a, **kw: SimpleNamespace(code=code))
    with pytest.raises(instagram.UploadUncertainError):
        instagram.upload(clip, client=client)


def test_instagram_local_errors_do_not_call_clip_upload(tmp_path):
    client = SimpleNamespace(user_id=1, clip_upload=lambda *a, **kw: pytest.fail("remote send"))
    with pytest.raises(instagram.UploadNotStartedError):
        instagram.upload(tmp_path / "missing.mp4", client=client)
    with pytest.raises(instagram.UploadNotStartedError):
        instagram.upload(tmp_path / "missing.mp4", client=None)


def _fake_links(tmp_path, monkeypatch, run=None, count=4):
    links = tmp_path / "links.txt"
    entries = [f"https://example.test/video-{i}" for i in range(count)]
    links.write_text("\n".join(entries) + "\n")
    events = []
    transcript = [{"start": 0, "end": 1, "text": "hello"}]

    def download(entry, limits):
        events.append(("download", entry["line_no"]))
        i = entry["line_no"]
        video = tmp_path / f"source-{i}.mp4"
        return [{"path": video, "url": entry["url"], "video_id": f"id-{i}",
                 "source_transcript": transcript, "title": "title", "description": "desc",
                 "uploader": "author", "clips": 1, "min_len": 15, "max_len": 60,
                 "sub_style": None, "no_subs": None, "analysis": None}], True

    class Pipeline:
        def __init__(self, options):
            self.options = options

        def run(self, video, **kwargs):
            events.append(("render", int(video.stem.split("-")[-1])))
            assert kwargs["source_transcript"] == transcript
            assert kwargs["context"]["video_id"].startswith("id-")
            if run:
                return run(video, kwargs, links)
            return SimpleNamespace(clips=[{"path": video}], manifest={"clips": []})

    monkeypatch.setattr(cli, "expand_and_download", download)
    monkeypatch.setattr(cli, "Pipeline", Pipeline)
    return links, events


def test_links_default_bound_and_immediate_done_before_next_source(tmp_path, monkeypatch):
    def run(video, kwargs, links):
        if video.stem.endswith("2"):
            assert links.read_text().splitlines()[0].startswith("# DONE ")
        return SimpleNamespace(clips=[1], manifest={})
    links, events = _fake_links(tmp_path, monkeypatch, run)
    assert cli.main(["--links", str(links)]) == 0
    assert events == [("download", 1), ("render", 1), ("download", 2), ("render", 2)]
    lines = links.read_text().splitlines()
    assert lines[0].startswith("# DONE ") and lines[1].startswith("# DONE ")
    assert lines[2:] == ["https://example.test/video-2", "https://example.test/video-3"]


def test_links_limit_and_dry_run_never_mark_done(tmp_path, monkeypatch):
    links, events = _fake_links(tmp_path, monkeypatch)
    original = links.read_bytes()
    assert cli.main(["--links", str(links), "--dry-run", "--limit", "1"]) == 0
    assert events == [("download", 1), ("render", 1)]
    assert links.read_bytes() == original
    assert not post_queue.queue_path().exists()


def test_queue_high_water_checked_before_each_download(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUE_MAX_PENDING", "1")

    def run(video, kwargs, links):
        post_queue.enqueue_clips([tmp_path / "rendered.mp4"], state=kwargs["queue_state"])
        return SimpleNamespace(clips=[1], manifest={})
    links, events = _fake_links(tmp_path, monkeypatch, run)
    assert cli.main(["--links", str(links)]) == 0
    assert events == [("download", 1), ("render", 1)]
    # A subsequent make does not download anything while backlog is full.
    events.clear()
    assert cli.main(["--links", str(links)]) == 0
    assert events == []


def test_completed_line_survives_interrupt_on_later_source(tmp_path, monkeypatch):
    def run(video, kwargs, links):
        if video.stem.endswith("2"):
            raise KeyboardInterrupt
        return SimpleNamespace(clips=[1], manifest={})
    links, _ = _fake_links(tmp_path, monkeypatch, run)
    with pytest.raises(KeyboardInterrupt):
        cli.main(["--links", str(links)])
    lines = links.read_text().splitlines()
    assert lines[0].startswith("# DONE ")
    assert not lines[1].startswith("# DONE ")


def test_existing_manifest_never_bypasses_pipeline_resume(tmp_path, monkeypatch):
    links, events = _fake_links(tmp_path, monkeypatch, count=1)
    manifest = cli.OUTPUT_DIR / "old" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"source_url": "https://example.test/video-0"}))
    assert cli.main(["--links", str(links), "--no-post", "--sub-style", "hormozi"]) == 0
    assert events == [("download", 1), ("render", 1)]


def test_links_atomic_failure_and_concurrent_edit_leave_original(tmp_path, monkeypatch):
    links = tmp_path / "links.txt"
    links.write_text("first\nsecond\n")
    cli._mark_links_done(links, [1], expected={1: "changed"})
    assert links.read_text() == "first\nsecond\n"
    monkeypatch.setattr(cli.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(OSError):
        cli._mark_links_done(links, [1])
    assert links.read_text() == "first\nsecond\n"
    assert not list(tmp_path.glob("*.tmp"))


def test_failed_and_empty_sources_are_not_marked_done(tmp_path, monkeypatch):
    links, _ = _fake_links(tmp_path, monkeypatch, count=1,
                           run=lambda *a: SimpleNamespace(clips=[], manifest={}))
    original = links.read_bytes()
    assert cli.main(["--links", str(links)]) == 0
    assert links.read_bytes() == original
    monkeypatch.setattr(cli, "expand_and_download", lambda *a: ([], True))
    assert cli.main(["--links", str(links)]) == 0
    assert links.read_bytes() == original


def test_local_make_never_posts_and_uses_bound(tmp_path, monkeypatch):
    sources = [tmp_path / f"local{i}.mp4" for i in range(3)]
    for source in sources:
        source.write_bytes(b"video")
    calls = []

    class Pipeline:
        def __init__(self, *a):
            pass

        def run(self, video, **kwargs):
            assert "source_transcript" in kwargs
            calls.append(video)
    monkeypatch.setattr(cli, "Pipeline", Pipeline)
    assert cli.main([str(p) for p in sources]) == 0
    assert calls == sources[:2]


@pytest.mark.parametrize("name,value", [("MAKE_MAX_SOURCES", "bad"),
                                        ("MAKE_MAX_SOURCES", "0"),
                                        ("QUEUE_MAX_PENDING", "-1")])
def test_invalid_bounds_fail_before_download(tmp_path, monkeypatch, name, value):
    links, events = _fake_links(tmp_path, monkeypatch)
    monkeypatch.setenv(name, value)
    assert cli.main(["--links", str(links)]) == 2
    assert events == []

