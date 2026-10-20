# rio/servicenow.py — ServiceNow records as data, read through the now-data verbs, and the status card.
#
# Carried from whitcortex tools/servicenow.py (the record status card, #2643) and trimmed. whitcortex
# read records through its own PySNC client; rio does not carry that client. The operator retired
# whitcortex's sn_* tool layer in favour of servicenow-coder's now-data verbs (whitcortex#2519;
# decided for rio on rio#1, 2026-10-13), so every read here is now_query, with now-data's field check
# in front of it. Each field comes back as {"value", "display"}: code compares and writes the value
# (a sys_id, a choice value), and the operator reads the display.
#
# The card is plain text, not whitcortex's markdown: rio runs in a terminal, where `**[INC…](url)**`
# is noise and the bare URL is what a terminal makes clickable. Instance text loses its control
# characters for the same reason whitcortex escaped markdown (PR #2645 review): a work note must not
# be able to drive the operator's screen, and in a terminal that means escape sequences.

from __future__ import annotations

import html
import re
from typing import Any

from now_data import verbs as data

from rio import config
from rio.tools.errors import ToolError
from rio.tools.now import call_verb


def _pair(raw: Any) -> dict:
    """A field as {"value", "display"} from now_query's display_value="all" shape."""
    if isinstance(raw, dict):
        return {"value": str(raw.get("value") or ""), "display": str(raw.get("display_value") or "")}
    text = "" if raw is None else str(raw)
    return {"value": text, "display": text}


_NUMBER = re.compile(r"[A-Za-z]{2,10}\d{5,}")


def record_number(number: Any) -> str:
    """A record number fit to go into an encoded query, upper-cased, or a refusal.

    The model supplies it, and it goes into `number=<it>` as text: `INC0010001^ORactive=true` would
    widen the read to every active record and the first row would be shown as the answer (an
    encoded query has no quoting; servicenow-coder lessons: "invalid encoded query returns all
    rows"). whitcortex's add_query had the same exposure; rio refuses anything but letters then
    digits, the shape the bare-number command accepts."""
    text = str(number or "").strip().upper()
    if not _NUMBER.fullmatch(text):
        raise ToolError(f"{number!r} is not a record number: expected letters then digits, e.g. INC0010023.")
    return text


def rows(table: str, query: str, fields: tuple[str, ...], limit: int) -> list[dict]:
    """Records matching an encoded query, each field as {"value", "display"}.

    `limit` is required on purpose: now_query caps a read at 1000 rows, and a caller that has to
    know it saw everything compares len() with its own limit (whitcortex PR #2642's "capped")."""
    out = call_verb(data.now_query, table=table, query=query, fields=",".join(fields), limit=limit,
                    display_value="all")
    return [{f: _pair(r.get(f)) for f in fields} for r in out.get("records") or []]


# The fields triage, enrichment and resolve read (whitcortex #2597's _TRIAGE_FIELDS, plus what
# resolve needs). sys_mod_count is what lets `apply triage` refuse an incident changed since the
# proposal.
INCIDENT_FIELDS = ("sys_id", "number", "state", "short_description", "description", "category",
                   "subcategory", "cmdb_ci", "assignment_group", "assigned_to", "caller_id",
                   "close_code", "close_notes", "sys_mod_count")


def incident_record(number: str) -> dict:
    """The incident as data for code, not text for a model (whitcortex #2597): each field as
    {"value", "display"}. An unknown number is a refusal (whitcortex #2113)."""
    number = record_number(number)
    found = rows("incident", f"number={number}", INCIDENT_FIELDS, limit=1)
    if not found:
        raise ToolError(f"Incident {number} not found.")
    return found[0]


def ticket_text(record: dict) -> str:
    return f"{record['short_description']['value']}\n{record['description']['value']}"


# ── Record status card (#2643) ───────────────────────────────────────────────
#
# One read on the parent `task` table resolves any record number on the instance (measured
# 2026-10-13 on dev442835: INC, CS, CHG, PRB, RITM, REQ, CTASK, SCTASK, ONB, CMPL and their task
# classes), and `sys_class_name` names the record's own table, which is what the link needs. The
# latest work note or comment is the newest `sys_journal_field` row on the record.

TASK_FIELDS = ("sys_id", "number", "sys_class_name", "state", "priority", "short_description",
               "assigned_to", "assignment_group")
_JOURNAL_FIELDS = ("sys_id", "element", "value", "sys_created_on", "sys_created_by")
LAST_ENTRY_CHARS = 300
JOURNAL_FETCH = 5           # rows read before picking the newest; see record_status
_MARKUP = re.compile(r"\[/?code\]|<[^>]*>")
# C0 and C1 controls, DEL included: ESC (\x1b) starts a terminal escape sequence, and the C1 CSI
# (\x9b) does on some terminals. Tabs and newlines go too; the card is one fact per line.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# A lone surrogate (half of a pair cut by a max_length truncation) can't be encoded for the screen:
# print() raises and rio would exit mid-conversation (PR #24 review).
_SURROGATE = re.compile(r"[\ud800-\udfff]")
_TABLE = re.compile(r"[a-z][a-z0-9_]*")
_SYS_ID = re.compile(r"[0-9a-f]{32}")


def safe(value: str) -> str:
    """Any instance text for the terminal: control characters and lone surrogates gone, whitespace
    squashed. Nothing else changes: `<jane@example.com>` or `n < 5` in a short description is text."""
    text = _SURROGATE.sub("\ufffd", _CONTROL.sub(" ", value or ""))
    return re.sub(r"\s+", " ", text).strip()


def plain(value: str) -> str:
    """A journal entry for the terminal: its markup gone, then safe(). Case comments arrive as
    [code]<p>…</p>[/code] (measured on dev442835, whitcortex #2643). Only journal text is HTML;
    whitcortex stripped markup from it alone, and so does rio."""
    return safe(html.unescape(_MARKUP.sub(" ", value or "")))


def record_status(number: str) -> dict:
    """The record's status fields as data, plus its newest work note or comment.
    An unknown number is a refusal, like every identifier miss (whitcortex #2113)."""
    number = record_number(number)
    # Two rows, not one: nothing on the platform makes task.number unique (an import, a number
    # counter reset after a clone), and a card that silently shows one of two is wrong half the time.
    found = rows("task", f"number={number}", TASK_FIELDS, limit=2)
    if not found:
        raise ToolError(f"No task record numbered {number}.")
    rec = found[0]
    rec["duplicate"] = len(found) > 1
    sys_id = rec["sys_id"]["value"]
    # sys_created_on has one-second resolution, and two entries in one second (a note and a comment
    # from one action) tie: the instance's order is then undefined. The newest is picked here, ties
    # broken by sys_id, so the same record always shows the same entry (whitcortex PR #2645 review).
    try:
        journal = rows("sys_journal_field",
                       f"element_id={sys_id}^elementINwork_notes,comments^ORDERBYDESCsys_created_on",
                       _JOURNAL_FIELDS, limit=JOURNAL_FETCH)
    except ToolError as e:
        # now-data refuses a table whose fields it can't read. The card is still worth having; it
        # says the journal wasn't read rather than that there are no notes.
        rec["last_entry"], rec["journal_error"] = None, str(e)
    else:
        newest = max(journal, key=lambda j: (j["sys_created_on"]["value"], j["sys_id"]["value"]),
                     default=None)
        rec["last_entry"] = None if newest is None else {
            "kind": newest["element"]["value"],
            "text": plain(newest["value"]["value"]),
            "by": newest["sys_created_by"]["value"],
            "when": newest["sys_created_on"]["display"] or newest["sys_created_on"]["value"]}
        rec["journal_error"] = ""
    table = rec["sys_class_name"]["value"]
    # Both come from the instance; a link is built only from what a table name and a sys_id can be.
    rec["url"] = (f"{config.sn_instance_url()}/{table}.do?sys_id={sys_id}"
                  if _TABLE.fullmatch(table) and _SYS_ID.fullmatch(sys_id) else "")
    return rec


def render_status_card(rec: dict) -> str:
    """One fact per line: number · type · state · priority; the short description; assignee and
    group; the latest note or comment with who and when; the link."""
    head = [rec["number"]["value"],
            rec["sys_class_name"]["display"] or rec["sys_class_name"]["value"],
            rec["state"]["display"] or rec["state"]["value"] or "no state"]
    if rec["priority"]["display"]:
        head.append(rec["priority"]["display"])
    lines = [" · ".join(safe(h) for h in head),
             safe(rec["short_description"]["value"]) or "(no short description)",
             f"Assigned: {safe(rec['assigned_to']['display']) or 'unassigned'} · "
             f"Group: {safe(rec['assignment_group']['display']) or 'none'}"]
    last = rec["last_entry"]
    if last:
        label = "Last work note" if last["kind"] == "work_notes" else "Last comment"
        text = last["text"][:LAST_ENTRY_CHARS] + ("…" if len(last["text"]) > LAST_ENTRY_CHARS else "")
        lines.append(f"{label} ({safe(last['by']) or 'unknown'}, {safe(last['when']) or 'no time'}): "
                     f"{text or '(empty)'}")
    elif rec.get("journal_error"):
        lines.append(f"Work notes and comments not read: {safe(rec['journal_error'])}")
    else:
        # An account that can't read the journal's rows gets none, which can't be told from
        # "no entries" (whitcortex #2643).
        lines.append("No work notes or comments (none, or not readable by this account).")
    lines.append(rec["url"] or "(no link: the instance returned an unexpected table name or sys_id)")
    if rec.get("duplicate"):
        lines.append(f"Note: another task record is also numbered {rec['number']['value']}; this card "
                     "shows the first the instance returned.")
    return "\n".join(lines)
