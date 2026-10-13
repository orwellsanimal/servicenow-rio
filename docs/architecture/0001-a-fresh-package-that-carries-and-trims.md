# 0001. Rio is a fresh package that carries and trims, not a fork of whitcortex

- **Status:** Accepted
- **Date:** 2026-10-13
- **Deciders:** Jody (operator: did not want the lab tree with "a bunch of stuff I will have to disable"), agent (measurement)

## Context

The operator wants a ServiceNow engineering companion on a managed corporate laptop, running on
Ollama, combining whitcortex's ServiceNow tools and core "brain" with servicenow-coder's pipeline.
Measured on 2026-10-13:

- whitcortex's core is ~12,000 lines across seven modules (`llm_client.py` 2,577, `agent/runner.py`
  3,618, `agent/router.py` 1,546, `mono_scanner.py` 1,545, `config.py` 730, `tools/__init__.py`
  528, `memory.py` 1,978), with the model assumptions concentrated in four of them (config 33
  `qwen` mentions, the client 17, the runner 15, the scanner 7). Its ServiceNow surface is ~3,100
  lines in eight files and is model-agnostic apart from two structured local calls.
- servicenow-coder is a pnpm monorepo for the Fluent SDK pipeline with no local inference client
  (Gemini 37 mentions, OpenAI 4, two "OLLAMA" strings in docs about GPU host CIs). Its two MCP
  servers, `now-data` and `now-config`, are Python packages over PySNC.
- The laptop has no discrete GPU and App Control for Business enforced (ADR-0002).

## Options considered

1. **Clone whitcortex and disable what is not needed.** Rejected by the operator: the research
   pipeline, routine engine, Chroma memory, Streamlit admin, Next.js client, GitHub SDLC automation
   and PowerShell fleet are most of the tree, each with its own startup and dependencies, and the
   feature flags gate tools, not subsystems.
2. **Vendor whitcortex as a git subtree and build on top.** Rejected: it keeps every dependency
   (chromadb, langgraph, FastAPI, streamlit) in the install, which is exactly the App Control risk
   ADR-0002 avoids, and it keeps the qwen-coupled scanner and client in the import path.
3. **(chosen) A fresh package.** Each carried module is copied, trimmed to what rio calls, and
   re-tested here against the same fakes. The carried modules keep their issue numbers in comments
   so the evidence that shaped them stays reachable.

## Decision

`rio/` is a new Python package. It carries, in this order:

1. The Ollama client (`rio/llm.py`): capability-aware, local only, no fallback.
2. The ServiceNow tools and graphs (`tools/servicenow.py`, `sn_triage`, `sn_resolve`,
   `sn_enrich`, the status card) with their tests and the fake instance.
3. The tool registry with `execute_tool`'s gates (typed errors, isolation), the approval card for
   writes, and the operator commands (`apply triage <INC>`, a bare record number).
4. servicenow-coder's `now-data` / `now-config` as libraries, once their packaging contract is
   stated (servicenow-coder issue).
5. The Fluent SDK pipeline entry point, last.

It leaves behind: the research pipeline, the routine engine and `daily_ops`, the Chroma memory,
the mono scanner, the Streamlit admin, the Next.js client, the GitHub SDLC automation, the
PowerShell watcher fleet, and the Gemini client.

## Consequences

- **Positive:** the install is small and pure Python on day one; nothing starts that the laptop
  does not need; every carried behaviour has a test here, not only in the lab.
- **Negative:** fixes land twice when a carried module changes upstream. The comments' issue
  numbers are the trail for porting them; there is no automatic sync, by design.
- **Open:** whether the Next.js client comes later for the phone-sized cards (whitcortex #2643
  chose markdown cards precisely so a client is optional).
