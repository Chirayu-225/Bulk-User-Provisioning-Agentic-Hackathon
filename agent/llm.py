"""Thin LLM client that always returns parsed JSON (or None on failure).

Providers: Gemini REST API (free tier key) or any OpenAI-compatible server
(Ollama on your own GPU, Groq, etc.). Callers must handle None by falling
back to deterministic logic, so the pipeline never depends on the LLM being up.
"""
import json
import re
import time

import requests

import config


def _extract_json(text: str):
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def provider_label() -> str:
    if config.LLM_PROVIDER == "gemini" and config.GEMINI_API_KEY:
        return f"gemini:{_resolved_gemini_model or config.GEMINI_MODEL}"
    if config.LLM_PROVIDER == "openai":
        return f"openai-compatible:{config.OPENAI_MODEL}"
    return "none (deterministic fallback)"


def chat_json(system: str, user: str) -> tuple[dict | None, dict]:
    """Returns (parsed_json_or_None, meta). meta has provider, latency_ms and error if any."""
    meta = {"provider": provider_label(), "latency_ms": None, "error": None}
    started = time.time()
    try:
        if config.LLM_PROVIDER == "gemini" and config.GEMINI_API_KEY:
            raw = _gemini(system, user)
        elif config.LLM_PROVIDER == "openai":
            raw = _openai_compat(system, user)
        else:
            meta["error"] = "no LLM configured"
            return None, meta
        meta["provider"] = provider_label()  # may have changed if a retired model was swapped out
        result = _extract_json(raw)
        meta["latency_ms"] = int((time.time() - started) * 1000)
        return result, meta
    except Exception as exc:  # network, quota, bad JSON — caller falls back
        meta["error"] = f"{type(exc).__name__}: {exc}"[:300]
        meta["latency_ms"] = int((time.time() - started) * 1000)
        return None, meta


def _post_with_retry(url: str, **kwargs) -> requests.Response:
    for attempt in range(3):
        resp = requests.post(url, timeout=config.LLM_TIMEOUT, **kwargs)
        if resp.status_code in (429, 500, 502, 503) and attempt < 2:
            time.sleep(2 * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()
    return resp


GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
_resolved_gemini_model: str | None = None   # last model that answered
_bad_models: set[str] = set()               # models that hung or errored this session
SKIP_WORDS = ("image", "tts", "audio", "live", "embedding", "vision", "thinking", "exp", "learnlm", "gemma")


def _version_key(name: str) -> tuple:
    nums = re.findall(r"\d+(?:\.\d+)?", name)
    return tuple(float(n) for n in nums) or (0.0,)


def available_gemini_models() -> list[str]:
    resp = requests.get(f"{GEMINI_BASE}/models", params={"pageSize": 200},
                        headers={"x-goog-api-key": config.GEMINI_API_KEY}, timeout=20)
    resp.raise_for_status()
    return [m["name"].split("/", 1)[-1] for m in resp.json().get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])]


def gemini_candidates(names: list[str] | None = None) -> list[str]:
    """Text models to try, best first: GEMINI_MODEL, then stable flash (newest first),
    then flash-lite, then previews. Speech/image/embedding models are excluded."""
    try:
        names = names if names is not None else available_gemini_models()
    except requests.RequestException:
        return [config.GEMINI_MODEL]
    text = [n for n in names if n.startswith("gemini") and not any(w in n for w in SKIP_WORDS)]
    newest = lambda pool: sorted(pool, key=_version_key, reverse=True)
    ordered = ([config.GEMINI_MODEL] if config.GEMINI_MODEL in text else []) + \
        newest([n for n in text if "flash" in n and "lite" not in n and "preview" not in n]) + \
        newest([n for n in text if "flash" in n and "lite" in n and "preview" not in n]) + \
        newest([n for n in text if "flash" in n and "preview" in n])
    out = []
    for n in ordered:
        if n not in out:
            out.append(n)
    return out or [config.GEMINI_MODEL]


def discover_gemini_model(names: list[str] | None = None) -> str | None:
    cands = gemini_candidates(names)
    return cands[0] if cands else None


def resolve_gemini_model() -> str:
    if _resolved_gemini_model:
        return _resolved_gemini_model
    for m in gemini_candidates():
        if m not in _bad_models:
            return m
    return config.GEMINI_MODEL


def _gemini(system: str, user: str) -> str:
    """Tries the last working model first; if a model hangs, is retired, or is overloaded,
    moves to the next candidate and remembers the one that answers."""
    global _resolved_gemini_model
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    headers = {"x-goog-api-key": config.GEMINI_API_KEY}
    order = ([_resolved_gemini_model] if _resolved_gemini_model else []) + \
        [m for m in gemini_candidates() if m != _resolved_gemini_model and m not in _bad_models]
    last_exc: Exception | None = None
    for model in order[:6]:
        try:
            resp = requests.post(f"{GEMINI_BASE}/models/{model}:generateContent", json=body,
                                 headers=headers, timeout=config.LLM_TIMEOUT)
            if resp.status_code in (404, 429, 500, 502, 503, 504):
                resp.raise_for_status()
            resp.raise_for_status()
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError, KeyError, IndexError) as exc:
            last_exc = exc
            _bad_models.add(model)
            if _resolved_gemini_model == model:
                _resolved_gemini_model = None
            continue
        _resolved_gemini_model = model
        return text
    raise last_exc or RuntimeError("no Gemini model available")


def _openai_compat(system: str, user: str) -> str:
    url = config.OPENAI_BASE_URL.rstrip("/") + "/chat/completions"
    body = {
        "model": config.OPENAI_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    resp = _post_with_retry(url, json=body, headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"})
    return resp.json()["choices"][0]["message"]["content"]
