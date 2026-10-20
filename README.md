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

**Check the prerequisites first.** Rio needs Python 3.12 (64-bit) and Ollama with a model pulled.
It doesn't need Git to install or run: pip fetches the now-mcp packages as zip archives from
GitHub (Git only matters for cloning or contributing). In PowerShell:

```powershell
py -3.12 --version   # Python 3.12.x. "py is not recognized": Python, or its py launcher, isn't installed
ollama list          # lists qwen3:14b (or 8b). If not: ollama pull qwen3:14b
```

If Python 3.12 is installed without the `py` launcher, use its full path in the first step below:
`& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m venv venv` for a per-user install,
or `& "C:\Program Files\Python312\python.exe" -m venv venv` for an all-users one.

Behind a proxy with an allowlist, the install needs `pypi.org`, `files.pythonhosted.org`,
`github.com` and `codeload.github.com` (GitHub redirects the archive download there).
`ollama pull` needs `registry.ollama.ai` and `dd20bb891979d25aebc8bec07b2b3bbc.r2.cloudflarestorage.com`:
the registry serves the manifest and small files itself, and redirects the multi-GB model layer
to that Cloudflare storage host (measured 2026-10-14 on qwen3:14b; a `HEAD` request doesn't show
the redirect, a `GET` does).

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

**The snapshot tools.** `now_field_exists` and `now_choice_values` answer from an instance-config
snapshot of *your* instance, because choice values differ between instances. Make one once, with
the instance and identity in `.env`:

```powershell
.\venv\Scripts\python -m rio.snapshot   # writes .\snapshot\ and prints the INSTANCE_CONFIG_ROOT line for .env
```

It reads the instance's dictionary page by page, which takes about 7–8 minutes on a developer
instance and longer on a larger one. It says how many rows its user couldn't read (`read N of M`);
that's normal, and the snapshot can't answer for those rows. Re-run it after the instance changes.
Rio refuses a snapshot taken from a different instance than `SN_INSTANCE`, and one it can't
place (no `instance.json`, which an interrupted re-export leaves behind). It needs only the schema
files; the others (properties, roles, catalog) are skipped if your user can't read them.

## Status

- **Ollama client and chat loop:** capability-aware, local only, with the decode rate on every reply.
- **Tools:** servicenow-coder's verbs via [now-mcp](https://github.com/orwellsanimal/now-mcp), imported as a library:
  - from the live instance: `now_query`, `now_get`, `now_aggregate`;
  - from the instance-config snapshot: `now_field_exists`, `now_choice_values`;
  - one write: `now_update`.
- **Status card:** type a record number (`INC0010023`, `status of CHG0000023`) and rio shows that
  record's card itself, with no model turn: type, state, priority, short description, assignee
  and group, the latest work note or comment, and the link. It works for any task record (INC,
  CHG, CS, PRB, RITM, REQ, tasks). The model can ask for one too, with `sn_record_status`.
- **Writes:** a write waits on an approval card that shows its real dry-run diff, and only `y` runs it ([ADR-0003](docs/architecture/0003-a-write-runs-only-from-an-approved-card.md)). With `RIO_READ_ONLY=true` there are no writes at all.
- **Next:** the triage / enrich / resolve graphs (#1), then the Fluent SDK pipeline.

## Conventions

[`CLAUDE.md`](CLAUDE.md) carries the fleet-canonical engineering block and the rio-specific traps.
Decisions live in [`docs/architecture/`](docs/architecture/).

## License

[Apache-2.0](LICENSE). Code carried from whitcortex keeps its whitcortex issue numbers in comments for provenance.
