# rio/reference.py — the ServiceNow documentation and the Fluent SDK samples, on disk, for the
# reference tools (#27, ADR-0004). `python -m rio.reference [--status] [--index-only]` checks the
# two submodules out and builds the documentation index.
#
#   servicenow-docs/           ServiceNow/ServiceNowDocs, one markdown file per page (52,776 on brazil)
#   servicenow-sdk-examples/   ServiceNow/sdk-examples, one folder per sample
#
# Reading 613 MB of markdown per question is not an option on the laptop, so a page is found by its
# front matter (title, description, product) and its path, which this indexes once per commit into
# state/. The page itself is read only when the model asks for it, a window at a time. The samples
# are 358 small files and are searched in full.
#
# Standard library only (ADR-0002): no vector store, whose wheels are compiled.

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from rio import config

SUBMODULES = ("servicenow-docs", "servicenow-sdk-examples")
INDEX_NAME = "docs_index.jsonl"
INDEX_VERSION = 1

# The front matter is the first thing in a page and is short; the longest measured was under 2 KB.
_HEAD_BYTES = 4096
_FRONT_KEYS = ("title", "description", "product", "canonical_url")

# What a tool may put in the context window is capped at 6000 characters by execute_tool; a read
# stays under it with room for its header and footer, so the footer (how to continue) is never cut.
READ_CHARS = 4500
LINE_CHARS = 600        # a table row or a minified line arrives as one line
MAX_RESULTS = 8
HOW_TO_GET = "run `python -m rio.reference` (see the README: the reference tools)"

_WORD = re.compile(r"[a-z0-9_]+")
_STOP = frozenset("a an and are as by can do does for from how i in is it of on or the to use "
                  "using what when with".split())

# Sample files worth searching, and the folders that are never the sample itself.
_EXAMPLE_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx", ".json", ".md", ".html", ".css", ".vue", ".svelte"})
_EXAMPLE_SKIP_DIRS = frozenset({".git", "node_modules", "dist", "target"})
_EXAMPLE_SKIP_FILES = frozenset({"package-lock.json", "pnpm-lock.yaml"})
_EXAMPLE_MAX_BYTES = 200_000


class Refusal(Exception):
    """A refusal the model or the operator can act on (the tools turn it into a ToolError)."""


def _long(path: Path) -> str:
    """A path the Windows file APIs accept past 260 characters without the LongPathsEnabled policy.

    The longest page path on brazil is 197 characters below the docs folder, so a checkout under a
    user profile crosses the limit, and without this the walk skips those pages silently."""
    text = str(path.resolve())
    if sys.platform == "win32" and not text.startswith("\\\\"):
        return "\\\\?\\" + text
    return text


def docs_pages_root() -> Path:
    return config.DOCS_ROOT / "markdown"


def index_path() -> Path:
    return config.STATE_DIR / INDEX_NAME


# ── Which commit is on disk ──────────────────────────────────────────────────

def commit_of(root: Path) -> str:
    """The commit checked out in a Git work tree, read from its files (so no Git is needed), or "".

    A submodule's .git is a file pointing at the real directory. HEAD is a sha when detached (what
    `git submodule update` leaves) or a ref (what `git submodule add` leaves), loose or packed."""
    dot = root / ".git"
    try:
        gitdir = dot
        if dot.is_file():
            gitdir = (root / dot.read_text(encoding="utf-8").split(":", 1)[1].strip()).resolve()
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head
        ref = head.split(None, 1)[1]
        loose = gitdir / ref
        if loose.is_file():
            return loose.read_text(encoding="utf-8").strip()
        for line in (gitdir / "packed-refs").read_text(encoding="utf-8").splitlines():
            sha, _, name = line.partition(" ")
            if name == ref:
                return sha
    except (OSError, IndexError):
        pass
    return ""


def docs_key() -> str:
    """What the index is an index OF: the docs commit, or for a copied folder (no .git) the README's
    size and time, which a fresh copy changes."""
    commit = commit_of(config.DOCS_ROOT)
    if commit:
        return commit
    try:
        st = (config.DOCS_ROOT / "README.md").stat()
    except OSError:
        return ""
    return f"copy:{st.st_size}:{st.st_mtime_ns}"


# ── The documentation index ──────────────────────────────────────────────────

def front_matter(head: str) -> dict[str, str]:
    """The `key: value` lines between the leading `---` fences. Not YAML: the pages use flat scalars,
    and a parser would be a dependency."""
    if not head.startswith("---"):
        return {}
    out: dict[str, str] = {}
    for line in head.splitlines()[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep and key in _FRONT_KEYS:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            out[key] = value
    return out


def build_index(progress=None) -> dict:
    """Walk the pages, read each one's front matter, and write the index. Returns its header.

    The header is the file's first line, so startup reads one line and not 13 MB. Measured on
    52,774 pages: 439 s straight after the checkout, when every file is read for the first time,
    and 5 s once they have been read; `progress` gets a line every 5000 pages so the first run
    doesn't look hung."""
    pages = docs_pages_root()
    if not pages.is_dir():
        raise Refusal(f"{config.DOCS_ROOT} has no markdown folder; {HOW_TO_GET}")
    key = docs_key()
    started = time.monotonic()
    base = _long(pages)
    rows: list[list[str]] = []
    unread = 0
    for folder, _dirs, files in os.walk(base):
        if progress and len(rows) // 5000 != (len(rows) + len(files)) // 5000:
            progress(f"  {len(rows) + len(files)} pages...")
        for name in files:
            if not name.endswith(".md"):
                continue
            full = os.path.join(folder, name)
            try:
                with open(full, "rb") as f:
                    head = f.read(_HEAD_BYTES).decode("utf-8", errors="replace")
            except OSError:
                unread += 1
                continue
            meta = front_matter(head)
            rel = os.path.relpath(full, base).replace(os.sep, "/")
            title = meta.get("title") or _first_heading(head) or name[:-3]
            rows.append([rel, title, meta.get("description", ""), meta.get("product", "")])
    rows.sort()
    header = {"version": INDEX_VERSION, "key": key, "pages": len(rows), "unread": unread,
              "seconds": round(time.monotonic() - started, 1)}
    out = index_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(header) + "\n")
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, out)        # a build that dies part-way leaves the old index, not half of a new one
    _loaded.clear()
    return header


def _first_heading(head: str) -> str:
    for line in head.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def index_header() -> dict:
    """The index's first line, or {} when there is no usable index."""
    try:
        with open(index_path(), encoding="utf-8") as f:
            header = json.loads(f.readline())
    except (OSError, ValueError):
        return {}
    return header if isinstance(header, dict) and header.get("version") == INDEX_VERSION else {}


def docs_problem() -> str:
    """Why the documentation can't be searched, or "" when it can. Startup and the CLI both ask this."""
    if not docs_pages_root().is_dir():
        return f"{config.DOCS_ROOT.name} is not checked out; {HOW_TO_GET}"
    header = index_header()
    if not header:
        return f"the documentation is not indexed yet; {HOW_TO_GET}"
    if header.get("key") != docs_key():
        return f"the documentation changed since it was indexed; {HOW_TO_GET}"
    return ""


def examples_problem() -> str:
    root = config.EXAMPLES_ROOT
    try:
        has_sample = any(p.is_dir() and p.name not in _EXAMPLE_SKIP_DIRS for p in root.iterdir())
    except OSError:
        has_sample = False
    return "" if has_sample else f"{root.name} is not checked out; {HOW_TO_GET}"


# Loaded on the first search and kept: each row beside its lower-cased search text.
_loaded: dict[str, list] = {}


def _index_rows() -> list[tuple[list[str], str, frozenset[str], str, str, str]]:
    path = str(index_path())
    if path not in _loaded:
        rows = []
        try:
            with open(path, encoding="utf-8") as f:
                f.readline()
                for line in f:
                    row = json.loads(line)
                    rel, title, description, product = row
                    stem = rel.rsplit("/", 1)[-1][:-3]
                    title_l = title.lower()
                    rows.append((row, title_l, frozenset(_WORD.findall(title_l)), stem.lower(), rel.lower(),
                                 f"{description} {product}".lower()))
        except (OSError, ValueError) as e:
            raise Refusal(f"the documentation index can't be read ({e}); {HOW_TO_GET}") from e
        _loaded.clear()
        _loaded[path] = rows
    return _loaded[path]


def terms_of(query: str) -> list[str]:
    words = list(dict.fromkeys(_WORD.findall(query.lower())))
    return [w for w in words if w not in _STOP] or words


def search_docs(query: str, limit: int = MAX_RESULTS) -> dict:
    """The pages whose title, file name, path or description hold the query's words, best first.

    A word matches inside a longer one, so `glide record` finds GlideRecord, but a whole word in the
    title counts for more, so `ACL` puts "ACL types" above "Oracle". Each word a page holds adds to
    its score, and where it holds it matters more than how many it holds: for `GlideRecord
    addEncodedQuery`, "GlideRecord - Global" (the name, whole, in the title) ranks above
    "MobileGlideRecord - Scoped" (the name inside another, and the method in its description)."""
    terms = terms_of(query)
    if not terms:
        raise Refusal("query is empty: give one to four keywords, e.g. GlideRecord addQuery.")
    phrase = " ".join(terms)
    scored = []
    for row, title, words, stem, rel, rest in _index_rows():
        matched = score = 0
        for t in terms:
            in_title = (0 if t not in title else 8 if t in words
                        else 6 if any(w.startswith(t) for w in words) else 3)
            s = in_title + (3 if t in stem else 2 if t in rel else 0) + (1 if t in rest else 0)
            if s:
                matched += 1
                score += s + 3
        if not matched:
            continue
        if phrase in title:
            score += 10
        scored.append((-score, len(title), -matched, row))
    scored.sort(key=lambda s: s[:2] + (s[3][0],))
    limit = max(1, min(int(limit), MAX_RESULTS))
    # The same page is published under more than one product folder (business rules sit under both
    # api-reference and build-workflows); one of each is enough of the model's eight results.
    results, seen = [], set()
    most = 0
    for _s, _l, matched, row in scored:
        same = (row[1], row[0].rsplit("/", 1)[-1])
        if same in seen:
            continue
        seen.add(same)
        most = max(most, -matched)
        results.append({"path": f"docs/{row[0]}", "title": row[1], "about": _clip(row[2], 200)})
        if len(results) == limit:
            break
    out: dict = {"count": len(results), "results": results}
    if not results:
        out["note"] = "no page matches; try fewer or different keywords (titles and descriptions are searched, not the page text)"
    else:
        if most < len(terms):
            out["note"] = "no page is indexed under every keyword; these are the closest"
        out["next"] = "read one with reference_read(path), or reference_read(path, find=\"a word\") for one part of a long page"
    return out


def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 1] + "…"


# ── The samples ──────────────────────────────────────────────────────────────

_examples: dict[str, list[tuple[str, str]]] = {}


def _example_files() -> list[tuple[str, str]]:
    """(relative path, text) for every sample file, read once per run."""
    root = config.EXAMPLES_ROOT
    key = str(root)
    if key not in _examples:
        files = []
        base = _long(root)
        for folder, dirs, names in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in _EXAMPLE_SKIP_DIRS)
            for name in sorted(names):
                if name in _EXAMPLE_SKIP_FILES or os.path.splitext(name)[1].lower() not in _EXAMPLE_SUFFIXES:
                    continue
                full = os.path.join(folder, name)
                try:
                    if os.path.getsize(full) > _EXAMPLE_MAX_BYTES:
                        continue
                    with open(full, encoding="utf-8", errors="replace") as f:
                        text = f.read()
                except OSError:
                    continue
                files.append((os.path.relpath(full, base).replace(os.sep, "/"), text))
        _examples.clear()
        _examples[key] = files
    return _examples[key]


def sample_names() -> list[str]:
    try:
        return sorted(p.name for p in config.EXAMPLES_ROOT.iterdir()
                      if p.is_dir() and p.name not in _EXAMPLE_SKIP_DIRS and not p.name.startswith("."))
    except OSError:
        return []


def search_examples(query: str, limit: int = MAX_RESULTS) -> dict:
    """Sample files that hold every word of the query, in their path or text, with the lines that
    matched. An empty query lists the samples."""
    terms = terms_of(query)
    if not terms:
        return {"samples": sample_names(),
                "next": "search with keywords, e.g. examples_search(\"BusinessRule\"), or read a file with reference_read"}
    scored = []
    for rel, text in _example_files():
        rel_l, text_l = rel.lower(), text.lower()
        if not all(t in rel_l or t in text_l for t in terms):
            continue
        hits = [(n, line) for n, line in enumerate(text.splitlines(), 1)
                if any(t in line.lower() for t in terms)]
        in_path = sum(t in rel_l for t in terms)
        # A sample's own .now.ts source says more than its package.json or README mentioning the word.
        source = 1 if rel_l.endswith((".ts", ".tsx", ".js", ".jsx")) and "/generated/" not in rel_l else 0
        scored.append((-in_path, -source, -len(hits), rel, hits))
    scored.sort(key=lambda s: s[:4])
    limit = max(1, min(int(limit), MAX_RESULTS))
    results = [{"path": f"examples/{rel}", "lines": [f"{n}: {_clip(line, 160)}" for n, line in hits[:3]]}
               for _p, _s, _h, rel, hits in scored[:limit]]
    out: dict = {"count": len(results), "results": results}
    if results:
        out["next"] = "read one with reference_read(path)"
    else:
        out["note"] = "no sample file holds every keyword; try fewer, or examples_search(\"\") to list the samples"
    return out


# ── Reading one file ─────────────────────────────────────────────────────────

def _resolve(path: str) -> tuple[Path, bool]:
    """(the file, whether it is a documentation page) for a `docs/...` or `examples/...` path.

    The path comes from the model. It is resolved and must still sit under the root it names, so
    `docs/../../.env` reads nothing."""
    cleaned = str(path).strip().replace("\\", "/").lstrip("/")
    kind, _, rest = cleaned.partition("/")
    roots = {"docs": docs_pages_root(), "examples": config.EXAMPLES_ROOT}
    if kind not in roots or not rest:
        raise Refusal(f"path must start with docs/ or examples/, as the search results give it; got {path!r}.")
    root = roots[kind].resolve()
    target = (root / rest).resolve()
    if root != target and root not in target.parents:
        raise Refusal(f"{path!r} is outside the {kind} folder.")
    if ".git" in target.relative_to(root).parts:
        raise Refusal(f"{path!r} is not a reference file.")
    if not os.path.isfile(_long(target)):
        raise Refusal(f"no such file: {path!r}. Use a path exactly as docs_search or examples_search returned it.")
    return target, kind == "docs"


_LINK = re.compile(r"\[([^\]\n]+)\]\(https?://[^)\s]+\)")
_ESCAPED = re.compile(r"\\([\\`*_{}\[\]()#+\-.!<>|~])")


def _plain(lines: list[str]) -> list[str]:
    """A page's prose without what only a renderer needs: a link keeps its text and loses its URL
    (each is ~120 characters of raw.githubusercontent.com), and an escaped underscore in a property
    name loses the backslash a model would otherwise copy. Code fences are left alone."""
    out, fenced = [], False
    for line in lines:
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced:
            line = _ESCAPED.sub(r"\1", _LINK.sub(r"\1", line))
        out.append(line)
    return out


def _lines(path: str) -> tuple[list[str], str]:
    """(the file's lines, a one-line header). A page comes without its front matter; its title and
    web address make the header."""
    target, is_doc = _resolve(path)
    with open(_long(target), encoding="utf-8", errors="replace") as f:
        text = f.read()
    if not is_doc:
        return text.splitlines(), ""
    header = ""
    if text.startswith("---"):
        meta = front_matter(text[:_HEAD_BYTES])
        end = text.find("\n---", 3)
        if end != -1:
            text = text[text.find("\n", end + 1) + 1:]
        header = _ESCAPED.sub(r"\1", " — ".join(v for v in (meta.get("title"), meta.get("canonical_url")) if v))
    return _plain(text.splitlines()), header


def _window(path: str, lines: list[str], start: int, budget: int) -> str:
    """Lines from `start` (1-based) up to `budget` characters, and a footer that says which lines
    they were and how to get the next ones."""
    out, used, last = [], 0, start - 1
    for i in range(start - 1, len(lines)):
        line = lines[i] if len(lines[i]) <= LINE_CHARS else lines[i][:LINE_CHARS] + "…"
        if out and used + len(line) + 1 > budget:
            break
        out.append(line)
        used += len(line) + 1
        last = i + 1
    footer = (f"[lines {start}-{last} of {len(lines)}; end of file]" if last >= len(lines) else
              f"[lines {start}-{last} of {len(lines)}; continue with reference_read(\"{path}\", start_line={last + 1})]")
    return "\n".join(out + ["", footer])


def read(path: str, find: str = "", start_line: int = 1) -> str:
    """A window of one file: from `start_line`, or from the first line holding `find`."""
    lines, header = _lines(path)
    try:
        start = max(1, int(start_line))
    except (TypeError, ValueError):
        start = 1
    find = str(find or "").strip()
    if find:
        needle = find.lower()
        at = next((i for i in range(start - 1, len(lines)) if needle in lines[i].lower()), None)
        if at is None:
            headings = [ln.lstrip("# ").strip() for ln in lines if ln.startswith("#")][:25]
            where = f" from line {start}" if start > 1 else ""
            raise Refusal(f"{find!r} is not in {path}{where}."
                          + (f" Its headings: {'; '.join(headings)}" if headings else ""))
        start = max(1, at)              # `at` is 0-based, so this is one line of lead-in
    if start > len(lines):
        raise Refusal(f"{path} has {len(lines)} lines; start_line {start} is past the end.")
    return "\n".join(([header, ""] if header else []) + [_window(path, lines, start, READ_CHARS)])


# ── Search and read in one call ──────────────────────────────────────────────
#
# A turn has one round of tool calls (ADR-0003), so a search that returned only paths could never be
# followed by the read in the same turn. Measured on qwen3:14b with a paths-only search: asked for a
# Fluent business rule from the samples, it called examples_search and then wrote one from memory,
# with `tableName` and `execute`, neither of which exists. So each search also carries the text of
# its best match, and reference_read is for the next part or another result, on a later turn.

LOOKUP_CHARS = 3600     # the best match's text
LOOKUP_TOTAL = 5800     # with the list of other matches: under execute_tool's 6000, so nothing is cut
_CANDIDATES = 3


def look_up_docs(query: str) -> str:
    """The best page for the query, opened where the query's words are, and the other matches."""
    found = search_docs(query)
    results = found["results"]
    if not results:
        return found["note"]
    terms = terms_of(query)
    # Words the index didn't match for a result must be in its text for it to be the answer:
    # `GlideRecord addEncodedQuery` matches three GlideRecord pages by title, and one of them is the
    # client API, which has no such method. Among the first few, take the page that holds the most.
    best = None
    for rank, hit in enumerate(results[:_CANDIDATES]):
        searched = f"{hit['title']} {hit['path']}".lower()
        unmatched = [t for t in terms if t not in searched]
        try:
            lines, header = _lines(hit["path"])
        except (Refusal, OSError):
            continue
        lowered = [ln.lower() for ln in lines]
        held = [t for t in unmatched if any(t in ln for ln in lowered)]
        if best is None or len(held) > len(best[4]):
            best = (hit, lines, header, lowered, held, rank)
        if len(held) == len(unmatched):
            break
    if best is None:
        raise Refusal(f"the pages the index lists for {query!r} can't be read; {HOW_TO_GET}")
    hit, lines, header, lowered, held, _rank = best
    start = 1
    if held:
        # A heading that holds the word is the section about it; failing that, its first mention.
        at = next((i for i, ln in enumerate(lowered) if ln.startswith("#") and any(t in ln for t in held)), None)
        if at is None:
            at = next(i for i, ln in enumerate(lowered) if any(t in ln for t in held))
        start = at + 1
    while start < len(lines) and not lines[start - 1].strip():
        start += 1                      # a page opens with blank lines after its front matter
    parts = [f"Best match: {hit['path']}", header or hit["title"], "", _window(hit["path"], lines, start, LOOKUP_CHARS)]
    if found.get("note"):
        parts += ["", f"Note: {found['note']}."]
    return _with_others(parts, "Other pages", [f"- {r['path']} — {r['title']}" for r in results if r is not hit])


def _with_others(parts: list[str], label: str, others: list[str]) -> str:
    """The lookup with as many of the other matches as fit under LOOKUP_TOTAL, each one whole:
    execute_tool would otherwise cut the list mid-path, and a cut path reads as a real one."""
    text = "\n".join(parts)
    heading = f"\n\n{label} (read one with reference_read):"
    room = LOOKUP_TOTAL - len(text) - len(heading)
    kept = []
    for line in others:
        room -= len(line) + 1
        if room < 0:
            break
        kept.append(line)
    return text + (heading + "\n" + "\n".join(kept) if kept else "")


def look_up_examples(query: str) -> str:
    """The best sample file for the query, in full when it fits, and the other files that match."""
    found = search_examples(query)
    if "samples" in found:
        return ("The samples: " + ", ".join(found["samples"])
                + ". Search with a keyword from the code, e.g. BusinessRule, to see one.")
    results = found["results"]
    if not results:
        return found["note"]
    hit = results[0]
    lines, _header = _lines(hit["path"])
    parts = [f"Best match: {hit['path']}", "", _window(hit["path"], lines, 1, LOOKUP_CHARS)]
    return _with_others(parts, "Other files", [f"- {r['path']}" for r in results[1:]])


# ── python -m rio.reference ──────────────────────────────────────────────────

def _git_checkout(names: list[str]) -> str:
    """Check the named submodules out at the commits this repo pins. Returns "" or why it didn't.

    --depth 1 because the documentation's history is not what rio reads; core.longpaths because the
    docs README warns its paths pass 260 characters on Windows."""
    if not (config.ROOT / ".git").exists():
        return (f"{config.ROOT} is not a Git checkout (a zip download has no submodule content). Clone rio with "
                "Git, or put a copy of each repository in a folder and set RIO_DOCS_ROOT / RIO_EXAMPLES_ROOT in .env")
    cmd = ["git", "-c", "core.longpaths=true", "submodule", "update", "--init", "--depth", "1", "--", *names]
    try:
        done = subprocess.run(cmd, cwd=config.ROOT, capture_output=True, text=True)
    except FileNotFoundError:
        return ("Git is not installed or not on PATH. Install it, or put a copy of each repository in a folder "
                "and set RIO_DOCS_ROOT / RIO_EXAMPLES_ROOT in .env")
    if done.returncode != 0:
        return f"`{' '.join(cmd)}` failed ({done.returncode}): {(done.stderr or done.stdout).strip()[-600:]}"
    return ""


def _status_lines() -> list[str]:
    header = index_header()
    docs, examples = docs_problem(), examples_problem()
    commit = commit_of(config.DOCS_ROOT)
    lines = [f"documentation: {config.DOCS_ROOT}",
             f"  commit:  {commit[:12] or '(no Git metadata)'}",
             f"  index:   {str(header.get('pages')) + ' pages' if header else 'none'} ({index_path()})",
             f"  usable:  {'yes' if not docs else 'no: ' + docs}",
             f"samples: {config.EXAMPLES_ROOT}",
             f"  commit:  {commit_of(config.EXAMPLES_ROOT)[:12] or '(no Git metadata)'}",
             f"  usable:  {'yes, ' + str(len(sample_names())) + ' samples' if not examples else 'no: ' + examples}"]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m rio.reference",
        description="Check out the ServiceNow docs and SDK samples at the pinned commits and index the docs.")
    parser.add_argument("--status", action="store_true", help="show what is on disk and change nothing")
    parser.add_argument("--index-only", action="store_true", help="skip Git; index the folder as it is")
    args = parser.parse_args(argv)
    if args.status:
        print("\n".join(_status_lines()))
        return 0 if not docs_problem() and not examples_problem() else 1

    if not args.index_only:
        # A folder set in .env is the operator's own copy: Git doesn't manage it.
        ours = [name for name, root in zip(SUBMODULES, (config.DOCS_ROOT, config.EXAMPLES_ROOT))
                if root.resolve() == (config.ROOT / name).resolve()]
        if ours:
            print(f"Checking out {', '.join(ours)} (the documentation is about 600 MB; the first time takes minutes)...")
            problem = _git_checkout(ours)
            if problem:
                print(f"Can't check out: {problem}", file=sys.stderr)
                return 1
    try:
        print(f"Indexing {docs_pages_root()} ...")
        header = build_index(progress=print)
    except Refusal as e:
        print(f"Can't index: {e}", file=sys.stderr)
        return 1
    print(f"Indexed {header['pages']} pages in {header['seconds']} s"
          + (f"; {header['unread']} could not be read" if header["unread"] else "") + ".")
    print("\n".join(_status_lines()))
    problems = [p for p in (docs_problem(), examples_problem()) if p]
    if problems:
        return 1
    print("\nRestart rio: docs_search, examples_search and reference_read are on its menu.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
