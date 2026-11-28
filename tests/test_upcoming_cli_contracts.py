"""Public dispatcher contracts for setup and shell completion.

These tests deliberately use no shell, subprocess, network, credentials,
cookies, or files outside pytest's temporary directory.
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from hooky import cli


def test_no_arguments_run_the_default_links_flow(monkeypatch) -> None:
    links = Mock(return_value=19)
    monkeypatch.setattr(cli, "_run_links_file", links)

    assert cli.main([]) == 19
    links.assert_called_once_with()


def test_setup_command_dispatches_to_the_offline_handler(monkeypatch) -> None:
    from hooky.setup import cli as setup_cli

    setup = Mock(return_value=17)
    monkeypatch.setattr(setup_cli, "main", setup)

    assert cli.main(["setup", "--non-interactive"]) == 17
    setup.assert_called_once_with(["--non-interactive"])


def test_completion_command_dispatches_to_the_shell_handler(monkeypatch) -> None:
    completion = Mock(return_value=23)
    monkeypatch.setattr(cli, "_completion", completion)

    assert cli.main(["completion", "bash"]) == 23
    completion.assert_called_once_with(["bash"])


@pytest.mark.parametrize("before,after,exit_code", [
    ({}, {}, 0),
    ({"source": {"attempts": 1}}, {"source": {"attempts": 1}}, 0),
    ({}, {"source": {"attempts": 1}}, 2),
    ({"source": {"attempts": 1}}, {"source": {"attempts": 2}}, 2),
], ids=["none", "unchanged", "new", "updated"])
def test_links_run_reports_new_or_updated_failure_records(tmp_path, monkeypatch, capsys, before, after, exit_code):
    from hooky import desktop
    from hooky.core import downloader, run_state

    monkeypatch.setattr(cli, "LINKS_FILE", tmp_path / "links.txt")
    monkeypatch.setattr(cli, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(cli, "_link_file_stats", lambda: (1, 0, 0))
    monkeypatch.setattr(cli, "_preflight", lambda **kwargs: 0)
    monkeypatch.setattr(downloader, "parse_links_file", lambda path: [])
    monkeypatch.setattr(run_state, "load_failures", Mock(side_effect=[before, after]))
    monkeypatch.setattr(desktop, "notify", Mock())
    pipeline = Mock(return_value=0)
    monkeypatch.setattr(cli, "_pipeline", pipeline)

    assert cli._process_links_file() == exit_code
    pipeline.assert_called_once_with(["--links", "--no-post"])
    assert ("Run finished with errors" if exit_code else "Run complete") in capsys.readouterr().out
