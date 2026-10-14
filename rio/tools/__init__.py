# rio/tools/__init__.py — the registry, the menu the model sees, and the one place a tool runs.
#
# Carried from whitcortex tools/__init__.py and trimmed (#2 here):
#   * `register` refuses a duplicate name (whitcortex #1963: import order silently chose the winner);
#   * `execute_tool` is the only dispatcher (whitcortex ADR-0124), and a tool's failure becomes text
#     for the model there and nowhere else, a deliberate refusal (ToolError) worded differently from
#     a crash (whitcortex ADR-0065);
#   * a write never runs inside a model turn: it needs `approved=True`, which only the approval card
#     passes (whitcortex ADR-0129/0130; ADR-0003 here).
# Left behind: the feature-flag ceiling, QA isolation and the repo_name check, which guard tools rio
# does not have.

from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from rio import config
from rio.tools.errors import ToolError


@dataclass(frozen=True)
class Tool:
    name: str
    fn: Callable[..., Any]
    group: str
    writes: bool
    # For a write: shows what the call WOULD do, without doing it, for the approval card. It raises
    # ToolError when the call cannot work (a missing record), so nothing is parked for it.
    preview: Callable[..., str] | None = None


TOOLS: dict[str, Tool] = {}

# Output a tool may put into the context window. num_ctx is 8192 tokens on the laptop (ADR-0002); a
# 50-row query serialised in full would push the conversation out of it.
MAX_RESULT_CHARS = 6000


def register(name: str, *, group: str, writes: bool, preview: Callable[..., str] | None = None):
    """Add a tool. `writes` has no default on purpose: every tool says whether it changes anything,
    so a new write cannot arrive ungated by omission (ADR-0003)."""
    def decorator(fn):
        prior = TOOLS.get(name)
        if prior is not None and (prior.fn.__module__, prior.fn.__qualname__) != (fn.__module__, fn.__qualname__):
            raise ValueError(f"duplicate tool registration: {name!r} is already "
                             f"{prior.fn.__module__}.{prior.fn.__qualname__}; rename one (whitcortex #1963).")
        if writes and preview is None:
            raise ValueError(f"write tool {name!r} needs a preview for its approval card.")
        TOOLS[name] = Tool(name, fn, group, writes, preview)
        return fn
    return decorator


def is_write(name: str) -> bool:
    tool = TOOLS.get(name)
    return bool(tool and tool.writes)


# ── The menu ─────────────────────────────────────────────────────────────────

_JSON_TYPES = {"str": "string", "int": "integer", "float": "number", "bool": "boolean",
               "dict": "object", "list": "array"}
_ARG_LINE = re.compile(r"^\s{2,}(\w+):\s*(.+)$")


def _arg_docs(doc: str) -> dict[str, str]:
    """`name: description` lines under an `Args:` heading. Small models misfill bare argument names
    (whitcortex #2519 measured 74% SN tool failures, many from invented tables), so each one says
    what it wants."""
    out: dict[str, str] = {}
    in_args = False
    for line in doc.splitlines():
        if line.strip() == "Args:":
            in_args = True
            continue
        if in_args:
            m = _ARG_LINE.match(line)
            if m:
                out[m.group(1)] = m.group(2).strip()
            elif line.strip() and not line.startswith("    "):
                break
    return out


def schemas() -> list[dict]:
    """The tools in Ollama's function-calling shape. A write is not on the menu when rio is read-only."""
    out = []
    for tool in TOOLS.values():
        if tool.writes and config.READ_ONLY:
            continue
        doc = inspect.getdoc(tool.fn) or ""
        arg_docs = _arg_docs(doc)
        props, required = {}, []
        for pname, param in inspect.signature(tool.fn).parameters.items():
            ann = param.annotation if param.annotation is not inspect.Parameter.empty else str
            ann_name = ann if isinstance(ann, str) else getattr(ann, "__name__", "str")
            prop = {"type": _JSON_TYPES.get(ann_name.split("[")[0], "string")}
            if pname in arg_docs:
                prop["description"] = arg_docs[pname]
            props[pname] = prop
            if param.default is inspect.Parameter.empty:
                required.append(pname)
        out.append({"type": "function", "function": {
            "name": tool.name, "description": doc.split("\n\n")[0].replace("\n", " "),
            "parameters": {"type": "object", "properties": props, "required": required}}})
    return out


# ── The one dispatcher ───────────────────────────────────────────────────────

def _as_text(result: Any) -> str:
    text = result if isinstance(result, str) else json.dumps(result, default=str, ensure_ascii=False)
    if len(text) > MAX_RESULT_CHARS:
        text = (text[:MAX_RESULT_CHARS]
                + f"\n[truncated: {len(text)} characters, showing {MAX_RESULT_CHARS}. Narrow the query or the fields.]")
    return text


def execute_tool(name: str, args: Any, *, approved: bool = False) -> str:
    """Run a tool by name and return text for the model. Never raises for a tool's own failure.

    `approved=True` is passed only by the approval card (rio.approval), for exactly the call the
    operator saw. A model turn never passes it, so a write the model names is refused here even if
    a caller forgot to check `is_write` first."""
    tool = TOOLS.get(name)
    if tool is None:
        return f"Tool error: unknown tool {name!r}. Available: {', '.join(sorted(TOOLS)) or 'none'}."
    if not isinstance(args, dict):
        return f"Tool error ({name}): arguments must be a JSON object, got {type(args).__name__}."
    if tool.writes and config.READ_ONLY:
        return f"Tool error ({name}): rio is read-only here (RIO_READ_ONLY); nothing was changed."
    if tool.writes and not approved:
        return f"Tool error ({name}): a write runs only from an approved card; nothing was changed."
    try:
        inspect.signature(tool.fn).bind(**args)
    except TypeError as e:  # the model's arguments: a missing or invented parameter name
        return f"Tool error ({name}): bad arguments: {e}"
    try:
        return _as_text(tool.fn(**args))
    except ToolError as e:
        return f"Tool error ({name}): {e}"
    except Exception as e:  # a crash, not a refusal: say so, distinctly (whitcortex ADR-0065)
        return f"Tool error ({name}): unexpected {type(e).__name__}: {e}"
