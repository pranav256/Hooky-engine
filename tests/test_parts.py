"""Story chronology, timeline mapping, export isolation, and resumable rendering."""
from pathlib import Path

import pytest

from hooky.options import PartsOptions
from hooky.parts.planner import normalize_story, plan_parts, remap_words, valid_ranges


def test_story_merges_overlap_and_preserves_chronology():
    story = normalize_story([
        {"start": 50, "end": 70, "role": "payoff"},
        {"start": 10, "end": 30, "role": "setup"},
        {"start": 20, "end": 40, "role": "stakes"},
    ], 100)
    assert [(s["start"], s["end"]) for s in story] == [(10, 40), (50, 70)]
    assert "setup" in story[0]["role"] and "stakes" in story[0]["role"]


@pytest.mark.parametrize("start,end", [(-1, 20), (5, 101), (10, 5), (True, 20),
                                        (float("nan"), 20), (5, float("inf"))])
def test_invalid_model_story_ranges_are_rejected(start, end):
    assert valid_ranges([{"start": start, "end": end}], 100) == []


def test_omitted_speech_is_removed_and_retained_words_are_retimed():
    segments = [{"start": 10, "end": 20}, {"start": 50, "end": 60}]
    words = [{"start": 11, "end": 12, "word": "setup"},
             {"start": 30, "end": 31, "word": "sponsor"},
             {"start": 52, "end": 53, "word": "payoff"}]
    assert remap_words(words, segments) == [
        {"start": 1, "end": 2, "word": "setup"},
        {"start": 12, "end": 13, "word": "payoff"}]


def test_parts_cover_retained_story_once_and_prefer_sentence_boundary():
    segments = [{"start": 5, "end": 45}, {"start": 80, "end": 150}]
    words = [{"start": 93, "end": 95, "word": "complete."}]
    parts = plan_parts(segments, remap_words(words, segments), 60)
    assert [p["duration"] for p in parts] == [55, 55]
    assert parts[0]["segments"] == [
        {"start": 5, "end": 45, "offset": 0},
        {"start": 80, "end": 95, "offset": 40}]
    assert parts[1]["segments"] == [{"start": 95, "end": 150, "offset": 0}]
    assert sum(fragment["end"] - fragment["start"]
               for part in parts for fragment in part["segments"]) == 110


@pytest.mark.parametrize("length,target", [(65, 60), (95, 90), (181, 60), (3.5, 1)])
def test_tail_and_platform_cap_preserve_every_retained_second(length, target):
    parts = plan_parts([{"start": 0, "end": length}], [], target)
    assert sum(p["duration"] for p in parts) == pytest.approx(length)
    assert all(0 < p["duration"] <= 90 for p in parts)
    assert all(a["end"] == b["start"] for a, b in zip(parts, parts[1:]))
    if length == 65:
        assert len(parts) == 1
    if length == 95:
        assert parts[-1]["duration"] >= 15


def test_story_ai_selects_a_coherent_subset_and_does_not_reorder_it(tmp_path, monkeypatch):
    from hooky.parts import planner
    prompts = []

    def ask(prompt, *args, **kwargs):
        prompts.append(prompt)
        if prompt.startswith("Identify"):
            return [{"start": 0, "end": 20, "role": "setup"},
                    {"start": 20, "end": 40, "role": "sponsor"},
                    {"start": 80, "end": 100, "role": "payoff"}]
        return [2, 0, True, 999, 2]

    monkeypatch.setattr(planner, "_ask_json", ask)
    story = planner.select_story([{"start": 0, "end": 100, "text": "story evidence"}],
                                 tmp_path / "source.mp4", 100, tmp_path / "cache")
    assert [(s["start"], s["end"]) for s in story] == [(0, 20), (80, 100)]
    assert "no target runtime or part count" in prompts[-1]


def test_parts_export_is_independent_of_queue_and_preserves_source(tmp_path, monkeypatch):
    from hooky.core import queue
    from hooky.parts import pipeline as module

    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    output = tmp_path / "parts"
    monkeypatch.setattr(queue, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(module, "get_duration", lambda path: 150)
    pipeline = module.PartsPipeline(PartsOptions(out=output, no_subs=True))
    calls = []
    pipeline.state["select_story"] = lambda *a, **kw: [
        {"start": 0, "end": 40}, {"start": 90, "end": 150}]

    def render(video, part, words, directory, options, count):
        path = directory / f"part_{part['index']:03d}.mp4"
        path.write_bytes(b"part")
        calls.append(part["index"])
        return path

    pipeline.state["render_part"] = render
    transcript = [{"start": 0, "end": 150, "text": "The original story."}]
    manifest = pipeline.run(source, source_transcript=transcript)
    assert manifest["complete"] and calls == [1, 2]
    assert manifest["retained_seconds"] == 100
    assert source.read_bytes() == b"source" and not queue.queue_path().exists()
    pipeline.state["select_story"] = lambda *a, **kw: pytest.fail("selection should be cached")
    pipeline.options.dry_run = True
    calls.clear()
    preview = pipeline.run(source, source_transcript=transcript)
    assert len(preview["parts"]) == 2 and not calls


def test_fragment_resume_after_interruption_does_not_reencode_completed_fragments(tmp_path, monkeypatch):
    from hooky.parts import renderer

    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    part = {"index": 1, "start": 0, "end": 2, "duration": 2,
            "segments": [{"start": 0, "end": 1, "offset": 0},
                         {"start": 4, "end": 5, "offset": 1}]}
    calls, fail = [], [True]

    def cut(video, start, end, path, **kwargs):
        calls.append(start)
        if start == 4 and fail[0]:
            fail[0] = False
            raise RuntimeError("interruption")
        path.write_bytes(b"fragment")

    def assemble(command, **kwargs):
        Path(command[-1]).write_bytes(b"assembled")

    monkeypatch.setattr(renderer, "cut_clip", cut)
    monkeypatch.setattr(renderer, "get_duration", lambda path: 1 if "fragment_" in path.name else 2)
    monkeypatch.setattr(renderer, "has_audio", lambda path: False)
    monkeypatch.setattr(renderer.subprocess, "run", assemble)
    options = PartsOptions(no_subs=True)
    with pytest.raises(RuntimeError, match="interruption"):
        renderer.render_part(source, part, [], tmp_path / "out", options, 1)
    output = renderer.render_part(source, part, [], tmp_path / "out", options, 1)
    renderer.render_part(source, part, [], tmp_path / "out", options, 1)
    assert calls == [0, 4, 4] and output.exists()
    assert not list((tmp_path / "out" / ".fragments").iterdir())


def test_whisper_full_story_windows_are_bounded_cached_and_deduplicated(tmp_path, monkeypatch):
    from hooky.core import subtitles

    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")
    calls = []

    def transcribe(path, language, clip_timestamps):
        calls.append(clip_timestamps)
        return [{"start": 299, "end": 299.5, "word": "first."},
                {"start": 301, "end": 301.5, "word": "second."}]

    monkeypatch.setattr(subtitles, "transcribe", transcribe)
    captions, words = subtitles.transcribe_source(source, 600, tmp_path / "speech")
    subtitles.transcribe_source(source, 600, tmp_path / "speech")
    assert calls == [[0, 302], [298, 600]]
    assert [word["word"] for word in words] == ["first.", "second."]
    assert len(captions) == 2


def test_parts_cli_validates_settings_before_download(monkeypatch):
    from hooky.parts import cli

    monkeypatch.setattr(cli, "expand_and_download", lambda *a: pytest.fail("download"))
    assert cli.main(["https://example.test/video", "--part-seconds", "nan"]) == 2
    assert cli.main(["https://example.test/video", "--part-seconds", "91"]) == 2


def test_parts_url_acquisition_skips_full_length_analysis_proxy(tmp_path, monkeypatch):
    from hooky.parts import cli

    captured = []

    def download(entry, limits):
        assert limits["make_analysis"] is False and limits["max_sources"] == 1
        return [{"path": tmp_path / "source.mp4", "url": entry["url"], "source_transcript": [],
                 "title": "Story", "description": "", "uploader": "channel"}], True

    class Pipeline:
        def __init__(self, options):
            assert options.background == "blur" and options.target_seconds == 60

        def run(self, video, **kwargs):
            captured.append(kwargs["source_url"])

    monkeypatch.setattr(cli, "expand_and_download", download)
    monkeypatch.setattr(cli, "PartsPipeline", Pipeline)
    assert cli.main(["https://example.test/video"]) == 0
    assert captured == ["https://example.test/video"]

