# rio/resolve.py — resolve an incident that already carries its resolution, and tag it (#1).
#
# Carried from whitcortex routines/graphs/sn_resolve.py (#2627) and trimmed:
#   read:    the incident (values + display) and its Event Management alert, if any
#   refuse:  already resolved/closed/canceled, or no close code / close notes: this never writes a
#            resolution, it only applies the one recorded
#   tags:    derived from the incident's own fields, no model: its CI, its subcategory (else
#            category), the alert's source, and its close code (operator, 2026-10-11)
#   write:   the tags, THEN state=Resolved, so a failed tagging leaves the incident unresolved rather
#            than resolved and untagged; now_update reads the state back (now-mcp#3)
#   report:  the incident's state and the alert's, read after the writes
# sn_resolve is a write: the model's call (or `resolve <INC>`) parks a card whose preview is plan(),
# and `y` runs exactly that call (ADR-0003). The card remembers the incident's sys_mod_count, and the
# write refuses one that changed while the card waited.

from __future__ import annotations

from now_data import verbs as data

from rio import tags as tagging
from rio.servicenow import incident_record, is_sys_id, record_number, rows
from rio.tools.errors import ToolError
from rio.tools.now import call_verb

_DONE_STATES = {"6": "Resolved", "7": "Closed", "8": "Canceled"}
RESOLVED = "6"

# number -> sys_mod_count the card was shown for; in memory, like the card itself (ADR-0003).
_SHOWN: dict[str, str] = {}


def read_alert(incident_sys_id: str) -> dict | None:
    """The Event Management alert bound to this incident, or None. An instance without Event
    Management (no em_alert table, or not readable) has none to report."""
    if not is_sys_id(incident_sys_id):
        return None
    try:
        found = rows("em_alert", f"incident={incident_sys_id}", ("number", "state", "source"), limit=1)
    except ToolError:
        return None
    if not found:
        return None
    return {f: found[0][f]["display"] or found[0][f]["value"] for f in ("number", "state", "source")}


def refusal(record: dict) -> str:
    """Why this incident cannot be resolved here, or ""."""
    state = record["state"]["value"]
    if state in _DONE_STATES:
        return f"it is already {_DONE_STATES[state]}"
    missing = [f for f in ("close_code", "close_notes") if not record[f]["value"].strip()]
    if missing:
        return (f"it has no {' and no '.join(m.replace('_', ' ') for m in missing)}; "
                "this applies a recorded resolution, it does not write one")
    return ""


def derive_tags(record: dict, alert: dict | None) -> list[str]:
    tags = [record["cmdb_ci"]["display"],
            record["subcategory"]["display"] or record["category"]["display"],
            (alert or {}).get("source", ""),
            record["close_code"]["display"]]
    return tagging.names([t for t in tags if t])


def _ready(number: str) -> tuple[dict, dict | None, list[str]]:
    record = incident_record(number)
    why = refusal(record)
    if why:
        raise ToolError(f"{record['number']['value']} was not resolved: {why}. Nothing was changed.")
    alert = read_alert(record["sys_id"]["value"])
    tags = derive_tags(record, alert)
    if not tags:
        raise ToolError(f"{record['number']['value']}: no tag can be derived from its fields. Nothing was changed.")
    return record, alert, tags


def preview(number: str) -> str:
    """The card: the state change, the close code it keeps, the tags, and the alert."""
    record, alert, tags = _ready(number)
    number = record["number"]["value"]
    plan = tagging.plan("incident", record["sys_id"]["value"], tags)
    _SHOWN[number] = (record["sys_id"]["value"], record["sys_mod_count"]["value"])
    lines = [f"  resolve {number}: state {record['state']['display'] or record['state']['value']} -> Resolved, "
             f"with its recorded close code {record['close_code']['display']!r}",
             f"  tags first: {', '.join(tags)}"]
    if plan["new"]:
        lines.append(f"    new tags created: {', '.join(plan['new'])}")
    if plan["already"]:
        lines.append(f"    already on it: {', '.join(plan['already'])}")
    lines.append(f"  alert: {alert['number']} ({alert['state']}), which resolving may close" if alert
                 else "  no Event Management alert is bound to it")
    return "\n".join(lines)


def run(number: str) -> str:
    """Tag, then resolve, then report. Only from the approved card."""
    number = record_number(number)
    record, alert, tags = _ready(number)
    shown = _SHOWN.pop(number, None)
    if shown is None:
        raise ToolError(f"no card was shown for resolving {number}; ask again")
    shown_id, shown_count = shown
    count = record["sys_mod_count"]["value"]
    # The same record, unchanged since the card: not another record that now has this number, and
    # not one whose count can't be read (PR #25 review, as triage's).
    if record["sys_id"]["value"] != shown_id or not count.isdigit() or count != shown_count:
        raise ToolError(f"{number} changed after the card was shown (sys_mod_count {shown_count} -> "
                        f"{count or 'unreadable'}); nothing was changed. Ask again.")
    sys_id = record["sys_id"]["value"]
    tagged = tagging.tag("incident", sys_id, number, tags)
    try:
        result = call_verb(data.now_update, table="incident", sys_id=sys_id, values={"state": RESOLVED},
                           confirmed=True)
    except Exception as e:
        # The tags landed; the state may or may not have (now-data's WriteUnconfirmed, a dropped
        # connection). Say both, so a retry isn't blind (whitcortex #2627, PR #25 review).
        raise ToolError(f"{tagged} The resolve may not have been saved ({type(e).__name__}: {e}); check "
                        f"{number}'s state before asking again.") from e
    if result.get("action") != "updated":
        kept = "; ".join(f"the instance kept {m['actual']!r}" for m in result.get("not_applied") or [])
        # A business rule or ACL can keep the state though the save answered 200 (whitcortex #2627).
        raise ToolError(f"{tagged} But it was NOT resolved: {kept or 'the state did not change'}.")
    after = incident_record(number)
    lines = [f"{number} is now {after['state']['display'] or after['state']['value']} "
             f"({record['close_code']['display']}).", tagged]
    if alert:
        now = read_alert(sys_id) or {}
        lines.append(f"Alert {alert['number']}: {alert['state']} before, {now.get('state') or 'not readable'} now.")
    else:
        lines.append("No Event Management alert is bound to it.")
    return "\n".join(lines)
