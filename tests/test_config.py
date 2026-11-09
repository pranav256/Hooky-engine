"""The documented .env syntax must work, including uncommented examples."""
import pytest

from hooky.dotenv import parse_value, parse_dotenv, dump_dotenv, validate_settings


@pytest.mark.parametrize("raw,expected", [
    ("auto       # source captions, then Whisper", "auto"),
    ("2 # CPU threads", "2"),
    ('"a # secret" # comment', "a # secret"),
    ("'a # secret'", "a # secret"),
    ("abc#123", "abc#123"),
    (" # empty", ""),
])
def test_dotenv_comments_preserve_literal_hashes(raw, expected):
    assert parse_value(raw) == expected


def test_shared_dotenv_roundtrip_quotes_windows_paths_export_and_duplicate_keys():
    raw = '''# comment
export API_EXTENSION="a # hash \\\"quote\\\""
WINDOWS_PATH='C:\\Users\\Example Person\\output'
EMPTY=
DUPLICATE=old
DUPLICATE=new
LITERAL=$HOME $(never execute)
'''
    values = parse_dotenv(raw)
    assert values["API_EXTENSION"] == 'a # hash "quote"'
    assert values["WINDOWS_PATH"] == "C:\\Users\\Example Person\\output"
    assert values["EMPTY"] == "" and values["DUPLICATE"] == "new"
    assert values["LITERAL"] == "$HOME $(never execute)"
    assert parse_dotenv(dump_dotenv(values)) == values


@pytest.mark.parametrize("values", [
    {"AUTO_MAX_CLIPS": "", "AUTO_CANDIDATES_PER_CHUNK": "", "AUTO_CLIP_DENSITY_MINUTES": ""},
    {"SUB_FONT_SIZE": "", "SUB_MAX_WORDS": "0", "SOURCE_MAX_HEIGHT": ""},
    {"PARTS_TARGET_SECONDS": "1", "PARTS_MODE": "story", "PARTS_BACKGROUND": "blur"},
    {"PARTS_TARGET_SECONDS": "90", "PARTS_MODE": " NUMBER-ONLY ", "PARTS_BACKGROUND": " PLAIN "},
    {"X264_PRESET": "placebo", "AUTO_CANDIDATES_PER_CHUNK": "100", "SUB_FONT_SIZE": "100", "SUB_MAX_WORDS": "8"},
])
def test_known_pipeline_setting_compatibility(values):
    validate_settings(values)


@pytest.mark.parametrize("auto_values,expected", [
    ({"AUTO_MAX_CLIPS": "", "AUTO_CANDIDATES_PER_CHUNK": "", "AUTO_CLIP_DENSITY_MINUTES": ""}, [40, 10, 10]),
    ({"AUTO_MAX_CLIPS": "100", "AUTO_CANDIDATES_PER_CHUNK": "100", "AUTO_CLIP_DENSITY_MINUTES": "1.5"}, [100, 20, 2]),
])
def test_validated_values_are_consumed_consistently_in_a_fresh_process(auto_values, expected):
    import json
    import os
    import subprocess
    import sys
    values = {**auto_values, "PARTS_MODE": " NUMBER-ONLY ", "PARTS_BACKGROUND": " PLAIN ",
              "PARTS_TARGET_SECONDS": "90", "X264_PRESET": " VERYSLOW ",
              "SUB_FONT_SIZE": "0", "SUB_MAX_WORDS": "", "SUB_STYLE": "", "SUBTITLE_SOURCE": "",
              "AUTO_CLIP_MODE": "", "WHISPER_COMPUTE": ""}
    validate_settings(values)
    script = '''import json, os
from hooky.core.highlights import AUTO_MAX_CLIPS, AUTO_CANDIDATES_PER_CHUNK, AUTO_DENSITY
from hooky.core.lowspec import X264_PRESET, WHISPER_COMPUTE
from hooky.config import SUB_STYLE, SUBTITLE_SOURCE, AUTO_CLIP_MODE
from hooky.parts.cli import build_parser
from hooky.parts.options import PartsOptions
from hooky.core.subtitles import build_ass
args = build_parser().parse_args(["source.mp4"])
parts = PartsOptions(mode=args.mode, target_seconds=args.part_seconds, background=args.background)
words = [{"word":"Hello", "start":0, "end":1}]
with_default_overrides = build_ass(words, 0, 2, "hormozi")
os.environ.pop("SUB_FONT_SIZE")
os.environ.pop("SUB_MAX_WORDS")
assert with_default_overrides == build_ass(words, 0, 2, "hormozi")
print(json.dumps({"auto":[AUTO_MAX_CLIPS,AUTO_CANDIDATES_PER_CHUNK,AUTO_DENSITY],
                  "preset":X264_PRESET,"parts":[parts.mode,parts.target_seconds,parts.background],
                  "defaults":[SUB_STYLE,SUBTITLE_SOURCE,AUTO_CLIP_MODE,WHISPER_COMPUTE]}))
'''
    result = subprocess.run([sys.executable, "-c", script], env={**os.environ, **values, "HOOKY_SETTINGS_SNAPSHOT": "1"},
                            capture_output=True, text=True, check=True, timeout=20)
    actual = json.loads(result.stdout)
    assert actual == {"auto": expected, "preset": "veryslow", "parts": ["number-only", 90, "plain"],
                      "defaults": ["hormozi", "auto", "content", "int8"]}


def test_initial_utf8_bom_is_shared_by_startup_setup_and_parser(tmp_path):
    from hooky.setup.storage import read_env
    raw = "\ufeff# PowerShell UTF-8 file\r\nSUB_STYLE=gold\r\nCUSTOM_EXTENSION=value\r\n"
    path = tmp_path / ".env"
    path.write_bytes(raw.encode("utf-8"))
    expected = {"SUB_STYLE": "gold", "CUSTOM_EXTENSION": "value"}
    assert parse_dotenv(raw) == expected
    assert read_env(path) == expected
    assert parse_dotenv(dump_dotenv(expected)) == expected


@pytest.mark.parametrize("raw", ["\ufeff\ufeffKEY=value", "KEY=value\n\ufeffNEXT=value"])
def test_bom_is_only_accepted_once_at_start_of_document(raw):
    with pytest.raises(ValueError, match="Invalid setting"):
        parse_dotenv(raw)


def test_bom_inside_value_is_preserved_literally():
    assert parse_dotenv("CUSTOM_EXTENSION=\ufeffliteral") == {"CUSTOM_EXTENSION": "\ufeffliteral"}

