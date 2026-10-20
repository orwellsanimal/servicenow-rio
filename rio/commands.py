# rio/commands.py — what the operator types that rio answers itself, before any model turn.
#
# Carried from whitcortex agent/runner.py's intercepts (#2643, #2614) and the triage apply command
# (whitcortex ADR-0128). A message that IS a command is answered without a model turn in between to
# reword it or drop the link:
#   <number> / status <number>   the record's status card
#   triage <INC>                 the triage dry run (one model call for the classifier, not three)
#   apply triage <INC>           a card for the saved proposal; only `y` applies it (rio#1 plan of
#                                record: whitcortex applied on the typed command itself)
#   enrich <INC>                 the enrichment dry run (the two model calls, no chat turn)
#   apply enrich <INC>           a card for the saved note; `y` posts it and tags the incident
#   resolve <INC>                the sn_resolve card, without a chat turn to ask for it
# A sentence that mentions a number is not a command; it goes to the model.

from __future__ import annotations

import re

from rio import approval
from rio.tools import TOOLS, execute_tool

_STATUS_COMMAND = re.compile(
    r"^\s*(?:(?:sn\s+)?status\s+(?:of\s+|for\s+)?)?([A-Za-z]{2,10}\d{5,})\s*[.!?]?\s*$",
    re.IGNORECASE)
_TRIAGE_COMMAND = re.compile(r"^\s*triage\s+(INC\d{5,})\s*[.!]?\s*$", re.IGNORECASE)
_ENRICH_COMMAND = re.compile(r"^\s*enrich\s+(INC\d{5,})\s*[.!]?\s*$", re.IGNORECASE)
_APPLY_COMMAND = re.compile(
    r"^\s*(?:apply\s+(?P<a>triage|enrich)|(?P<b>triage|enrich)\s+apply)\s+(?P<n>INC\d{5,})\s*[.!]?\s*$",
    re.IGNORECASE)
_RESOLVE_COMMAND = re.compile(r"^\s*resolve\s+(INC\d{5,})\s*[.!]?\s*$", re.IGNORECASE)
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


def apply_target(text: str) -> tuple[str, str] | None:
    """("triage" | "enrich", the number) for `apply triage INC0010023` / `triage apply …` /
    `apply enrich …`, the whole message, any case, an optional '.' or '!', else None. A question or a
    sentence that mentions it does not match: applying is the operator's typed decision, not
    something to infer (whitcortex #2614)."""
    m = _APPLY_COMMAND.match(text or "")
    return ((m.group("a") or m.group("b")).lower(), m.group("n").upper()) if m else None


def apply_number(text: str) -> str | None:
    """The number when the whole message is an apply command, else None."""
    target = apply_target(text)
    return target[1] if target else None


def enrich_number(text: str) -> str | None:
    """`enrich INC0000059`, the whole message, else None."""
    return _number(_ENRICH_COMMAND, text)


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


def resolve_number(text: str) -> str | None:
    """`resolve INC0010049`, the whole message, else None. It only parks the card; `y` resolves."""
    return _number(_RESOLVE_COMMAND, text)


def _card(tool: str, args: dict, label: str, refused: str):
    """Park the card for a write the operator named. The card is the answer; nothing runs yet."""
    if tool not in TOOLS:
        return label, f"{refused}: {_OFF}", None
    parked = approval.park(tool, args, operator=True)
    if isinstance(parked, approval.Card):
        return label, parked.render(), parked
    return label, f"{refused}: {parked}", None


def handle(text: str) -> tuple[str, str, approval.Card | None] | None:
    """(a label for the history, what to show, a card now waiting) when `text` is a command,
    else None."""
    if target := apply_target(text):
        workflow, number = target
        tool, label = f"sn_{workflow}_apply", f"apply {workflow} {number}"
        if tool not in TOOLS:
            return label, f"Not applied: {_OFF}", None
        from rio import enrich, triage      # loaded once the record tools are, which need now-data
        # The card names the saved proposal or note it shows, so `y` can't write a different one
        # (PR #25 review).
        pending = {"triage": triage.pending_id, "enrich": enrich.pending_id}[workflow](number)
        return _card(tool, {"number": number, "proposal": pending}, label, "Not applied")
    if number := resolve_number(text):
        return _card("sn_resolve", {"number": number}, f"resolve {number}", "Not resolved")
    if number := triage_number(text):
        return f"sn_triage {number}", _run("sn_triage", number, "No triage for"), None
    if number := enrich_number(text):
        return f"sn_enrich {number}", _run("sn_enrich", number, "No enrichment for"), None
    if number := status_number(text):
        return f"sn_record_status {number}", status_card(number), None
    return None
