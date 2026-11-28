"""Shared Gemini helpers: one client, free-tier-friendly retries + model rotation.

Free-tier rate limits are per MODEL, so when the primary model is exhausted
(429 RESOURCE_EXHAUSTED) or unavailable (503 overloaded) the next model in
GEMINI_MODEL_FALLBACKS is tried at runtime — no restart, no .env edit. The
rotation is sticky for the rest of the process: once switched, later calls
stay on the working model until it too fails.

Retired models (404 — Google removes old ones over time) are skipped
immediately and remembered, so a dead model in the fallback list never
burns retries or gets picked again.
"""
from __future__ import annotations

import random
import sys
import time

from ..config import (GEMINI_API_KEY, GEMINI_FALLBACK_MODELS, GEMINI_MIN_INTERVAL,
                      GEMINI_MODEL)
from .logbook import log as _logbook

# The Google SDK is optional and expensive to import. Keep commands such as
# `--help`, queue inspection, and local cutting fast by loading it only when a
# Gemini operation is actually requested.
genai = None
_genai_types = None
_GENAI_AVAILABLE: bool | None = None


def _load_genai() -> bool:
    global genai, _genai_types, _GENAI_AVAILABLE
    if _GENAI_AVAILABLE is not None:
        return bool(_GENAI_AVAILABLE)
    try:
        from google import genai as _genai
        from google.genai import types as _types
    except ImportError:
        _GENAI_AVAILABLE = False
        return False
    genai = _genai
    _genai_types = _types
    _GENAI_AVAILABLE = True
    return True

MAX_RETRIES = 4
BASE_DELAY = 4.0  # seconds; exponential backoff multiplier for transient errors

# Free-tier safety: never send two requests closer together than this. Reports
# of Gemini's free-tier RPM vary wildly (2-15 RPM) and Google no longer
# publishes fixed numbers (limits now live in AI Studio), so pacing stays
# conservative: 20s spacing guarantees staying under the strictest published
# limit while a noon batch of ~2 calls per video still processes several
# videos per hour.
MIN_INTERVAL = GEMINI_MIN_INTERVAL

# 429 quotas reset at midnight Pacific; a run started before that would keep
# hammering exhausted models forever. After this many 429s in a row across the
# run (rotation included — any success resets the count), give up cleanly.
_MAX_RATE_LIMIT_HITS = 6

_last_call = 0.0  # module-level timestamp of the previous request
_rate_limit_hits = 0  # 429s since the last successful call (run-wide)

# --- runtime model rotation -------------------------------------------------
# Rate limits (429) and overloads (503) are per model, so exhausting one
# doesn't have to stop the run: GEMINI_FALLBACK_MODELS is tried in order.
# Current index into that list; -1 = still on GEMINI_MODEL (the .env primary).
_fallback_idx = -1
_dead_models: set[str] = set()  # 404-retired: never retried this process

# Models that need thinking disabled/trimmed to avoid burning output tokens
# on reasoning (which starves the actual response under max_output_tokens).
_THINKING_BUDGET_ZERO = {"gemini-2.5-flash", "gemini-2.5-flash-lite"}
_THINKING_LEVEL = {"gemini-3.8-flash": "low", "gemini-3.7-flash": "low",
                   "gemini-3.6-flash": "low", "gemini-3.5-flash": "low",
                   "gemini-3.1-flash-lite": "low", "gemini-3-flash-preview": "low"}


def current_model() -> str:
    """The model the next call will use (primary, or a sticky fallback)."""
    global _fallback_idx
    if 0 <= _fallback_idx < len(GEMINI_FALLBACK_MODELS):
        m = (GEMINI_FALLBACK_MODELS[_fallback_idx] or "").strip()
        if m:
            return m
    return (GEMINI_MODEL or "").strip() or "gemini-3.8-flash"


def _skip_dead() -> None:
    """Advance past any fallback models already known to be retired (404)."""
    global _fallback_idx
    while (0 <= _fallback_idx < len(GEMINI_FALLBACK_MODELS)
           and GEMINI_FALLBACK_MODELS[_fallback_idx] in _dead_models):
        _fallback_idx += 1


def _is_rate_limit_error(exc: Exception) -> bool:
    """Detect free-tier exhaustion (429 / RESOURCE_EXHAUSTED) from any client lib shape."""
    text = f"{type(exc).__name__} {exc}".lower()
    return ("429" in text or "resource_exhausted" in text
            or "rate limit" in text or "quota" in text and "api" not in text)


def _is_unavailable_error(exc: Exception) -> bool:
    """Detect model overload (503 UNAVAILABLE / 'high demand')."""
    text = f"{type(exc).__name__} {exc}".lower()
    return "503" in text or "unavailable" in text or "high demand" in text


def _is_retired_model_error(exc: Exception) -> bool:
    """Detect a model Google has removed (404 'no longer available')."""
    text = f"{type(exc).__name__} {exc}".lower()
    return ("404" in text or "not_found" in text) and "no longer" in text


def _advance_model() -> bool:
    """Move to the next usable fallback model. True when there is one to try."""
    global _fallback_idx
    nxt = _fallback_idx + 1
    if nxt >= len(GEMINI_FALLBACK_MODELS):
        return False
    _fallback_idx = nxt
    _skip_dead()
    return 0 <= _fallback_idx < len(GEMINI_FALLBACK_MODELS)


def _mark_retired(model: str) -> None:
    _dead_models.add(model)
    print(f"[gemini] {model} has been retired by Google (404) — "
          f"removing it from GEMINI_MODEL_FALLBACKS in your .env is recommended",
          file=sys.stderr)


def _pace() -> None:
    """Sleep so consecutive Gemini calls stay >= MIN_INTERVAL apart."""
    global _last_call
    wait = MIN_INTERVAL - (time.time() - _last_call)
    if wait > 0:
        print(f"[gemini] pacing: waiting {wait:.0f}s (free-tier RPM safety)",
              file=sys.stderr)
        time.sleep(wait)
    _last_call = time.time()


def _reset_pacing_clock() -> None:
    """Let the next call go immediately (used after switching models)."""
    global _last_call
    _last_call = 0.0


class GeminiNotConfigured(RuntimeError):
    pass


def check_available() -> None:
    if not _load_genai():
        print("[gemini] google-genai not installed. Run: uv sync --locked --extra all",
              file=sys.stderr)
        raise GeminiNotConfigured("google-genai is not installed. Run: uv sync --locked --extra all")
    if not GEMINI_API_KEY:
        print("[gemini] GEMINI_API_KEY not set. Free key: https://aistudio.google.com/apikey",
              file=sys.stderr)
        raise GeminiNotConfigured("GEMINI_API_KEY is not set. Add a key in Studio Settings or .env.")


def get_client():
    check_available()
    assert genai is not None
    return genai.Client(api_key=GEMINI_API_KEY)


def _thinking_config(model: str):
    """Per-family thinking trim: free tier pays for thinking tokens too.

    - 2.5 family: thinking_budget=0 disables thinking entirely.
    - 3.x family: 'minimal' is rejected by 3.8, so use thinking_level=low.
    Both stop the model from silently eating max_output_tokens on reasoning.
    """
    if not _load_genai():
        return None
    assert _genai_types is not None
    if model in _THINKING_BUDGET_ZERO:
        return _genai_types.ThinkingConfig(thinking_budget=0)
    level = _THINKING_LEVEL.get(model)
    if level:
        return _genai_types.ThinkingConfig(thinking_level=level)
    return None


def _call_model(client, model: str, contents: list, temperature: float,
                max_output_tokens: int) -> str:
    """One generate_content attempt on a specific model (no retry logic)."""
    if not _load_genai():
        raise GeminiNotConfigured("google-genai is not installed")
    assert _genai_types is not None
    thinking = _thinking_config(model)
    config_kwargs: dict = {
        "temperature": temperature,
        "max_output_tokens": max_output_tokens,
    }
    if thinking is not None:
        config_kwargs["thinking_config"] = thinking
    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=_genai_types.GenerateContentConfig(**config_kwargs),
    )
    return response.text or ""


def list_models(client=None) -> list[str]:
    """Model IDs this API key can see (used by --check-gemini). Empty on error."""
    client = client or get_client()
    try:
        return [getattr(m, "name", "?") for m in client.models.list()]
    except Exception:
        return []


def generate(prompt: str, *, parts: list | None = None,
             temperature: float = 0.4, max_output_tokens: int = 2048) -> str:
    """Call Gemini with retry/backoff AND runtime model rotation.

    - 429/quota  -> next model in GEMINI_FALLBACK_MODELS (fresh quota bucket)
    - 503 / high demand -> next model (the current one is overloaded)
    - 404 'no longer available' -> model retired; skip it forever this process
    - anything else -> exponential backoff on the same model
    Rotation is sticky: once the run has moved to a fallback, it stays there
    until that model also fails.
    """
    client = get_client()
    contents: list = [prompt] + list(parts or [])
    last_exc: Exception | None = None
    global _rate_limit_hits

    for attempt in range(MAX_RETRIES):
        _skip_dead()
        if _fallback_idx >= len(GEMINI_FALLBACK_MODELS):
            break  # every fallback (and the primary) is dead — give up below
        model = current_model()
        _pace()
        try:
            text = _call_model(client, model, contents, temperature,
                               max_output_tokens)
            _rate_limit_hits = 0  # success: the run is healthy again
            return text
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # free tier: 429/503/404s and 5xx are common
            last_exc = exc
            text = f"{type(exc).__name__} {exc}".lower()
            if "api key" in text or "api_key" in text or "permission" in text \
                    or "unauthenticated" in text or "401" in text \
                    or "403" in text:
                raise RuntimeError(
                    f"Gemini rejected the API key / permissions ({exc}) — "
                    f"check GEMINI_API_KEY in .env") from exc
            if "model is required" in text:
                if _advance_model():
                    print(f"[gemini] empty or invalid model -> switching to {current_model()}",
                          file=sys.stderr)
                    _reset_pacing_clock()
                    continue
                model = "gemini-3.8-flash"
            if _is_rate_limit_error(exc):
                # count EVERY 429 (even ones rotation absorbs): several in a
                # row across the run means every bucket is empty — stop.
                _rate_limit_hits += 1
                if _rate_limit_hits >= _MAX_RATE_LIMIT_HITS:
                    break  # quota window reset is hours away — stop hammering
            if _is_retired_model_error(exc):
                _mark_retired(model)
                if _advance_model():
                    print(f"[gemini] {model} retired -> switching to "
                          f"{current_model()} for the rest of this run",
                          file=sys.stderr)
                    _logbook("info", f"Gemini model {model} retired (404); "
                                     f"switched to {current_model()}")
                    _reset_pacing_clock()
                    continue  # try the next model right away
                break  # nothing left to rotate into
            if (_is_rate_limit_error(exc) or _is_unavailable_error(exc)) \
                    and _advance_model():
                kind = "rate-limited" if _is_rate_limit_error(exc) \
                    else "overloaded"
                new_model = current_model()
                print(f"[gemini] {model} {kind} -> switching to "
                      f"{new_model} for the rest of this run", file=sys.stderr)
                _logbook("info", f"Gemini model {model} {kind}; "
                                 f"switched to {new_model}")
                _reset_pacing_clock()
                continue  # try the next model right away (fresh bucket)
            wait = BASE_DELAY * (2 ** attempt) + random.uniform(0, 2)
            msg = (f"attempt {attempt + 1}/{MAX_RETRIES} failed: {exc} "
                   f"-> retrying in {wait:.0f}s")
            print(f"[gemini] {msg}", file=sys.stderr)
            _logbook("wait", f"Gemini busy, retry in {wait:.0f}s ({exc.__class__.__name__})")
            time.sleep(wait)

    tried = [GEMINI_MODEL] + list(GEMINI_FALLBACK_MODELS)
    raise RuntimeError(
        f"Gemini failed after {MAX_RETRIES} attempts (model chain: "
        f"{' -> '.join(tried)}): {last_exc}")


def make_image_part(data: bytes, mime_type: str = "image/jpeg"):
    check_available()
    assert _genai_types is not None
    return _genai_types.Part.from_bytes(data=data, mime_type=mime_type)


def check_gemini() -> int:
    """--check-gemini: verify key, list models, live-test the chain. 0 = ok.

    Diagnostic only: calls each model in the chain once, in parallel (bypasses
    the free-tier pacer deliberately — this is a tool run, not a batch).
    """
    import concurrent.futures

    available = _load_genai()
    print("=== Gemini health check ===")
    print(f"  library:  google-genai {'installed' if available else 'MISSING'}")
    print(f"  api key:  {'present' if GEMINI_API_KEY else 'MISSING'}")
    chain = [GEMINI_MODEL] + list(GEMINI_FALLBACK_MODELS)
    print(f"  chain:    {' -> '.join(chain)}")

    try:
        client = get_client()
    except GeminiNotConfigured:
        return 1

    visible = set(list_models(client))
    if visible:
        print(f"  key is valid — {len(visible)} model(s) visible\n")
    else:
        print("  could not list models (key or network problem)\n")

    def probe(model: str):
        try:
            _call_model(client, model, "Reply with the single word: ok", 0.0, 20)
            return model, "OK", ""
        except Exception as exc:
            if _is_retired_model_error(exc):
                return model, "RETIRED", str(exc)[:90]
            if _is_rate_limit_error(exc):
                return model, "QUOTA (429)", str(exc)[:90]
            if _is_unavailable_error(exc):
                return model, "OVERLOADED (503)", str(exc)[:90]
            return model, "FAIL", str(exc)[:90]

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(chain)) as pool:
        futures = [pool.submit(probe, m) for m in chain]
        for fut in concurrent.futures.as_completed(futures):
            results.append(fut.result())
    results.sort(key=lambda r: chain.index(r[0]))

    any_ok = False
    for model, status, detail in results:
        mark = {"OK": "✓"}.get(status, "✗")
        if status == "OK":
            any_ok = True
        print(f"  {mark} {model:<24} {status}" + (f" — {detail}" if detail else ""))

    print()
    if not any_ok:
        print("  No model in the chain works right now. Fix in .env:")
        print("    GEMINI_MODEL=gemini-3.8-flash          # primary active model")
        print("    GEMINI_MODEL_FALLBACKS=gemini-3.1-flash-lite,gemini-flash-latest")
        return 1
    if any(s == "RETIRED" for _, s, _ in results):
        print("  A retired model is in your chain — remove it from .env "
              "(GEMINI_MODEL_FALLBACKS) to stop the skip warnings.")
    print("  At least one model works — Hooky will auto-rotate onto it.")
    return 0

