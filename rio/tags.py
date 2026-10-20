# rio/tags.py — ServiceNow tags on a record, through now_query and now_insert (#1).
#
# Carried from whitcortex tools/servicenow.py (sn_tag_incident, _tag_id, #2627). A tag is a `label`
# row (a name, viewable by everyone, type standard), and tagging a record adds a `label_entry` row
# pointing at it (table + table_key), shaped like the ones the UI makes. now_insert reads every
# inserted value back (now-mcp#3); a row it created but couldn't confirm is named, never retried
# (whitcortex #2611, PR #2650 review).

from __future__ import annotations

from now_data import verbs as data

from rio.servicenow import query_safe, rows
from rio.tools.errors import ToolError
from rio.tools.now import call_verb


LABEL_MAX = 40     # label.name's max_length (UI Meta, dev442835, 2026-10-19): longer is cut on insert


def literal(value: str) -> str:
    """A value for `field=<value>` in an encoded query: a caret doubled is a literal caret, where
    a single one would end the term and start another (now-data query_check, measured). A tag
    name is derived from instance text (a CI's name), so it can hold one."""
    return value.replace("^", "^^")


def names(raw: list[str]) -> list[str]:
    """Tag names: trimmed, cut to what label.name holds (so an existing cut tag is found again, not
    re-created with a read-back that differs), blank and duplicate ones dropped, order kept. A name
    holding a script prefix is dropped: it would run in the lookup (PR #25 review). Carets survive,
    doubled by literal()."""
    out = (" ".join(str(t).split())[:LABEL_MAX].strip() for t in raw)
    return list(dict.fromkeys(n for n in out if n and query_safe(n.replace("^", ""))))


def existing_tag(name: str) -> str:
    """The sys_id of the everyone-visible tag called `name`, or "". Several same-named tags: always
    the oldest (whitcortex PR #2628)."""
    found = rows("label", f"name={literal(name)}^viewable_by=everyone^active=true^ORDERBYsys_created_on",
                 ("sys_id",), limit=1)
    return found[0]["sys_id"]["value"] if found else ""


def plan(table: str, sys_id: str, tags: list[str]) -> dict:
    """What tagging would do, without doing it: {"new": [names to create], "add": [names to put on
    the record], "already": [names it has]}."""
    out = {"new": [], "add": [], "already": []}
    for name in names(tags):
        tag_id = existing_tag(name)
        if not tag_id:
            out["new"].append(name)
            out["add"].append(name)
        elif rows("label_entry", f"label={tag_id}^table={table}^table_key={sys_id}", ("sys_id",), limit=1):
            out["already"].append(name)
        else:
            out["add"].append(name)
    return out


def _insert(table: str, values: dict, what: str) -> str:
    out = call_verb(data.now_insert, table=table, values=values, confirmed=True)
    if out.get("action") != "inserted":
        kept = "; ".join(f"{d['field']} read back {d['actual']!r}, not {d['requested']!r}"
                         for d in out.get("differences") or [])
        raise ToolError(f"{what}: created as {table} {out.get('sys_id')}, but {kept or 'it differs'}. "
                        "Do not retry; check the record.")
    return str(out["sys_id"])


def tag(table: str, sys_id: str, number: str, tags: list[str]) -> str:
    """Put the tags on the record: an existing tag is reused, a missing one created, one the record
    already has left as it is. Returns what happened, for the operator."""
    wanted = names(tags)
    if not wanted:
        raise ToolError("no tags to add")
    added, already = [], []
    for name in wanted:
        tag_id = existing_tag(name) or _insert(
            "label", {"name": name, "viewable_by": "everyone", "type": "standard", "active": "true"},
            f"Tag '{name}'")
        if rows("label_entry", f"label={tag_id}^table={table}^table_key={sys_id}", ("sys_id",), limit=1):
            already.append(name)
            continue
        label = table.replace("_", " ").title()
        _insert("label_entry", {"label": tag_id, "table": table, "table_key": sys_id,
                                "title": f"{label} - {number}",
                                "url": f"{table}.do?sys_id={sys_id}&sysparm_view="},
                f"Tag '{name}' on {number}")
        added.append(name)
    parts = [f"Tagged {number}: {', '.join(added)}" if added else f"No new tags on {number}"]
    if already:
        parts.append(f"already tagged: {', '.join(already)}")
    return "; ".join(parts) + "."
