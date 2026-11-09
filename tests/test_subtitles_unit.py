"""Fast regression tests for subtitle timing and style rendering."""
from hooky.core.subtitles import build_ass


def dialogue(ass: str) -> list[str]:
    return [line for line in ass.splitlines() if line.startswith("Dialogue:")]


def test_ass_has_word_timing_and_no_overlay_style() -> None:
    ass = build_ass([
        {"start": 0.2, "end": 0.6, "word": "hello"},
        {"start": 0.7, "end": 1.1, "word": "world"},
    ], 0.0, 2.0, "hormozi")

    assert len(dialogue(ass)) == 1
    assert r"{\k50}HELLO" in ass
    assert r"{\k40}WORLD" in ass
    assert "Style: Hook" not in ass


def test_dynamic_auto_scaling_for_extra_wide_text() -> None:
    ass = build_ass([
        {"start": 0.0, "end": 1.0, "word": "PNEUMONOULTRAMICROSCOPICSILICOVOLCANOCONIOSIS"},
    ], 0.0, 1.0, "hormozi")
    assert r"{\fs" in ass


def test_sub_font_size_and_max_words_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("SUB_FONT_SIZE", "50")
    monkeypatch.setenv("SUB_MAX_WORDS", "2")
    ass = build_ass([
        {"start": 0.0, "end": 0.5, "word": "one"},
        {"start": 0.5, "end": 1.0, "word": "two"},
        {"start": 1.0, "end": 1.5, "word": "three"},
    ], 0.0, 2.0, "hormozi")
    assert ",50," in ass
    assert len(dialogue(ass)) == 2


def test_stale_long_word_is_not_resurrected_in_later_clip() -> None:
    words = [
        {"start": 0.0, "end": 19.0, "word": "stale"},
        {"start": 10.2, "end": 10.7, "word": "later"},
    ]
    ass = build_ass(words, 10.0, 12.0, "clean")
    assert "STALE" not in ass
    assert "later" in ass


def test_words_with_tied_onsets_are_kept_in_one_caption_group() -> None:
    words = [{"start": 0.5, "end": 0.6, "word": str(i)} for i in range(4)]
    lines = dialogue(build_ass(words, 0.0, 2.0, "hormozi"))
    assert len(lines) == 1
    assert all(f"{i}" in lines[0] for i in range(4))


def test_karaoke_uses_next_word_onset_without_accumulated_rounding_drift() -> None:
    words = [
        {"start": 1.00, "end": 1.4, "word": "one"},
        {"start": 1.50, "end": 1.9, "word": "two"},
        {"start": 2.10, "end": 2.5, "word": "three"},
    ]
    lines = dialogue(build_ass(words, 0.0, 4.0, "hormozi"))
    assert len(lines) == 1
    assert r"{\k50}ONE" in lines[0]
    assert r"{\k60}TWO" in lines[0]
    assert r"{\k40}THREE" in lines[0]


def test_all_subtitle_styles_build_valid_ass() -> None:
    from hooky.core.subtitles import STYLES
    words = [{"start": 1.0, "end": 1.5, "word": "Hello"}, {"start": 1.5, "end": 2.0, "word": "World"}]
    for style_name in STYLES:
        ass = build_ass(words, 0.0, 3.0, style_name)
        assert "Style: Sub" in ass
        assert "Dialogue:" in ass

