# servicenow-rio — agent conventions and gotchas

Python harness for Ollama as a ServiceNow engineering companion on a managed Windows laptop.
Local inference only. See [`README.md`](README.md) for what is carried from whitcortex and
servicenow-coder and what is left behind; decisions are in [`docs/architecture/`](docs/architecture/).

---

## Engineering discipline (how this repo's agent works an issue)

*Instructions, not suggestions. Follow on every substantive change.*

**Before code**
- State the acceptance criterion in one sentence. Can't? The issue isn't ready — ground it
  in the code first.
- **Reproduce first.** Confirm the behavior in the actual code before building.
  Auto-generated / enriched / secondhand evidence (issue bodies, investigator notes) is a
  **lead, not ground truth** — verify every claim against source. Put repro evidence
  (file:line, output) **on the issue**.

**While building**
- Branch off `main`: `feat/<issue>-slug` / `fix/<issue>-slug`. Never commit to `main`.
- **A regression test lands with the fix** — not done without the test that keeps it fixed.
  `venv\Scripts\python -m pytest tests/ -v` must be green before the PR.
- Write a **MADR-lite ADR** (`docs/architecture/NNNN-*.md`) when a reasonable engineer could
  defensibly choose differently, a rejected option would plausibly be re-proposed, or the
  decision constrains future code. Skip conventions / obvious calls.

**Review & land**
- PR with `Closes #<issue>` in the body (`fix(#N):` in the title does **not** auto-close).
- `@claude` review with a **targeted ask** — name 2–4 risk areas, not "please review." Post
  executed-test evidence (command + counts); the reviewer can't run tests.
- Findings are **leads to verify, not verdicts** — right-in-abstract can be wrong-for-this-repo;
  measure before applying, and state declines with reasons on the PR.
- **The human merges.** Agents never merge their own PRs.

**After landing**
- Verify in the **running product**, not just the suite, when there's a runnable surface —
  note the usability verdict, not just pass/fail.

> The block above is the fleet-canonical standard, adopted **verbatim** from whitcortex's
> `docs/satellite_sdlc_baseline.md` §4a (only `{TEST_COMMAND}` filled in). Do not edit it here;
> propose changes against the baseline as a `needs-standard-change` finding. Everything below is
> **rio-specific addendum**.

---

## Addendum — this repo's traps

- **The machine is a CPU.** Radeon 760M integrated graphics is not accelerated by Ollama on
  Windows. Measure before trusting any latency assumption from the lab (its 14b decodes at ~44
  tok/s on a discrete GPU). `rio` prints the decode rate on every reply; put the number on the
  issue when it matters. `num_ctx` defaults to 8192. **Never send `num_thread`**: on the lab box
  the key alone cost 25–50x (whitcortex #1992), and here the default thread count is right.
- **App Control for Business is enforced.** Unsigned native libraries inside pip wheels can be
  blocked. Day-one dependencies are pure Python (`requests`, `pysnc`, `python-dotenv`). Before
  adding anything with a compiled extension (pydantic-core, numpy, chroma), run the import on the
  laptop and record the result on the issue.
- **Capabilities come from the model, not from its name.** `rio.llm.capabilities()` reads
  `/api/show`; `think` is sent only to a model that lists `thinking`, `tools` only to one that
  lists `tools`. Ollama returns 400 for `think` on a model that cannot think. Do not special-case
  model names.
- **Local only, by construction.** There is no cloud client in this repo. A feature that needs
  web search or a cloud model gets an ADR first.
- **Credentials.** `SN_INSTANCE` plus the pair `SN_IDENTITY` selects (`human` →
  `SN_HUMAN_USER`/`SN_HUMAN_PASSWORD`, `machine`, `agent`), the same model as whitcortex;
  servicenow-coder's `SN_USER`/`SN_PASSWORD` are read as the `legacy` pair so one `.env` can
  serve both. No coded defaults: unset means not configured.
- **The suite must not open a socket to Ollama or the instance.** Tests stub `requests.post` at
  the client's edge or the GlideRecord client; a test that wants the model is marked `live`.
  The ServiceNow workflows (`rio/servicenow.py` and what builds on it) are tested against
  `tests/fake_instance.py`, faked at the now-data verbs. It evaluates the encoded queries it is
  sent and fails on one it can't read, so a test can't pass on a query the instance would widen.
- **Text into an encoded query only through `rio.servicenow.query_safe()`.** An encoded query has no
  quoting: a caret ends the term (`^OR` widens it), a comma splits an IN list, and a `javascript:`
  value is run on the instance. Ticket text, CI names and tag names are all typed by someone else
  (PR #25 review: a short description could have put a script into `nameIN`). A record number goes
  through `record_number()`; anything else that isn't a sys_id goes through `query_safe()`, or is
  doubled (`^^`) when it must be looked up exactly. `tests/conftest.py` fails any test that opens a
  connection, so a default argument bound to the real client shows up at once.
- **Carried code keeps its receipts.** A module copied from whitcortex keeps the issue numbers in
  its comments (`#2597`, `#2627`, `#2630`, `#2643`); they point at the measured evidence that
  shaped it. Trim the code, not the history.
