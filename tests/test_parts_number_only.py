"""Targeted checks for Gemini-selected cartoon/episode parts."""
from pathlib import Path

import pytest

from hooky.options import PartsOptions


def test_number_only_options_force_numeric_label_and_no_subtitles():
    options = PartsOptions(mode="number-only")
    assert options.no_subs is True
    assert options.labels is True


def test_number_only_pipeline_uses_gemini_and_skips_whisper(tmp_path, monkeypatch):
    from hooky.parts import pipeline as module

    source = tmp_path / "cartoon.mp4"
    source.write_bytes(b"source")
    output = tmp_path / "parts"
    monkeypatch.setattr(module, "get_duration", lambda path: 125.0)
    pipeline = module.PartsPipeline(
        PartsOptions(mode="number-only", out=output, target_seconds=60))

    def must_not_run(*args, **kwargs):
        pytest.fail("number-only mode must not use story transcription")

    pipeline.state["select_story"] = must_not_run
    pipeline.state["transcribe_source"] = must_not_run
    selected_calls = []
    pipeline.state["find_highlights"] = lambda *args, **kwargs: (
        selected_calls.append(kwargs) or [
            {"start": 10, "end": 50, "reason": "funny setup and reaction"},
            {"start": 80, "end": 120, "reason": "funny payoff"},
        ])
    rendered = []

    def render(video, part, words, directory, options, total):
        assert words == [] and options.mode == "number-only"
        path = directory / f"part_{part['index']:03d}.mp4"
        path.write_bytes(b"part")
        rendered.append((part["index"], total))
        return path

    pipeline.state["render_part"] = render
    manifest = pipeline.run(source)
    assert manifest["mode"] == "number-only"
    assert manifest["labels"] == "numeric part number only"
    assert manifest["complete"] is True
    assert manifest["selection_evidence"].startswith("visual/audio evidence + Gemini")
    assert manifest["retained_seconds"] == 80
    assert len(manifest["parts"]) == 2
    assert selected_calls and selected_calls[0]["focus"] == (
        "funny, surprising, absurd, reaction, visual gag, or punchline moments")
    assert rendered == [(1, 2), (2, 2)]
    assert source.read_bytes() == b"source"


def test_number_only_renderer_sends_only_numeric_label(tmp_path, monkeypatch):
    from hooky.parts import renderer

    source = tmp_path / "cartoon.mp4"
    source.write_bytes(b"source")
    labels = []

    def cut(video, start, end, output, **kwargs):
        labels.append(kwargs["label"])
        output.write_bytes(b"fragment")

    def assemble(command, **kwargs):
        Path(command[-1]).write_bytes(b"assembled")

    monkeypatch.setattr(renderer, "cut_clip", cut)
    monkeypatch.setattr(renderer, "get_duration", lambda path: 2.0)
    monkeypatch.setattr(renderer, "has_audio", lambda path: False)
    monkeypatch.setattr(renderer.subprocess, "run", assemble)
    output = renderer.render_part(
        source,
        {"index": 7, "start": 0, "end": 2, "duration": 2,
         "segments": [{"start": 0, "end": 2, "offset": 0}]},
        [], tmp_path / "out", PartsOptions(mode="number-only"), 12)
    assert labels == ["7"]
    assert output.exists()

