# rio/approval.py — a write the model asks for waits on a card; only the next "y" runs it.
#
# Carried from whitcortex agent/runner.py's pending-call card (#2621, #2620; ADR-0129/0130 there,
# ADR-0003 here), for a terminal with one conversation:
#   * when the model names a write, its tool and args are kept and the card shows the tool's own
#     preview of what would change; nothing runs;
#   * the very next input answers the card, whatever it says: "y"/"yes" runs THAT call, once,
#     through execute_tool, and the reply is the tool's own result (no model turn between the
#     operator's yes and the write, which is where whitcortex's unasked writes came from);
#     anything else runs nothing;
#   * in memory on purpose: a restart drops the card.

from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass

from rio.tools import TOOLS, execute_tool
from rio.tools.errors import ToolError

_YES = re.compile(r"\s*(y|yes)\s*[.!]?\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class Card:
    tool: str
    args: dict
    preview: str

    def render(self) -> str:
        return (f"APPROVAL NEEDED: {self.tool}\n"
                f"  args: {json.dumps(self.args, ensure_ascii=False, default=str)}\n"
                f"{self.preview}\n"
                f"Run it? y = run exactly this, anything else = don't.")


def park(tool: str, args: dict) -> Card | str:
    """A card for this write, or the reason there is none (text for the model). Never runs the write."""
    t = TOOLS.get(tool)
    if t is None or not t.writes:
        return f"Tool error: {tool!r} is not a write tool; nothing to approve."
    if not isinstance(args, dict):
        return f"Tool error ({tool}): arguments must be a JSON object, got {type(args).__name__}."
    try:
        inspect.signature(t.fn).bind(**args)
    except TypeError as e:
        return f"Tool error ({tool}): bad arguments: {e}"
    try:
        preview = t.preview(**args)
    except ToolError as e:
        return f"Tool error ({tool}): {e}"
    except Exception as e:
        return f"Tool error ({tool}): could not preview: {type(e).__name__}: {e}"
    return Card(tool, dict(args), preview)


def answer(card: Card, text: str) -> str:
    """The operator's reply to the card shown just before it."""
    if _YES.match(text or ""):
        return execute_tool(card.tool, card.args, approved=True)
    return "Not done: nothing was changed."
