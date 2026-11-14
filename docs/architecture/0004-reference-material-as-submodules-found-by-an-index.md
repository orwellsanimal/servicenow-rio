# 0004. The ServiceNow docs and SDK samples are submodules, found by a front-matter index, and a search returns text

- **Status:** Accepted
- **Date:** 2026-11-14
- **Deciders:** Jody (operator: submodules; search tools in the first change), agent (index, tool shape, measurements)

## Context

Rio could answer about an instance (the now-* tools, the snapshot) but had nothing to answer "how
does this API work" or "what does a Fluent business rule look like" from. A 12–14b local model's
recall of ServiceNow APIs is not reliable, and rio is local only (ADR-0002).

ServiceNow publishes both as Git repositories: `ServiceNow/ServiceNowDocs` (one branch per release
family; `brazil` has 52,774 markdown pages, 613 MB checked out) and `ServiceNow/sdk-examples`
(26 samples, 358 files). At depth 1 the two fetch as 87 MB.

Three constraints shape the rest:

- **A turn has one round of tool calls** (ADR-0003). The model cannot search and then read before
  it answers.
- **`num_ctx` is 8192** and a tool result is cut at 6000 characters (ADR-0002).
- **Pure Python** (ADR-0002): App Control can block the compiled wheels a vector store ships.

## Options considered

**How rio holds the two repositories**

1. **A mirror clone into a gitignored folder**, refreshed by a command: whitcortex's
   `pipeline/sync_servicenow_docs.py` (shallow clone, then fetch and hard reset). Nothing enters
   this repo and updating needs no commit. But what a given rio commit was tested against is then
   not recorded anywhere, and two machines can hold different documentation without knowing.
2. **(chosen) Git submodules, shallow, pinned to a branch**, as servicenow-coder holds them. The pin
   is in the repo, a bump is a reviewed commit, and the two repos work the same way. The cost: a
   bump needs a commit here and a pull there, and a zip download of rio has no submodule content.

**How a page is found**

1. **Scan the text on every question.** 613 MB per question; not on a laptop.
2. **A vector store** (whitcortex ingests the same repo into Chroma). Compiled wheels, an embedding
   model beside the chat model on six cores, and a re-ingest per bump.
3. **(chosen) An index of each page's front matter** (title, description, product) and path, built
   once per docs commit into `state/`. The pages carry good titles: the API pages are named for
   the API. A search over 52,774 rows takes 25–100 ms.

**What a search returns**

1. **Paths, for the model to read next.** Measured on qwen3:14b: asked for a Fluent business rule
   from the samples, it called `examples_search`, got paths and three matching lines, and wrote a
   rule from memory with `tableName` and `execute`, neither of which exists. The read never
   happened, because the turn was over.
2. **A second tool round for reads.** It changes ADR-0003's turn for every tool, and its CPU cost
   on the laptop is the open measurement in #3.
3. **(chosen) The text of the best match, with the other matches listed.** One call, one round.

## Decision

- `servicenow-docs` (`brazil`) and `servicenow-sdk-examples` (`main`) are submodules with
  `shallow = true`. `python -m rio.reference` checks them out at the pinned commits
  (`--depth 1`, `core.longpaths=true`) and builds the index; it never moves a pin.
- `RIO_DOCS_ROOT` / `RIO_EXAMPLES_ROOT` point rio at a copied folder on a machine without Git.
- The index is keyed by the docs commit. `docs_search` is off, with the reason on the startup
  banner, when the folder is missing, not indexed, or no longer the commit that was indexed.
- `docs_search` and `examples_search` return the best match's text (3600 characters) and the other
  matches, in under 5800 characters. For the docs, the best match is the first of the top three
  whose text holds the query's words that its title and path didn't, opened at the heading that
  holds them. `reference_read` reads a further window of a file, by line or by a word.
- A path from the model is resolved and must stay under the root it names.
- A weekly workflow compares each pin with its upstream branch and keeps one tracker issue. It
  opens no pull request: Actions in this repository is not allowed to create them.

## Consequences

- **Positive:** measured on qwen3:14b after the change, the same sample question returned the
  sample's real code, and a `GlideRecord addEncodedQuery` question was answered from the method's
  own section. No new dependency. Rio starts as before when the folders are empty.
- **Negative:** a page is found only by words in its title, description, file name or path. A
  question in words the title doesn't use finds the wrong page or none: for `flow designer trigger`
  the lookup opens a MetricBase page, with "Workflow Studio flow trigger types" fourth in the list. Searching headings as
  well would help and costs a larger index; not done until a miss on the laptop asks for it.
- **Negative:** each lookup puts up to 5800 characters into an 8192-token window, more than any
  other tool. Two lookups in one round, on top of a long conversation, can push early turns out.
- **Negative:** the documentation needs Git, or a hand-copied folder. The quick start's "no Git
  needed" now holds for everything except these tools.
- **Not measured here:** the checkout and the first index on the laptop itself, under App Control
  and its antivirus. On the development machine the first index took 439 s and a repeat took 5 s.
- **Follow-up when a release family goes GA:** `branch` in `.gitmodules` is edited by hand; the
  tracker reports a deleted upstream branch as an error rather than as "up to date".
