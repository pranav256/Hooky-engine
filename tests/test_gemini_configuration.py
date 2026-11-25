"""Missing optional AI configuration must not terminate Studio workers."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from hooky import config
from hooky.core import gemini
from hooky.web import analyzer, worker


@pytest.mark.parametrize("sdk_available", [True, False])
def test_health_check_returns_failure_without_exiting(monkeypatch, sdk_available):
    monkeypatch.setattr(gemini, "_load_genai", lambda: sdk_available)
    monkeypatch.setattr(gemini, "GEMINI_API_KEY", "")
    assert gemini.check_gemini() == 1


@pytest.mark.parametrize("sdk_available", [True, False])
@pytest.mark.parametrize("topic", ["", "code"])
def test_analysis_without_ai_configuration_keeps_transcript_fallback(tmp_path, monkeypatch, sdk_available, topic):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(analyzer, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("HOOKY_WEB_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(gemini, "_load_genai", lambda: sdk_available)
    monkeypatch.setattr(gemini, "GEMINI_API_KEY", "")
    cues = [{"start": i * 2, "end": i * 2 + 2, "text": "A useful idea about code."} for i in range(30)]
    monkeypatch.setattr(analyzer, "prepare_source", AsyncMock(return_value=(
        {"title": "Sample source", "duration": 60, "id": "fixture0001"}, cues)))
    job = {"id": "missing_ai", "type": "analyze", "status": "running", "progress": 5}
    assert asyncio.run(worker.run({"job": job, "payload": {"url": "https://example.com/video", "topic": topic}})) == 0
    assert job["status"] == "completed"
    assert job["result"]["hooks"]
    if topic:
        assert job["result"]["selection_mode"] == "keyword"
    else:
        assert all(hook["selection_method"] == "Speech and keyword suggestion" for hook in job["result"]["hooks"])
