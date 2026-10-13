# 0002. Local-only inference, capability-aware, sized to a CPU laptop

- **Status:** Accepted
- **Date:** 2026-10-13
- **Deciders:** Jody (operator: local only; qwen3 as the baseline), agent (measurement)

## Context

The target machine is a Dell Pro 14 Plus: AMD Ryzen 5 PRO 230 with Radeon 760M integrated
graphics, 32 GB RAM (21.6 GB free at the time of reading), Windows 11 Enterprise with App Control
for Business enforced, Credential Guard and VBS on. Ollama on Windows does not accelerate that iGPU,
so inference runs on six cores from system RAM. whitcortex's lab box decodes `qwen3:14b` at ~44
tokens per second on a 16 GB discrete GPU; the same model here will be low single digits, and
prompt processing of a long system prompt plus tool schemas is the larger cost per turn.

Three Ollama features whitcortex relies on vary by model template: native tool calling (`tools`),
the `think` flag (Ollama answers 400 when it is sent to a model without a thinking capability), and
structured output (`format` with a JSON schema, grammar-constrained, so it works for any model).

## Options considered

1. **Carry whitcortex's `llm_client` as is.** Rejected: 2,577 lines, a Gemini fallback with a
   circuit breaker, a per-model residency table, and 17 qwen-specific branches; the fallback alone
   contradicts "ticket data stays on the machine".
2. **Hard-code the laptop's model and its flags.** Rejected: the operator already has a different
   model installed (a gemma) and will measure it; a client that special-cases names cannot say
   why a different model fails.
3. **(chosen) A small client that asks the model what it supports.** `/api/show` returns the
   model's `capabilities`; the client sends `think` only when `thinking` is listed and refuses
   `tools` when `tools` is not, with a plain reason. Every reply carries the decode rate from
   Ollama's own `eval_count` / `eval_duration`, so the laptop's speed is measured on every turn, not
   assumed.

## Decision

- `rio/llm.py` talks to Ollama's native `/api/chat` only (the `/v1` endpoint ignores `think`;
  whitcortex measured that). No cloud client exists in the repo.
- Defaults: `MODEL=qwen3:14b` as the baseline, `NUM_CTX=8192`, `keep_alive` left to Ollama. The
  client never sends `num_thread` (whitcortex #1992: the key alone cost 25–50x on the lab box).
- The chat loop prints the model's capabilities on start and tokens per second on every reply.
  The first measurement on the laptop decides between `qwen3:14b` and `qwen3:8b`; `qwen3:4b` is
  the candidate for the two schema-constrained calls (triage's classifier, enrichment's terms).
- Dependencies are pure Python until an import is measured on the laptop under App Control.

## Consequences

- **Positive:** the client is ~150 lines and testable with a stubbed `requests.post`; a model
  swap is a `.env` change plus a measurement; nothing leaves the machine.
- **Negative:** no fallback means an Ollama outage is a visible failure, not a slower answer. That
  is the intended behaviour on a corporate machine.
- **Open:** whether Ollama's Vulkan path ever lights the 760M up; the serve log's inference
  compute line (`library=cpu` today) is where that would show.
