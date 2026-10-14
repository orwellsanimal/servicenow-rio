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

**Check the prerequisites first.** Rio needs Python 3.12 (64-bit), Git (pip installs the now-mcp
packages from GitHub) and Ollama with a model pulled. In PowerShell:

```powershell
py -3.12 --version   # Python 3.12.x. "py is not recognized": Python, or its py launcher, isn't installed
git --version        # "git is not recognized": install Git for Windows
ollama list          # lists qwen3:14b (or 8b). If not: ollama pull qwen3:14b
```

If Python 3.12 is installed without the `py` launcher, use its full path in the first step below
(for example `& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m venv venv`).

Then, from the repo folder in PowerShell:

```powershell
py -3.12 -m venv venv
.\venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env               # fill in OLLAMA_URL, MODEL and the SN_* pair; RIO_READ_ONLY=true on a work instance
.\venv\Scripts\python -m pytest tests/ -q
.\venv\Scripts\python -m rio         # a terminal chat loop that reports tokens per second
```

`The module 'venv' could not be loaded` on any `.\venv\...` line means the first step didn't create
the venv: PowerShell reads a missing `venv\Scripts\python` as a module name. Fix the first step.

The first run prints the model's capabilities and the decode speed of the first reply. That number
decides whether `qwen3:14b` or `qwen3:8b` is the working model on this machine.

## Status

- **Ollama client and chat loop:** capability-aware, local only, with the decode rate on every reply.
- **Tools:** servicenow-coder's verbs via [now-mcp](https://github.com/orwellsanimal/now-mcp), imported as a library:
  - from the live instance: `now_query`, `now_get`, `now_aggregate`;
  - from the instance-config snapshot: `now_field_exists`, `now_choice_values`;
  - one write: `now_update`.
- **Writes:** a write waits on an approval card that shows its real dry-run diff, and only `y` runs it ([ADR-0003](docs/architecture/0003-a-write-runs-only-from-an-approved-card.md)). With `RIO_READ_ONLY=true` there are no writes at all.
- **Next:** the triage / enrich / resolve graphs and the status card (#1), then the operator commands, then the Fluent SDK pipeline.

## Conventions

[`CLAUDE.md`](CLAUDE.md) carries the fleet-canonical engineering block and the rio-specific traps.
Decisions live in [`docs/architecture/`](docs/architecture/).

## License

[Apache-2.0](LICENSE). Code carried from whitcortex keeps its whitcortex issue numbers in comments for provenance.
