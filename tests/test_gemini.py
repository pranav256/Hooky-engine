"""Unit tests for hooky/core/gemini.py error classification + rotation.

No network access: the client and every model call are faked. Run:
    python -m pytest tests/test_gemini.py   (or: python tests/test_gemini.py)
"""
from __future__ import annotations

import contextlib
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hooky.core.gemini as g  # noqa: E402


@contextlib.contextmanager
def patched(primary="test-primary", fallbacks=("fb1", "fb2")):
    """Isolate gemini module state: fake config, no pacing, no logbook I/O."""
    saved = (g.GEMINI_MODEL, list(g.GEMINI_FALLBACK_MODELS), g.MIN_INTERVAL,
             g.BASE_DELAY, g._fallback_idx, list(g._rotation_log),
             set(g._dead_models), g._last_call, g._rate_limit_hits, g._logbook,
             g.get_client, g._call_model)
    g.GEMINI_MODEL = primary
    g.GEMINI_FALLBACK_MODELS = list(fallbacks)
    g.MIN_INTERVAL = 0.0
    g.BASE_DELAY = 0.0
    g._fallback_idx = -1
    g._rotation_log.clear()
    g._dead_models.clear()
    g._last_call = 0.0
    g._rate_limit_hits = 0
    g._logbook = lambda *a, **k: None
    try:
        yield
    finally:
        (g.GEMINI_MODEL, fallback_list, g.MIN_INTERVAL, g.BASE_DELAY,
         idx, rot, dead, g._last_call, g._rate_limit_hits,
         g._logbook, g.get_client, g._call_model) = saved
        g.GEMINI_FALLBACK_MODELS = fallback_list
        g._fallback_idx = idx
        g._rotation_log = rot
        g._dead_models = dead


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


def test_quota_match_bug_is_fixed() -> None:
    # 'quota' alone must NOT count as rate limiting (it is a generic API word);
    # regression test for the and/or precedence bug in the old matcher.
    assert not g._is_rate_limit_error(
        RuntimeError("error calling the api: malformed request"))
    assert g._is_rate_limit_error(RuntimeError("quota exceeded for the model"))


# --- rotation behaviour ------------------------------------------------------

def test_rotation_on_429() -> None:
    script = Script(**{"test-primary": [ERR_429], "fb1": ["ok"]})
    with patched() as _:
        g._call_model = script  # type: ignore[method-assign]
        g.get_client = lambda: object()  # type: ignore[assignment]
        assert g.generate("hi") == "ok"
        assert g.current_model() == "fb1"
        assert script.calls == ["test-primary", "fb1"]


def test_rotation_on_503_overload() -> None:
    script = Script(**{"test-primary": [ERR_503], "fb1": ["ok"]})
    with patched():
        g._call_model = script  # type: ignore[method-assign]
        g.get_client = lambda: object()  # type: ignore[assignment]
        assert g.generate("hi") == "ok"
        assert g.current_model() == "fb1"


def test_rotated_models_are_sticky() -> None:
    # after rotating, later calls skip straight to the working model
    script = Script(**{"test-primary": [ERR_429], "fb1": ["ok", "ok2"]})
    with patched():
        g._call_model = script  # type: ignore[method-assign]
        g.get_client = lambda: object()  # type: ignore[assignment]
        assert g.generate("one") == "ok"
        assert g.generate("two") == "ok2"
        assert script.calls == ["test-primary", "fb1", "fb1"]


def test_retired_models_are_skipped_forever() -> None:
    script = Script(**{"test-primary": [ERR_404_RETIRED],
                       "fb1": [ERR_404_RETIRED], "fb2": ["ok"]})
    with patched():
        g._call_model = script  # type: ignore[method-assign]
        g.get_client = lambda: object()  # type: ignore[assignment]
        assert g.generate("hi") == "ok"
        assert g._dead_models == {"test-primary", "fb1"}
        assert script.calls == ["test-primary", "fb1", "fb2"]
        # next call must not touch the dead models again
        assert g.generate("again") == "ok"
        assert script.calls[-1] == "fb2" and len(script.calls) == 4


def test_all_models_retired_raises_without_looping() -> None:
    script = Script(**{"test-primary": [ERR_404_RETIRED],
                       "fb1": [ERR_404_RETIRED], "fb2": [ERR_404_RETIRED]})
    with patched():
        g._call_model = script  # type: ignore[method-assign]
        g.get_client = lambda: object()  # type: ignore[assignment]
        try:
            g.generate("hi")
        except RuntimeError as exc:
            assert "model chain" in str(exc)
        else:
            raise AssertionError("expected RuntimeError")
        assert len(script.calls) == 3  # one attempt per model, no retries


def test_persistent_429_stops_hammering() -> None:
    script = Script(**{"test-primary": [ERR_429], "fb1": [ERR_429],
                       "fb2": [ERR_429]})
    with patched():
        g._call_model = script  # type: ignore[method-assign]
        g.get_client = lambda: object()  # type: ignore[assignment]
        g._rate_limit_hits = g._MAX_RATE_LIMIT_HITS  # already exhausted
        try:
            g.generate("hi")
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected RuntimeError")
        assert len(script.calls) == 1  # bailed out on the first 429


def test_auth_error_fails_fast() -> None:
    script = Script(**{"test-primary": [ERR_AUTH]})
    with patched():
        g._call_model = script  # type: ignore[method-assign]
        g.get_client = lambda: object()  # type: ignore[assignment]
        try:
            g.generate("hi")
        except RuntimeError as exc:
            assert "GEMINI_API_KEY" in str(exc)
        else:
            raise AssertionError("expected RuntimeError")
        assert script.calls == ["test-primary"]  # no retries, no rotation


# --- thinking trim + status --------------------------------------------------

def test_thinking_config_per_family() -> None:
    if not g._GENAI_AVAILABLE:
        return
    tc25 = g._thinking_config("gemini-2.5-flash")
    assert tc25 is not None and tc25.thinking_budget == 0
    tc38 = g._thinking_config("gemini-3.8-flash")
    assert tc38 is not None and str(tc38.thinking_level).endswith("LOW")
    assert g._thinking_config("mystery-model") is None


def test_rotation_status_message() -> None:
    with patched():
        assert "no rotation this run" in g.rotation_status()
        g._rotation_log.append("fb1")
        g._dead_models.add("test-primary")
        text = g.rotation_status()
        assert "fb1" in text and "test-primary" in text and "retired" in text


def test_pace_never_sleeps_with_zero_interval() -> None:
    with patched():
        g._pace("any-model")  # MIN_INTERVAL=0 in this context: returns instantly
        g._pace("any-model")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"GEMINI_TESTS_PASSED ({len(fns)} tests)")


