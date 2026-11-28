"""Offline tests for setup validation and local configuration storage."""
from __future__ import annotations

from pathlib import Path

from hooky.setup import cli as setup_cli
from hooky.setup.profiler import SystemProfile, ToolInfo
from hooky.setup.settings import profile_defaults
from hooky.setup.storage import read_env, settings_for_display, write_env


def _profile(*, threads: int = 8, ram_gb: int = 16) -> SystemProfile:
    return SystemProfile(
        os_name="SyntheticOS",
        distro="Synthetic distro",
        release="0",
        machine="synthetic",
        cpu_threads=threads,
        ram_bytes=ram_gb * 1024**3,
        gpu_hints=(),
        ffmpeg=ToolInfo(False),
        ffprobe=ToolInfo(False),
        hardware_acceleration=(),
    )


def test_noninteractive_setup_selects_profile_and_writes_only_requested_file(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    env_path = tmp_path / ".env"
    monkeypatch.setattr(setup_cli.config, "ENV_FILE", env_path)
    monkeypatch.setattr(setup_cli, "collect_system_profile", lambda: _profile(threads=2, ram_gb=2))

    assert setup_cli.main(["--non-interactive", "--profile", "low"]) == 0

    saved = read_env(env_path)
    assert saved["HOOKY_SETUP_COMPLETE"] == "1"
    assert saved["HOOKY_PROFILE"] == "low"
    assert saved["QUALITY"] == "low"
    assert saved["WHISPER_MODEL"] == "tiny"
    assert list(tmp_path.iterdir()) == [env_path]
    assert "Saved Hooky configuration" in capsys.readouterr().out


def test_setup_validation_rejects_conflicting_auth_metadata_without_reading_cookies(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    env_path = tmp_path / ".env"
    monkeypatch.setattr(setup_cli.config, "ENV_FILE", env_path)
    monkeypatch.setattr(setup_cli, "collect_system_profile", lambda: _profile())

    result = setup_cli.main([
        "--non-interactive",
        "--set", "YTDLP_COOKIES_FROM_BROWSER=firefox",
        "--set", "YTDLP_COOKIES_FILE=synthetic-cookie-path",
    ])

    assert result == 2
    assert not env_path.exists()
    assert "Choose browser cookies or a cookie file" in capsys.readouterr().err


def test_setup_storage_redacts_secrets_and_round_trips_non_secret_values(
    tmp_path: Path,
) -> None:
    output = tmp_path / ".env"
    values = {
        "GEMINI_API_KEY": "placeholder-key",
        "IG_PASSWORD": "placeholder-password",
        "QUALITY": "high",
        "UNMANAGED_EXAMPLE": "retained-by-storage",
    }

    write_env(values, output)
    content = output.read_text(encoding="utf-8")
    display = dict(settings_for_display(values))

    assert read_env(output)["QUALITY"] == "high"
    assert read_env(output)["UNMANAGED_EXAMPLE"] == "retained-by-storage"
    assert display["GEMINI_API_KEY"] == "<set; hidden>"
    assert display["IG_PASSWORD"] == "<set; hidden>"
    assert "placeholder-key" not in "\n".join(value for _, value in settings_for_display(values))
    assert "placeholder-key" in content


def test_profile_recommendation_is_deterministic_for_mocked_hardware() -> None:
    low = profile_defaults(_profile(threads=2, ram_gb=2), "adaptive")
    quality = profile_defaults(_profile(threads=8, ram_gb=16), "quality")

    assert low["QUALITY"] == "low"
    assert low["WHISPER_THREADS"] == "2"
    assert quality["QUALITY"] == "highest"
    assert quality["WHISPER_MODEL"] == "small"


