"""Topic selection reads the whole transcript and never invents fallback matches."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from hooky.web import analyzer, worker
from hooky import config


@pytest.fixture
def topic_source(tmp_path, monkeypatch):
    monkeypatch.setattr(analyzer, "OUTPUT_DIR", tmp_path)
    cues = [{"start": i * 2, "end": i * 2 + 2,
             "text": f"Ordinary sports discussion number {i}. " + "A detailed explanation of today's game. " * 5}
            for i in range(300)]
    cues[-1]["text"] = "Unique conclusion: startup mistakes can teach founders."
    metadata = {"title": "Long recording", "id": "fixture0001", "duration": 600}
    monkeypatch.setattr(analyzer, "prepare_source", AsyncMock(return_value=(metadata, cues)))
    return metadata, cues


def test_topic_search_reads_late_transcript_and_preserves_evidence(topic_source, monkeypatch):
    def select(prompt, cache):
        assert "startup mistakes" in prompt
        return [{"start": 580, "end": 600, "title": "Founder lessons", "reason": "The conclusion explains mistakes."}]
    selection = Mock(side_effect=select)
    monkeypatch.setattr(analyzer, "_ask_json", selection)
    job = {"id": "topic_fixture", "status": "running"}
    result = asyncio.run(analyzer.run_analysis_pipeline("https://example.com/video", job, topic="startup mistakes"))
    assert selection.call_count > 1
    assert "Unique conclusion" in selection.call_args.args[0]
    assert len(result["hooks"]) == 1
    hook = result["hooks"][0]
    assert hook["start"] == 580 and hook["end"] == 600
    assert any("startup mistakes" in cue["text"] for cue in hook["cues"])
    assert result["topic"] == "startup mistakes" and result["selection_mode"] == "ai"
    assert job["status"] == "completed"


def test_ai_failure_uses_only_real_keyword_matches(topic_source, monkeypatch):
    monkeypatch.setattr(analyzer, "_ask_json", Mock(side_effect=RuntimeError("Quota unavailable")))
    result = asyncio.run(analyzer.run_analysis_pipeline("https://example.com/video", {}, topic="startup mistakes"))
    assert result["selection_mode"] == "keyword" and result["notice"]
    assert result["hooks"]
    assert all(hook["category"] == "Word match" for hook in result["hooks"])
    assert all(hook["start"] > 550 for hook in result["hooks"])
    assert all("mistakes" in hook["reason"] for hook in result["hooks"])


@pytest.mark.parametrize("ai_result", [[], RuntimeError("AI unavailable")])
def test_no_topic_matches_is_a_completed_empty_result(topic_source, monkeypatch, ai_result):
    selection = Mock(return_value=ai_result if isinstance(ai_result, list) else None,
                     side_effect=ai_result if isinstance(ai_result, Exception) else None)
    monkeypatch.setattr(analyzer, "_ask_json", selection)
    job = {}
    result = asyncio.run(analyzer.run_analysis_pipeline("https://example.com/video", job, topic="quantum entanglement"))
    assert result["hooks"] == [] and result["status"] == "success"
    assert job["status"] == "completed"
    assert "No matching" in job["step"]


def test_invalid_and_overlapping_ai_ranges_are_rejected(topic_source, monkeypatch):
    topic_source[1][:] = [{"start": 0, "end": 60, "text": "Startup mistakes and founder advice."}]
    monkeypatch.setattr(analyzer, "_ask_json", Mock(return_value=[
        {"start": float("nan"), "end": 20}, {"start": -1, "end": 20},
        {"start": True, "end": 20}, {"start": 0, "end": 700},
        {"start": 0, "end": 3}, {"start": 0, "end": 20, "relevance": 90},
        {"start": 10, "end": 25, "relevance": 80}, {"start": 30, "end": 60},
    ]))
    result = asyncio.run(analyzer.run_analysis_pipeline("https://example.com/video", {}, topic="founder advice"))
    assert [(hook["start"], hook["end"]) for hook in result["hooks"]] == [(0, 20), (30, 60)]


def test_worker_forwards_topic_without_losing_no_match_result(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    analysis = AsyncMock(return_value={"status": "success", "topic": "founder advice", "hooks": []})
    async def completed(url, job, topic):
        job["status"] = "completed"
        return analysis.return_value
    analysis.side_effect = completed
    monkeypatch.setattr(analyzer, "run_analysis_pipeline", analysis)
    job = {"id": "topic_worker", "type": "analyze", "status": "running"}
    assert asyncio.run(worker.run({"job": job, "payload": {"url": "https://example.com/video", "topic": "founder advice"}})) == 0
    assert analysis.call_args.kwargs["topic"] == "founder advice"
    assert job["result"]["hooks"] == []
