# rio/commands.py — what the operator types that rio answers itself, before any model turn.
#
# Carried from whitcortex agent/runner.py's intercepts (#2643, #2614) and the triage apply command
# (whitcortex ADR-0128). A message that IS a command is answered without a model turn in between to
# reword it or drop the link:
#   <number> / status <number>   the record's status card
#   triage <INC>                 the triage dry run (one model call for the classifier, not three)
#   apply triage <INC>           a card for the saved proposal; only `y` applies it (rio#1 plan of
#                                record: whitcortex applied on the typed command itself)
# A sentence that mentions a number is not a command; it goes to the model.

from __future__ import annotations

import re

from rio import approval
from rio.tools import TOOLS, execute_tool

_STATUS_COMMAND = re.compile(
    r"^\s*(?:(?:sn\s+)?status\s+(?:of\s+|for\s+)?)?([A-Za-z]{2,10}\d{5,})\s*[.!?]?\s*$",
    re.IGNORECASE)
_TRIAGE_COMMAND = re.compile(r"^\s*triage\s+(INC\d{5,})\s*[.!]?\s*$", re.IGNORECASE)
_APPLY_COMMAND = re.compile(
    r"^\s*(?:apply\s+triage|triage\s+apply)\s+(INC\d{5,})\s*[.!]?\s*$", re.IGNORECASE)
_OFF = "the record tools are off (see the startup banner)."


def _number(pattern: re.Pattern, text: str) -> str | None:
    m = pattern.match(text or "")
    return m.group(1).upper() if m else None


def status_number(text: str) -> str | None:
    """The record number when the WHOLE message asks for one record's status:
    `INC0000059`, `status INC0000059`, `status of chg0000023`, else None.

    A hex id that starts with two letters (`ab123456`) reads as a record number; the cost is one
    "not found" (whitcortex PR #2645 review): pinned, not prevented."""
    return _number(_STATUS_COMMAND, text)


def triage_number(text: str) -> str | None:
    """`triage INC0010023`, the whole message, else None."""
    return _number(_TRIAGE_COMMAND, text)


def apply_number(text: str) -> str | None:
    """`apply triage INC0010023` / `triage apply INC0010023`, the whole message, any case, an
    optional '.' or '!', else None. A question or a sentence that mentions it does not match:
    applying is the operator's typed decision, not something to infer (whitcortex #2614)."""
    return _number(_APPLY_COMMAND, text)


def _run(tool: str, number: str, refused: str) -> str:
    """The tool's text, or why there is none. Through execute_tool, the one dispatcher, so a
    refusal (no such record) comes back as text like any tool's."""
    if tool not in TOOLS:
        return f"{refused} {number}: {_OFF}"
    result = execute_tool(tool, {"number": number})
    return f"{refused} {number}: {result}" if result.startswith("Tool error") else result


def status_card(number: str) -> str:
    """The card, or why there is none."""
    return _run("sn_record_status", number, "No status card for")


def handle(text: str) -> tuple[str, str, approval.Card | None] | None:
    """(a label for the history, what to show, a card now waiting) when `text` is a command,
    else None."""
    if number := apply_number(text):
        if "sn_triage_apply" not in TOOLS:
            return f"apply triage {number}", f"Not applied: {_OFF}", None
        parked = approval.park("sn_triage_apply", {"number": number}, operator=True)
        if isinstance(parked, approval.Card):
            return f"apply triage {number}", parked.render(), parked
        return f"apply triage {number}", f"Not applied: {parked}", None
    if number := triage_number(text):
        return f"sn_triage {number}", _run("sn_triage", number, "No triage for"), None
    if number := status_number(text):
        return f"sn_record_status {number}", status_card(number), None
    return None
