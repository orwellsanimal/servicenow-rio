# rio/turn.py — one chat turn: at most one round of tool calls, and writes stop at a card.
#
# Native tool calling only (the model must list the `tools` capability, ADR-0002); a model without
# it gets a plain chat turn and no menu. One round per turn keeps the CPU cost of a turn bounded:
# the model asks for tools once, rio runs the reads, and a second call (without the menu) answers
# from their results. A write is never run here: the first one parks on a card (rio.approval) and
# the turn ends there, so the operator sees the call before anything happens (ADR-0003).

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from rio import config, llm
from rio.approval import Card, park
from rio.tools import execute_tool, is_write, schemas


# A reply with neither text nor a tool call. On the first call Ollama returns this when it can't
# parse a tool call the model generated; the turn is dropped (not added to history) and the operator
# told. After a read round the reads did run (a write would have parked on a card first), so the
# second message says what ran and that nothing was changed.
_EMPTY = ("(no reply) the model returned neither text nor a tool call; Ollama may have failed to "
          "parse a tool call. Nothing ran. Try rephrasing.")


# A tool call written out as text instead of made: {"name": …, "arguments"|"parameters": …}, the
# OpenAI shape {"type": "function", "function": {…}}, or a list of these; bare, or directly inside
# <tool_call> tags or a ```json fence. qwen3 does this on the second call, which has no menu by
# design (#8): the operator would see what looks like a queued write, or a call to a tool that
# doesn't exist ("now_card"). It never runs; rio cuts it out and says so. Not caught, on purpose
# until it is seen under rio's native tools: whitcortex's text protocol, {"tool": …, "args": …}.
_START = re.compile(r"[{\[]")
_OPENS = re.compile(r"(```(?:json)?|<tool_call>)\s*$")            # a wrapper ending right before a call
_CLOSES = {"```": re.compile(r"\s*```"), "<tool_call>": re.compile(r"\s*</tool_call>")}
_DECODER = json.JSONDecoder()


def _call_name(obj) -> str | None:
    if isinstance(obj, dict):
        if isinstance(obj.get("name"), str) and ("arguments" in obj or "parameters" in obj):
            return obj["name"]
        if isinstance(obj.get("function"), dict):
            return _call_name(obj["function"])
    return None


def _call_names(obj) -> list[str]:
    """The tool names when `obj` is a written-out call, or a non-empty list of nothing but calls."""
    found = [_call_name(o) for o in obj] if isinstance(obj, list) else [_call_name(obj)]
    return found if found and all(found) else []


def _written_calls(text: str) -> tuple[list[str], str]:
    """(the tool names written out as calls, the text with those calls removed).

    Only each call's own span goes, with a fence or tag that directly encloses it: other code blocks
    keep their fences. Text without a written-out call comes back unchanged."""
    names, kept, cut, pos = [], [], 0, 0
    while m := _START.search(text, pos):
        start = m.start()
        try:
            obj, end = _DECODER.raw_decode(text, start)
        except json.JSONDecodeError:
            pos = start + 1
            continue
        found = _call_names(obj)
        if found:
            names += found
            opened = _OPENS.search(text, cut, start)
            if opened:
                closed = _CLOSES["```" if opened.group(1).startswith("```") else "<tool_call>"].match(text, end)
                if closed:
                    start, end = opened.start(), closed.end()
            kept.append(text[cut:start])
            cut = end
        pos = end
    if not names:
        return [], text
    kept.append(text[cut:])
    return names, re.sub(r"\n{3,}", "\n\n", "".join(kept)).strip()


def _written_note(names: list[str], offered: set[str], tools_run: list[str], no_menu: str = "") -> str:
    unique = list(dict.fromkeys(names))
    after = f"After reading with {', '.join(tools_run)}, the" if tools_run else "The"
    note = (f"({after} model wrote out a call to {', '.join(unique)} as text instead of making it. "
            "It did not run, and nothing was changed.")
    if not offered:  # asking again won't help: there is no menu at all
        return note + f" rio offers no tools here: {no_menu or 'none are loaded'}.)"
    hidden = [n for n in unique if n not in offered and config.READ_ONLY and is_write(n)]
    unknown = [n for n in unique if n not in offered and n not in hidden]
    if hidden:
        note += f" rio is read-only here (RIO_READ_ONLY), so {', '.join(hidden)} isn't offered."
    if unknown:
        note += f" {', '.join(unknown)} {'is' if len(unknown) == 1 else 'are'} not a tool rio offers here."
    return note + ("" if hidden and not unknown else " Ask again to have a tool run.") + ")"


def _answer(turn: "Turn", history: list[dict], content: str, offered: set[str], no_menu: str = "") -> "Turn":
    """Finish a turn on a text reply, cutting out any tool call the model wrote out as text."""
    names, rest = _written_calls(content)
    if not names:
        history.append({"role": "assistant", "content": content})
        turn.text = content.strip()
        return turn
    note = _written_note(names, offered, turn.tools_run, no_menu)
    if not rest:     # nothing but made-up calls: drop the turn, like an empty reply
        return Turn(text=note, ok=False, tools_run=turn.tools_run, replies=turn.replies)
    history.append({"role": "assistant", "content": rest})   # never the made-up calls: the model would copy them
    turn.text = f"{rest}\n\n{note}"
    return turn


def _empty_after(tools: list[str]) -> str:
    # tools is empty when the round held only writes that park() refused (no card): nothing ran.
    if not tools:
        return "(no reply) the model returned no text; nothing ran or was changed. Try asking again."
    return (f"(no reply) the model returned no text after reading with {', '.join(tools)}; "
            "nothing was changed. Try asking again.")


@dataclass
class Turn:
    text: str = ""                      # what to show the operator
    ok: bool = True
    card: Card | None = None            # set when a write is waiting for "y"
    tools_run: list[str] = field(default_factory=list)
    replies: list[llm.Reply] = field(default_factory=list)


def _args(raw) -> dict | str:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return raw
        return parsed
    return raw


def run(history: list[dict], *, chat=llm.chat, model_caps: frozenset[str] | None = None) -> Turn:
    """Answer the last user message in `history`, appending what the model and the tools said."""
    caps = model_caps if model_caps is not None else llm.capabilities()
    menu = schemas() if "tools" in caps else []
    offered = {m["function"]["name"] for m in menu}
    no_menu = "" if menu else ("this model does not list the tools capability" if "tools" not in caps
                               else "none are loaded; see the startup banner")
    turn = Turn()
    first = chat(history, tools=menu or None, think=False)
    turn.replies.append(first)
    if not first.ok:
        return Turn(text=f"(no reply) {first.error}", ok=False, replies=turn.replies)
    if not first.tool_calls and not first.content.strip():
        return Turn(text=_EMPTY, ok=False, replies=turn.replies)
    if not first.tool_calls:
        return _answer(turn, history, first.content, offered, no_menu)

    history.append({"role": "assistant", "content": first.content, "tool_calls": first.tool_calls})
    for call in first.tool_calls:
        fn = call.get("function") or {}
        name, args = str(fn.get("name") or ""), _args(fn.get("arguments"))
        if is_write(name):
            if turn.card is not None:
                result = "Not run: one write per turn; ask again after the operator answers the card."
            else:
                parked = park(name, args)
                if isinstance(parked, Card):
                    turn.card = parked
                    result = "Waiting for the operator to approve this call on a card; it has not run."
                else:
                    result = parked
        else:
            result = execute_tool(name, args)
            turn.tools_run.append(name)
        history.append({"role": "tool", "tool_name": name, "content": result})

    if turn.card is not None:
        # No summary turn: the card is the answer, and the next input answers the card.
        turn.text = turn.card.render()
        return turn

    second = chat(history, think=False)       # no menu: one tool round per turn
    turn.replies.append(second)
    if not second.ok:
        return Turn(text=f"(no reply after tools) {second.error}", ok=False,
                    tools_run=turn.tools_run, replies=turn.replies)
    if not second.content.strip():
        return Turn(text=_empty_after(turn.tools_run), ok=False, tools_run=turn.tools_run, replies=turn.replies)
    return _answer(turn, history, second.content, offered, no_menu)
