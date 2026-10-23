"""LLM diagnostic: times each candidate Gemini text model, then makes one real call
through the agent's client (which fails over between models automatically).

  python check_llm.py
"""
import time

import requests

import config
from agent import llm

print("Provider setting:", config.LLM_PROVIDER)
if config.LLM_PROVIDER == "gemini":
    if not config.GEMINI_API_KEY:
        raise SystemExit("GEMINI_API_KEY is empty — put it in .env next to run.py")
    cands = llm.gemini_candidates()
    print("Text models the agent will try, in order:", ", ".join(cands))
    print(f"\nTiming test ({config.LLM_TIMEOUT} s limit each):")
    for model in cands[:6]:
        body = {"contents": [{"parts": [{"text": 'Reply with JSON {"ok": true}'}]}],
                "generationConfig": {"responseMimeType": "application/json"}}
        start = time.time()
        try:
            r = requests.post(f"{llm.GEMINI_BASE}/models/{model}:generateContent", json=body,
                              timeout=config.LLM_TIMEOUT, headers={"x-goog-api-key": config.GEMINI_API_KEY})
            note = f"HTTP {r.status_code}" + ("" if r.ok else " " + r.text[:100].replace("\n", " "))
        except requests.RequestException as exc:
            note = type(exc).__name__
        print(f"  {model:<32} {time.time() - start:6.1f}s  {note}")

print("\nReal call through the agent (with automatic failover):")
start = time.time()
result, meta = llm.chat_json("Reply with JSON only.", 'Return {"ok": true}')
print("  Model used:", meta["provider"], f"| took {time.time() - start:.1f}s")
print("  Result:", result, "| error:", meta["error"])
if result and config.LLM_PROVIDER == "gemini":
    print(f"\nLLM is WORKING. Tip: put GEMINI_MODEL={llm.resolve_gemini_model()} in .env to skip slower models.")
elif not result:
    print("\nLLM call failed — the agent will use its fallback rules")
