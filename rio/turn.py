# rio/turn.py — one chat turn: at most one round of tool calls, and writes stop at a card.
#
# Native tool calling only (the model must list the `tools` capability, ADR-0002); a model without
# it gets a plain chat turn and no menu. One round per turn keeps the CPU cost of a turn bounded:
# the model asks for tools once, rio runs the reads, and a second call (without the menu) answers
# from their results. A write is never run here: the first one parks on a card (rio.approval) and
# the turn ends there, so the operator sees the call before anything happens (ADR-0003).

from __future__ import annotations

import json
from dataclasses import dataclass, field

from rio import llm
from rio.approval import Card, park
from rio.tools import execute_tool, is_write, schemas


# A reply with neither text nor a tool call. On the first call Ollama returns this when it can't
# parse a tool call the model generated; the turn is dropped (not added to history) and the operator
# told. After a read round the reads did run (a write would have parked on a card first), so the
# second message says what ran and that nothing was changed.
_EMPTY = ("(no reply) the model returned neither text nor a tool call; Ollama may have failed to "
          "parse a tool call. Nothing ran. Try rephrasing.")


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
    turn = Turn()
    first = chat(history, tools=menu or None, think=False)
    turn.replies.append(first)
    if not first.ok:
        return Turn(text=f"(no reply) {first.error}", ok=False, replies=turn.replies)
    if not first.tool_calls and not first.content.strip():
        return Turn(text=_EMPTY, ok=False, replies=turn.replies)
    if not first.tool_calls:
        history.append({"role": "assistant", "content": first.content})
        turn.text = first.content.strip()
        return turn

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
    history.append({"role": "assistant", "content": second.content})
    turn.text = second.content.strip()
    return turn
