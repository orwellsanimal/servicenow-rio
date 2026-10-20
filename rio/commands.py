# rio/commands.py — what the operator types that rio answers itself, before any model turn.
#
# Carried from whitcortex agent/runner.py's intercepts (#2643, #2614): a message that IS a record
# number (or `status <number>`) is a command, so the card is the tool's own text, with no model in
# between to reword it or drop the link. A sentence that mentions a number is not a command; it
# goes to the model.

from __future__ import annotations

import re

from rio.tools import TOOLS, execute_tool

_STATUS_COMMAND = re.compile(
    r"^\s*(?:(?:sn\s+)?status\s+(?:of\s+|for\s+)?)?([A-Za-z]{2,10}\d{5,})\s*[.!?]?\s*$",
    re.IGNORECASE)


def status_number(text: str) -> str | None:
    """The record number when the WHOLE message asks for one record's status:
    `INC0000059`, `status INC0000059`, `status of chg0000023`, else None.

    A hex id that starts with two letters (`ab123456`) reads as a record number; the cost is one
    "not found" (whitcortex PR #2645 review): pinned, not prevented."""
    m = _STATUS_COMMAND.match(text or "")
    return m.group(1).upper() if m else None


def status_card(number: str) -> str:
    """The card, or why there is none. Runs through execute_tool, the one dispatcher, so a refusal
    (no such record) comes back as text like any tool's."""
    if "sn_record_status" not in TOOLS:
        return f"No status card for {number}: the record tools are off (see the startup banner)."
    result = execute_tool("sn_record_status", {"number": number})
    if result.startswith("Tool error"):
        return f"No status card for {number}: {result}"
    return result
