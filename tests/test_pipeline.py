"""Pipeline orchestration regressions; no model, network, or FFmpeg needed."""
from pathlib import Path

from hooky.options import PipelineOptions
from hooky.pipeline import Pipeline


def test_selected_transcription_requests_only_clip_windows(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    calls: list[list[float]] = []
    pipeline = Pipeline(PipelineOptions(no_upload=True))

    def fake_transcribe(path: Path, language=None, clip_timestamps=None):
        calls.append(list(clip_timestamps or []))
        return [{"start": 8.0, "end": 9.0, "word": "hello"}]

    pipeline.state["transcribe"] = fake_transcribe
    words = pipeline.transcribe_windows(
        source, [{"start": 10.0, "end": 20.0}, {"start": 40.0, "end": 45.0}])

    assert words and calls == [[8.0, 22.0, 38.0, 47.0]]


def test_run_selects_highlights_before_partial_transcription(tmp_path: Path,
                                                             monkeypatch) -> None:
    master = tmp_path / "master.mp4"
    analysis = tmp_path / "master_analysis.mp4"
    master.write_bytes(b"master")
    analysis.write_bytes(b"analysis")
    selected_calls: list[list[float]] = []
    output = tmp_path / "out"
    output.mkdir()
    rendered = output / "reel_01.mp4"
    rendered.write_bytes(b"clip")

    monkeypatch.setattr("hooky.pipeline.get_duration", lambda path: 60.0)
    pipeline = Pipeline(PipelineOptions(no_upload=True, out=output,
                                         clips=1, min_len=5, max_len=30))
    pipeline.state["find_highlights"] = lambda *args, **kwargs: [
        {"start": 10.0, "end": 20.0, "reason": "test"}
    ]

    def fake_transcribe(path: Path, language=None, clip_timestamps=None):
        assert path == analysis
        selected_calls.append(list(clip_timestamps or []))
        return [{"start": 12.0, "end": 13.0, "word": "hello"}]

    pipeline.state["transcribe"] = fake_transcribe
    pipeline.state["generate_captions"] = lambda *args, **kwargs: [{
        "caption": "test caption"
    }]
    pipeline.state["cut_all_clips"] = lambda *args, **kwargs: [{
        "index": 1, "start": 10.0, "end": 20.0, "reason": "test",
        "path": rendered, "cover": None, "subtitled": True,
    }]

    job = pipeline.run(master, analysis_path=analysis, queue_state=None)

    assert job.clips and selected_calls == [[8.0, 22.0]]


def test_source_captions_can_replace_whisper_for_subtitle_timing(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    pipeline = Pipeline(PipelineOptions(no_upload=True, subtitle_source="auto"))
    pipeline.state["transcribe"] = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("Whisper should not run when source captions exist"))

    words = pipeline.subtitle_words(
        source,
        [{"start": 2.0, "end": 5.0}],
        [{"start": 2.0, "end": 5.0, "text": "source captions work"}],
        tmp_path / "selected.words.json",
    )

    assert [word["word"] for word in words] == ["source", "captions", "work"]


def test_selected_word_checkpoints_are_reused_per_window(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    pipeline = Pipeline(PipelineOptions(no_upload=True, subtitle_source="whisper"))
    calls: list[list[float]] = []

    def fake_transcribe(path, language=None, clip_timestamps=None):
        calls.append(list(clip_timestamps or []))
        return [
            {"start": 10.0, "end": 11.0, "word": "first"},
            {"start": 40.0, "end": 41.0, "word": "second"},
        ]

    pipeline.state["transcribe"] = fake_transcribe
    cache = tmp_path / "selected.words.json"
    first = pipeline.transcribe_windows(
        source, [{"start": 10.0, "end": 20.0}, {"start": 40.0, "end": 45.0}],
        cache_path=cache)
    second = pipeline.transcribe_windows(
        source, [{"start": 10.0, "end": 20.0}], cache_path=cache)

    assert len(first) == 2 and len(second) == 1
    assert calls == [[8.0, 22.0, 38.0, 47.0]]


def test_full_selection_resumes_across_queue_batches_and_deleted_posted_files(tmp_path, monkeypatch):
    from hooky.core import queue

    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    monkeypatch.setattr("hooky.pipeline.get_duration", lambda path: 300.0)
    monkeypatch.setattr(queue, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setenv("QUEUE_MAX_PENDING", "2")
    pipeline = Pipeline(PipelineOptions(out=tmp_path / "out", no_subs=True, no_captions=True))
    selections, rendered = [], []

    def select(*args, **kwargs):
        selections.append(kwargs["n_clips"])
        return [{"start": i * 30, "end": i * 30 + 20} for i in range(5)]

    def render(video, clips, output_dir, **kwargs):
        results = []
        for clip in clips:
            path = output_dir / f"{clip['clip_id']}.mp4"
            path.write_bytes(b"clip")
            rendered.append(clip["start"])
            results.append({**clip, "path": path, "subtitled": False})
        return results

    pipeline.state["find_highlights"] = select
    pipeline.state["cut_all_clips"] = render
    first = pipeline.run(source, queue_state=queue.load_queue())
    assert not first.complete and len(first.manifest["planned_clips"]) == 5
    assert first.manifest["remaining"] == 3
    state = queue.load_queue()
    for path in list(state["pending"]):
        queue.mark_posted(Path(path), state, "confirmed")
        Path(path).unlink()
    second = pipeline.run(source, queue_state=queue.load_queue())
    assert not second.complete and second.manifest["remaining"] == 1
    state = queue.load_queue()
    for path in list(state["pending"]):
        queue.mark_posted(Path(path), state, "confirmed")
    third = pipeline.run(source, queue_state=queue.load_queue())
    assert third.complete and len(third.manifest["clips"]) == 5
    assert selections == [0] and rendered == [0, 30, 60, 90, 120]

