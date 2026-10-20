# rio/tags.py — ServiceNow tags on a record, through now_query and now_insert (#1).
#
# Carried from whitcortex tools/servicenow.py (sn_tag_incident, _tag_id, #2627). A tag is a `label`
# row (a name, viewable by everyone, type standard), and tagging a record adds a `label_entry` row
# pointing at it (table + table_key), shaped like the ones the UI makes. now_insert reads every
# inserted value back (now-mcp#3); a row it created but couldn't confirm is named, never retried
# (whitcortex #2611, PR #2650 review).

from __future__ import annotations

from now_data import verbs as data

from rio.servicenow import is_sys_id, query_safe, rows, safe
from rio.tools.errors import ToolError
from rio.tools.now import call_verb

LABEL_MAX = 40     # label.name's max_length (UI Meta, dev442835, 2026-10-19): longer is cut on insert


def names(raw: list[str]) -> list[str]:
    """Tag names: one safe line each, cut to what label.name holds (so an existing cut tag is found
    again, not re-created with a read-back that differs), blank and duplicate ones dropped (case
    aside, as the instance's `name=` matches), order kept.

    A tag name is someone else's text (a CI's name, a choice label), and it goes into `name=<it>`, so
    it must be query_safe: no script prefix, and no caret. Whether `^^` reads as one literal caret for
    `=` is unmeasured, and a name ending in one made `name=Edge^^^viewable_by…`, which no parser agrees
    on (PR #26 review), so a name with a caret is dropped rather than escaped. A comma is fine in `=`."""
    out: dict[str, str] = {}
    for t in raw:
        name = safe(str(t))[:LABEL_MAX].strip()
        if name and query_safe(name) and name.lower() not in out:
            out[name.lower()] = name
    return list(out.values())


def existing_tag(name: str) -> str:
    """The sys_id of the everyone-visible, active tag called `name`, or "". Several same-named tags:
    always the oldest (whitcortex PR #2628). `name` comes from names(), so it is query_safe."""
    found = rows("label", f"name={name}^viewable_by=everyone^active=true^ORDERBYsys_created_on",
                 ("sys_id",), limit=1)
    if not found:
        return ""
    tag_id = found[0]["sys_id"]["value"]
    if not is_sys_id(tag_id):           # found, but not an id rio can use: refuse, don't create a second
        raise ToolError(f"the tag '{name}' exists with an id rio can't use ({tag_id!r})")
    return tag_id


def _tagged(tag_id: str, table: str, sys_id: str) -> bool:
    if not (is_sys_id(tag_id) and is_sys_id(sys_id)):
        raise ToolError(f"can't look up a tag entry with ids {tag_id!r} / {sys_id!r}")
    return bool(rows("label_entry", f"label={tag_id}^table={table}^table_key={sys_id}", ("sys_id",), limit=1))


def plan(table: str, sys_id: str, tags: list[str]) -> dict:
    """What tagging would do, without doing it: {"new": [names to create], "add": [names to put on
    the record], "already": [names it has]}."""
    out = {"new": [], "add": [], "already": []}
    for name in names(tags):
        tag_id = existing_tag(name)
        if not tag_id:
            out["new"].append(name)
            out["add"].append(name)
        elif _tagged(tag_id, table, sys_id):
            out["already"].append(name)
        else:
            out["add"].append(name)
    return out


def _insert(table: str, values: dict, what: str) -> str:
    out = call_verb(data.now_insert, table=table, values=values, confirmed=True)
    if out.get("action") != "inserted":
        kept = "; ".join(f"{d['field']} read back {d['actual']!r}, not {d['requested']!r}"
                         for d in out.get("differences") or [])
        raise ToolError(f"{what}: created as {table} {out.get('sys_id')}, but {kept or 'it differs'}")
    return str(out["sys_id"])


def tag(table: str, sys_id: str, number: str, tags: list[str]) -> str:
    """Put the tags on the record: an existing tag is reused, a missing one created, one the record
    already has left as it is. Returns what happened, for the operator. A failure part-way says
    which tags are already on and where it stopped, so the caller can say what did happen (PR #26
    review: a failed third tag read as nothing tagged)."""
    wanted = names(tags)
    if not wanted:
        raise ToolError("no tags to add")
    added, already = [], []
    for name in wanted:
        # What the step was decides what a retry risks: a lookup inserted nothing; a create may have
        # left a label (a second same-named tag next time); an entry may have left one on the record
        # (PR #26 review round 2).
        step = "looking up the tag"
        try:
            tag_id = existing_tag(name)
            if not tag_id:
                step = "creating the tag"
                tag_id = _insert("label", {"name": name, "viewable_by": "everyone", "type": "standard",
                                           "active": "true"}, f"Tag '{name}'")
            step = "looking up the tag on the record"
            if _tagged(tag_id, table, sys_id):
                already.append(name)
                continue
            step = "putting the tag on the record"
            label = table.replace("_", " ").title()
            _insert("label_entry", {"label": tag_id, "table": table, "table_key": sys_id,
                                    "title": f"{label} - {number}",
                                    "url": f"{table}.do?sys_id={sys_id}&sysparm_view="},
                    f"Tag '{name}' on {number}")
        except Exception as e:
            on = ", ".join(added + already) or "none"
            risk = {"creating the tag": f" The '{name}' tag may exist now: check the tags before asking again, "
                                        "or a retry can create a second one.",
                    "putting the tag on the record": f" '{name}' may be on {number} now: check before asking again."
                    }.get(step, " Nothing was written at that step.")
            raise ToolError(f"tagging {number} stopped at '{name}', {step} ({type(e).__name__}: {e}); tags on it "
                            f"before that: {on}.{risk}") from e
        added.append(name)
    parts = [f"Tagged {number}: {', '.join(added)}" if added else f"No new tags on {number}"]
    if already:
        parts.append(f"already tagged: {', '.join(already)}")
    return "; ".join(parts) + "."
