import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from hooky import config
from hooky.web import worker


def test_worker_only_publishes_changed_progress_and_final_state(tmp_path, monkeypatch, capsys):
    from hooky.web import analyzer
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(worker.os, "environ", os.environ.copy())

    async def delayed_analysis(url, job):
        await asyncio.sleep(0.45)
        job["progress"] = 40
        await asyncio.sleep(0.45)
        job["status"] = "completed"
        return {"hooks": []}

    monkeypatch.setattr(analyzer, "run_analysis_pipeline", delayed_analysis)
    job = {"id": "progress_fixture", "type": "analyze", "status": "running", "progress": 5}
    assert asyncio.run(worker.run({"job": job, "payload": {"url": "https://example.com/video"}})) == 0
    events = [json.loads(line.removeprefix(worker.EVENT_PREFIX))
              for line in capsys.readouterr().out.splitlines() if line.startswith(worker.EVENT_PREFIX)]
    assert [event["progress"] for event in events] == [5, 40, 100]
    assert events[-1]["status"] == "completed"
    assert events[-1]["result"] == {"hooks": []}


@pytest.mark.parametrize("outcome", ["success", "ai_fallback", "source_failure"])
def test_analysis_worker_publishes_job_progress_and_result(tmp_path, monkeypatch, capsys, outcome):
    from hooky.web import analyzer

    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(worker.os, "environ", os.environ.copy())
    metadata = {"title": "Sample source", "duration": 60, "id": "EDCwQe7P8T0"}
    cues = [{"start": i * 2, "end": i * 2 + 2, "text": "A useful idea about code and investing."}
            for i in range(30)]
    prepare = AsyncMock(return_value=(metadata, cues))
    if outcome == "source_failure":
        prepare.side_effect = RuntimeError("Source unavailable")
    monkeypatch.setattr(analyzer, "prepare_source", prepare)
    generate = Mock(return_value=json.dumps([{"start": 0, "end": 20, "title": "Chosen hook"}]))
    if outcome == "ai_fallback":
        generate.side_effect = RuntimeError("AI unavailable")
    monkeypatch.setattr(analyzer, "generate", generate)
    job = {"id": "analysis_fixture", "type": "analyze", "status": "running", "progress": 5}

    code = asyncio.run(worker.run({"job": job, "payload": {"url": "https://example.com/video"}}))

    assert prepare.call_args.args[2] is job
    events = [json.loads(line.removeprefix(worker.EVENT_PREFIX))
              for line in capsys.readouterr().out.splitlines() if line.startswith(worker.EVENT_PREFIX)]
    assert events[-1]["result"] == job["result"]
    assert job["logs"]
    if outcome == "source_failure":
        assert code == 1 and events[-1]["status"] == "failed"
        assert job["error"] == "Source unavailable"
        assert job["result"]["hooks"] == []
        generate.assert_not_called()
    else:
        assert code == 0 and events[-1]["status"] == "completed"
        assert events[-1]["progress"] == 100
        assert job["result"]["video_title"] == metadata["title"]
        assert job["result"]["hooks"]
        assert job["step_number"] == 5
        generate.assert_called_once()


@pytest.mark.parametrize("target", ["", "@example", "local"])
def test_cli_worker_keeps_queue_done_markers_and_owns_its_outputs(tmp_path, monkeypatch, target):
    output = tmp_path / "output"
    links, channels = tmp_path / "links.txt", tmp_path / "channels.txt"
    links.write_text("https://example.com/video\n")
    channels.write_text("@example\n")
    for name, value in (("OUTPUT_DIR", output), ("PROJECT_ROOT", tmp_path), ("LINKS_FILE", links), ("CHANNELS_FILE", channels)):
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(worker.os, "environ", os.environ.copy())
    original = links.read_text()
    async def fake_cli(*args, **kwargs):
        job_links = Path(args[args.index("--links") + 1])
        job_channels = Path(kwargs["env"]["CHANNELS_FILE"])
        if not target:
            assert job_links == links and job_channels == channels
            job_links.write_text("# DONE https://example.com/video\n")
        else:
            assert job_links.is_relative_to(output / "web_cache" / "make_fixture")
            assert job_channels.is_relative_to(output / "web_cache" / "make_fixture")
        owned = Path(kwargs["env"]["OUTPUT_DIR"])
        assert owned == output / "make_fixture"
        reel = owned / "Source" / "reel_finished.mp4"
        reel.parent.mkdir(parents=True)
        reel.write_bytes(b"valid clip" * 300)
        process = Mock()
        async def wait():
            return 0
        process.wait = wait
        return process
    monkeypatch.setattr(worker.asyncio, "create_subprocess_exec", fake_cli)
    job = {"id": "make_fixture", "type": "make", "status": "running", "progress": 5}
    assert asyncio.run(worker.run({"job": job, "payload": {"target": target, "style": "gold", "framing": "center"}})) == 0
    assert job["output_file"] == "make_fixture/Source/reel_finished.mp4"
    assert (output / job["output_file"]).exists()
    assert links.read_text() == ("# DONE https://example.com/video\n" if not target else original)


def test_empty_cli_job_cannot_claim_another_workers_output(tmp_path, monkeypatch):
    output = tmp_path / "output"
    output.mkdir()
    for name, value in (("OUTPUT_DIR", output), ("PROJECT_ROOT", tmp_path), ("LINKS_FILE", tmp_path / "links.txt"), ("CHANNELS_FILE", tmp_path / "channels.txt")):
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(worker.os, "environ", os.environ.copy())
    async def concurrent_render(*args, **kwargs):
        (output / "reel_other_worker.mp4").write_bytes(b"other worker" * 300)
        process = Mock()
        async def wait():
            return 0
        process.wait = wait
        return process
    monkeypatch.setattr(worker.asyncio, "create_subprocess_exec", concurrent_render)
    job = {"id": "make_empty", "type": "make", "status": "running", "progress": 5}
    assert asyncio.run(worker.run({"job": job, "payload": {"target": "", "style": "gold", "framing": "center"}})) == 1
    assert job["status"] == "failed" and not job.get("output_file")
    assert "no new complete reels" in job["error"]
