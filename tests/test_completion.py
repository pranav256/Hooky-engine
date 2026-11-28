"""Offline tests for native shell completion generation and candidates."""
from __future__ import annotations

import pytest

from hooky import cli, completion


@pytest.mark.parametrize("shell", completion.SHELLS)
def test_completion_script_is_available_for_each_supported_shell(shell: str) -> None:
    script = completion.script(shell)

    assert script
    assert "hooky" in script


def test_completion_dispatch_prints_script_without_starting_workflows(monkeypatch, capsys) -> None:
    monkeypatch.setattr(completion, "script", lambda shell: f"synthetic {shell} script\n")

    assert cli.main(["completion", "bash"]) == 0
    assert capsys.readouterr().out == "synthetic bash script\n"


def test_completion_candidates_cover_commands_and_value_choices() -> None:
    assert "completion" in completion.candidates(["hooky", ""], 1)
    assert "status" in completion.candidates(["hooky", ""], 1)
    assert "--profile" in completion.candidates(["hooky", "make", "--"], 2)
    assert completion.candidates(["hooky", "make", "--auto-mode", ""], 3) == [
        "content", "duration",
    ]


def test_completion_rejects_unknown_shell_without_external_access(capsys) -> None:
    assert cli.main(["completion", "tcsh"]) == 1
    assert "Usage: hooky completion" in capsys.readouterr().err

