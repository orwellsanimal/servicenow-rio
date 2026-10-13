# servicenow-rio

A Python harness for Ollama as a ServiceNow engineering companion on Windows. Local inference
only; one process; nothing that needs elevation.

Rio combines three things that already exist:

| From | What | Why |
|---|---|---|
| [whitcortex](https://github.com/orwellsanimal/whitcortex) | the ServiceNow tools (`tools/servicenow.py`, the triage / enrich / resolve graphs, the status card), the tool registry with its dispatch gates, the approval card for writes, the operator commands | they are model-agnostic apart from two structured local calls, and they are tested against a fake instance |
| [servicenow-coder](https://github.com/orwellsanimal/servicenow-coder) | the `now-data` and `now-config` MCP servers (Python, PySNC), the instance-config snapshot | live-instance verbs and the dictionary, written once, reusable as a library |
| whitcortex's "brain" | the Ollama client, the chat loop, the routing of a turn to tools | trimmed to what a laptop needs: no Gemini fallback, no research pipeline, no routine engine, no vector memory, no admin UI |

Rio is **not** a fork of whitcortex. The lab tree carries a research pipeline, a routine engine, a
safety scanner tuned to one model, a Streamlit admin, a Next.js client and a PowerShell fleet;
none of that comes along. Each carried module is copied, trimmed and re-tested here
([ADR-0001](docs/architecture/0001-a-fresh-package-that-carries-and-trims.md)).

## The machine it is built for

A managed corporate laptop (Dell Pro 14 Plus, Ryzen 5 PRO 230 with Radeon 760M integrated
graphics, 32 GB RAM, Windows 11 Enterprise, App Control for Business enforced). That sets four
rules ([ADR-0002](docs/architecture/0002-local-only-inference-sized-to-the-cpu.md)):

1. **CPU inference.** Ollama does not accelerate that iGPU, so every token comes from six cores.
   Short system prompts, a small tool menu per turn, `num_ctx` of 8192 not 16k, and never send
   `num_thread`.
2. **Local only.** No cloud fallback exists in the client. Ticket data stays on the machine.
3. **Capability-aware.** The client asks `/api/show` what the model supports and sends `think`
   and `tools` only when it does. qwen3 is the baseline; any other model must pass the same
   measurement before it is trusted.
4. **Pure Python where possible.** App Control can block unsigned native libraries that pip wheels
   ship. Day one depends on the standard library, `requests`, `pysnc` and `python-dotenv`. A
   dependency with native code needs a measured import test on the laptop first.

## Quick start

```
py -3.12 -m venv venv
venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env        # fill in OLLAMA_URL, MODEL and the SN_* pair
venv\Scripts\python -m rio    # a terminal chat loop that reports tokens per second
venv\Scripts\python -m pytest tests/ -v
```

The first run prints the model's capabilities and the decode speed of the first reply. That number
decides whether `qwen3:14b` or `qwen3:8b` is the working model on this machine.

## Status

Bootstrap. The client and the chat loop are real; the ServiceNow tools arrive with the issues in
the tracker, in this order: the tools and graphs with their tests, the approval card and operator
commands, the `now-data` / `now-config` seam, then the Fluent SDK pipeline from servicenow-coder.

## Conventions

[`CLAUDE.md`](CLAUDE.md) carries the fleet-canonical engineering block and the rio-specific traps.
Decisions live in [`docs/architecture/`](docs/architecture/).
