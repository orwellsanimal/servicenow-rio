# rio/llm.py — the one Ollama client: native /api/chat, local only, capability-aware (ADR-0002).
#
# Carried from whitcortex llm_client.py and trimmed: no cloud fallback, no circuit breaker, no
# residency table. What stays is what the lab measured the hard way:
#   * native /api/chat, never /v1 (the /v1 endpoint silently ignores `think`);
#   * `think` only to a model that lists the `thinking` capability (Ollama answers 400 otherwise);
#   * `tools` only to a model that lists `tools`, refused with a reason when it does not;
#   * `num_ctx` always sent, `num_thread` never (whitcortex #1992);
#   * `num_predict` always sent, so a looping model cannot hold the runner until the timeout;
#   * the decode rate from Ollama's own eval_count / eval_duration on every reply, so the laptop's
#     speed is measured, not assumed.

from __future__ import annotations

import json
from dataclasses import dataclass, field

import requests

from rio import config


@dataclass
class Reply:
    content: str = ""
    ok: bool = True
    error: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    thinking: str = ""
    model: str = ""
    eval_count: int = 0
    eval_duration_ns: int = 0
    total_duration_ns: int = 0

    @property
    def tokens_per_s(self) -> float:
        return self.eval_count / (self.eval_duration_ns / 1e9) if self.eval_duration_ns else 0.0

    @property
    def seconds(self) -> float:
        return self.total_duration_ns / 1e9


_CAPABILITIES: dict[str, frozenset[str]] = {}


def capabilities(model: str | None = None, *, refresh: bool = False) -> frozenset[str]:
    """What the model supports, from its own /api/show: e.g. {"completion", "tools", "thinking"}.
    Raises on a model Ollama does not have; an empty set means Ollama predates the field."""
    model = model or config.MODEL
    if not refresh and model in _CAPABILITIES:
        return _CAPABILITIES[model]
    r = requests.post(f"{config.OLLAMA_URL}/api/show", json={"model": model}, timeout=30)
    if r.status_code == 404:
        raise RuntimeError(f"Ollama has no model called {model!r}: `ollama pull {model}` first.")
    r.raise_for_status()
    caps = frozenset(str(c) for c in (r.json().get("capabilities") or []))
    _CAPABILITIES[model] = caps
    return caps


def chat(messages: list[dict], *, model: str | None = None, tools: list[dict] | None = None,
         format: dict | str | None = None, think: bool | None = None,
         num_ctx: int | None = None, timeout: int | None = None) -> Reply:
    """One non-streaming turn. Never raises on a model or transport failure: the Reply says
    `ok=False` with the reason, so the caller decides what the user sees."""
    model = model or config.MODEL
    try:
        caps = capabilities(model)
    except (requests.RequestException, RuntimeError, ValueError) as e:
        return Reply(ok=False, error=str(e), model=model)
    if tools and "tools" not in caps:
        return Reply(ok=False, model=model,
                     error=f"{model} does not list the 'tools' capability; a turn with tools cannot run on it.")
    body: dict = {"model": model, "messages": messages, "stream": False,
                  "options": {"num_ctx": num_ctx or config.NUM_CTX, "num_predict": config.NUM_PREDICT}}
    if tools:
        body["tools"] = tools
    if format:
        body["format"] = format
    if think is not None and "thinking" in caps:
        body["think"] = think          # a model without `thinking` gets no flag at all
    try:
        r = requests.post(f"{config.OLLAMA_URL}/api/chat", json=body, timeout=timeout or config.LLM_TIMEOUT)
    except requests.RequestException as e:
        return Reply(ok=False, error=f"Ollama at {config.OLLAMA_URL} did not answer: {e}", model=model)
    if r.status_code != 200:
        return Reply(ok=False, model=model, error=f"Ollama answered {r.status_code}: {r.text[:300]}")
    try:
        data = r.json()
    except json.JSONDecodeError as e:
        return Reply(ok=False, error=f"Ollama's reply was not JSON: {e}", model=model)
    msg = data.get("message") or {}
    return Reply(content=str(msg.get("content") or ""), tool_calls=list(msg.get("tool_calls") or []),
                 thinking=str(msg.get("thinking") or ""), model=str(data.get("model") or model),
                 eval_count=int(data.get("eval_count") or 0),
                 eval_duration_ns=int(data.get("eval_duration") or 0),
                 total_duration_ns=int(data.get("total_duration") or 0))
