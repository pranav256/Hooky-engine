"""Offline Gemini error classification, pacing, and model rotation."""
from __future__ import annotations

import pytest

from hooky.core import gemini as g


@pytest.fixture(autouse=True)
def isolated_gemini(monkeypatch):
    for name, value in {
        "GEMINI_MODEL": "test-primary", "GEMINI_FALLBACK_MODELS": ["fb1", "fb2"],
        "MIN_INTERVAL": 0.0, "BASE_DELAY": 0.0, "_fallback_idx": -1,
        "_dead_models": set(), "_last_call": 0.0,
        "_rate_limit_hits": 0,
    }.items():
        monkeypatch.setattr(g, name, value)
    monkeypatch.setattr(g, "_logbook", lambda *a, **k: None)
    monkeypatch.setattr(g, "get_client", lambda: object())


class Script:
    """Fake _call_model: per-model list of outcomes; the last one repeats."""

    def __init__(self, **by_model):
        self.by_model = {k: list(v) for k, v in by_model.items()}
        self.calls: list[str] = []

    def __call__(self, client, model, contents, temperature, max_output_tokens):
        self.calls.append(model)
        outcomes = self.by_model.setdefault(model, ["ok"])
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


ERR_429 = RuntimeError("429 RESOURCE_EXHAUSTED quota exceeded for model")
ERR_503 = RuntimeError("503 UNAVAILABLE 'This model is currently experiencing "
                       "high demand. Please try again later.'")
ERR_404_RETIRED = RuntimeError("404 NOT_FOUND 'This model models/x is no "
                               "longer available. Please update your code.'")
ERR_404_OTHER = RuntimeError("404 NOT_FOUND 'model not found'")
ERR_AUTH = RuntimeError("401 API key not valid. Please pass a valid API key.")


# --- error classification ----------------------------------------------------

def test_classification() -> None:
    assert g._is_rate_limit_error(ERR_429)
    assert g._is_rate_limit_error(RuntimeError("daily quota exceeded"))
    assert not g._is_rate_limit_error(ERR_503)
    assert not g._is_rate_limit_error(ERR_404_RETIRED)
    assert g._is_unavailable_error(ERR_503)
    assert not g._is_unavailable_error(ERR_429)
    assert g._is_retired_model_error(ERR_404_RETIRED)
    assert not g._is_retired_model_error(ERR_404_OTHER)


def test_generic_api_error_is_not_a_rate_limit() -> None:
    # Generic API errors must not trigger model rotation.
    assert not g._is_rate_limit_error(
        RuntimeError("error calling the api: malformed request"))
    assert g._is_rate_limit_error(RuntimeError("quota exceeded for the model"))


# --- rotation behaviour ------------------------------------------------------

@pytest.mark.parametrize("failure", [ERR_429, ERR_503], ids=["quota", "overload"])
def test_rotation_on_transient_error(monkeypatch, failure) -> None:
    script = Script(**{"test-primary": [failure], "fb1": ["ok"]})
    monkeypatch.setattr(g, "_call_model", script)
    assert g.generate("hi") == "ok"
    assert g.current_model() == "fb1"
    assert script.calls == ["test-primary", "fb1"]


def test_rotated_models_are_sticky(monkeypatch) -> None:
    # after rotating, later calls skip straight to the working model
    script = Script(**{"test-primary": [ERR_429], "fb1": ["ok", "ok2"]})
    monkeypatch.setattr(g, "_call_model", script)
    assert g.generate("one") == "ok"
    assert g.generate("two") == "ok2"
    assert script.calls == ["test-primary", "fb1", "fb1"]


def test_retired_models_are_skipped_forever(monkeypatch) -> None:
    script = Script(**{"test-primary": [ERR_404_RETIRED],
                       "fb1": [ERR_404_RETIRED], "fb2": ["ok"]})
    monkeypatch.setattr(g, "_call_model", script)
    assert g.generate("hi") == "ok"
    assert g._dead_models == {"test-primary", "fb1"}
    assert script.calls == ["test-primary", "fb1", "fb2"]
    # next call must not touch the dead models again
    assert g.generate("again") == "ok"
    assert script.calls[-1] == "fb2" and len(script.calls) == 4


def test_all_models_retired_raises_without_looping(monkeypatch) -> None:
    script = Script(**{"test-primary": [ERR_404_RETIRED],
                       "fb1": [ERR_404_RETIRED], "fb2": [ERR_404_RETIRED]})
    monkeypatch.setattr(g, "_call_model", script)
    with pytest.raises(RuntimeError, match="model chain"):
        g.generate("hi")
    assert len(script.calls) == 3  # one attempt per model, no retries


def test_persistent_429_stops_hammering(monkeypatch) -> None:
    script = Script(**{"test-primary": [ERR_429], "fb1": [ERR_429],
                       "fb2": [ERR_429]})
    monkeypatch.setattr(g, "_call_model", script)
    g._rate_limit_hits = g._MAX_RATE_LIMIT_HITS  # already exhausted
    with pytest.raises(RuntimeError):
        g.generate("hi")
    assert len(script.calls) == 1  # bailed out on the first 429


def test_auth_error_fails_fast(monkeypatch) -> None:
    script = Script(**{"test-primary": [ERR_AUTH]})
    monkeypatch.setattr(g, "_call_model", script)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        g.generate("hi")
    assert script.calls == ["test-primary"]  # no retries, no rotation


# --- thinking trim ----------------------------------------------------------

def test_thinking_config_per_family() -> None:
    if not g._load_genai():
        pytest.skip("google-genai is not installed")
    tc25 = g._thinking_config("gemini-2.5-flash")
    assert tc25 is not None and tc25.thinking_budget == 0
    tc38 = g._thinking_config("gemini-3.8-flash")
    assert tc38 is not None and str(tc38.thinking_level).endswith("LOW")
    assert g._thinking_config("mystery-model") is None


def test_pace_never_sleeps_with_zero_interval(monkeypatch) -> None:
    monkeypatch.setattr(g.time, "time", lambda: 42.0)
    monkeypatch.setattr(g.time, "sleep", lambda seconds: pytest.fail("unexpected pacing delay"))
    g._pace()
    g._pace()
    assert g._last_call == 42.0
