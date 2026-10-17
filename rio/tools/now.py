# rio/tools/now.py — servicenow-coder's now-data / now-config verbs as rio tools (#5).
#
# The verbs come from the now-mcp package (orwellsanimal/now-mcp), imported as a library: its
# `verbs` modules import nothing from the MCP SDK, so the install stays pure Python under App
# Control (ADR-0002). These wrappers add three things the library cannot know:
#   * rio's credentials (SN_IDENTITY, rio.config), handed over with now_data.client.configure();
#   * docstrings with an Args: block, so each argument carries a description on the menu;
#   * the write gate: now_update is a write (ADR-0003). Its preview is the library's own dry run
#     (confirmed=False); only the approved card calls it with confirmed=True.
# now-data (0.4.0+) checks every request against the table's fields and choice lists first and
# refuses what ServiceNow would silently drop (state=new, an unknown field): the refusal is a
# ValueError carrying the fix ("use state=1"), which _call hands the model as a tool error.
# now_run_script is deliberately not offered: arbitrary server-side JavaScript is not something a
# chat model should propose on a work instance.

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rio import config
from rio.tools import register
from rio.tools.errors import ToolError

# Errors the verbs raise by design (bad table, missing record, no snapshot, no credentials) become
# refusals the model can act on; anything else stays a crash in execute_tool.
_REFUSALS = (ValueError, NotImplementedError, LookupError)


def _call(fn, **kwargs) -> Any:
    try:
        return fn(**kwargs)
    except _REFUSALS as e:
        raise ToolError(str(e)) from e
    except RuntimeError as e:  # now_data.client.ConfigError, now_config.loader.SnapshotNotFound
        raise ToolError(str(e)) from e


def _values(values: Any) -> dict:
    """now_update's `values`, which the menu offers as a JSON object in a string.

    With a nested object, qwen3:14b (Q4) closes it with `))` instead of `}}` when a value inside
    holds a parenthesis ("ATF:TEST2 (rio check)"). That's invalid JSON, so Ollama 0.23.2 drops the
    call: /api/chat returns no content and no tool_calls (#11). Inside a JSON string the model
    closes it correctly. A dict is still accepted, for a model that gets nested objects right."""
    if isinstance(values, str):
        try:
            parsed = json.loads(values)
        except json.JSONDecodeError as e:
            raise ToolError(f"values must be a JSON object, e.g. {{\"state\": \"2\"}}; could not parse it: {e}") from e
        if not isinstance(parsed, dict):
            raise ToolError(f"values must be a JSON object of field: value pairs, got a JSON {type(parsed).__name__}.")
        values = parsed
    if isinstance(values, dict):
        if not values:
            raise ToolError("values is empty: name at least one field to set, e.g. {\"state\": \"2\"}.")
        return values
    raise ToolError(f"values must be a JSON object of field: value pairs, got {type(values).__name__}.")


def load() -> list[str]:
    """Register the tools whose prerequisites are present. Returns one line per group for the banner."""
    notes = []
    try:
        from now_config import verbs as cfg  # noqa: F401
        from now_data import client, verbs as data  # noqa: F401
    except ImportError as e:
        return [f"now-* tools: not installed ({e.name}); see requirements.txt"]

    problem = config.sn_config_problem()
    if not problem:
        try:
            client.configure(config.SN_INSTANCE, *config.sn_credentials())  # now-data normalises it
        except client.ConfigError as e:  # e.g. a password of spaces, which sn_config_problem() lets by
            problem = str(e)
    if problem:
        notes.append(f"now-data tools: off ({problem})")
    else:
        off, warning = _check_fields()
        if off:
            notes.append(f"now-data tools: off ({off})")
        else:
            _register_data()
            notes.append("now-data tools: query, get, aggregate"
                         + ("" if config.READ_ONLY else ", update (approval card)")
                         + (" [read-only]" if config.READ_ONLY else "")
                         + (f" [warning: {warning}]" if warning else ""))

    if not config.INSTANCE_CONFIG_ROOT:
        notes.append("now-config tools: off (INSTANCE_CONFIG_ROOT not set; make one with "
                     "`python -m rio.snapshot`, which prints the line to put in .env)")
        return notes
    off = missing_schema(config.INSTANCE_CONFIG_ROOT) or _other_instance()
    if off:
        notes.append(f"now-config tools: off ({off})")
        return notes
    try:
        cfg.use_snapshot(root=config.INSTANCE_CONFIG_ROOT)
        cfg.now_field_exists(table="task", field="number")  # reads tables.json and columns.json
    except Exception as e:
        notes.append(f"now-config tools: off ({e})")
    else:
        _register_config()
        notes.append("now-config tools: field_exists, choice_values")
    return notes


# What now_field_exists and now_choice_values read. The other snapshot files (properties, roles, ...)
# are optional: the exporter skips one its user can't read, and these two tools don't need them.
SCHEMA_FILES = ("schema/tables.json", "schema/columns.json", "schema/choices.json")


def missing_schema(root: str | Path) -> str:
    """Why `root` can't serve the snapshot tools, or "" when it can. rio.snapshot asks the same
    question before printing the INSTANCE_CONFIG_ROOT line, so the two never disagree."""
    gone = [rel for rel in SCHEMA_FILES if not (Path(root).expanduser() / rel).is_file()]
    if gone:
        return f"{root} has no {', '.join(gone)}; run `python -m rio.snapshot` without --only"
    return ""


def _instance_key(url: str) -> tuple[str, int | None]:
    """(host, port), ignoring case, scheme, path and a default port: how now-data's exporter tells
    whether a folder already holds this instance's snapshot (now-mcp#16/#17)."""
    from urllib.parse import urlsplit

    from now_data.client import instance_url

    parts = urlsplit(instance_url(url))
    return (parts.hostname or "").rstrip("."), None if parts.port in (None, 80, 443) else parts.port


def _other_instance() -> str:
    """Why the snapshot can't be trusted for SN_INSTANCE, or "" when it can (#22).

    A snapshot answers for the instance it came from: choice values differ between instances, so a
    lab snapshot would give confident wrong answers about a work instance. So this fails closed. A
    snapshot without a readable instance.json is refused too: the exporter removes that file before
    it writes and puts it back last, so its absence can mean a re-export died part-way and left two
    instances' files mixed. With no SN_INSTANCE there is no instance to confuse it with."""
    if not config.SN_INSTANCE:
        return ""
    redo = "re-export it with `python -m rio.snapshot`"
    try:
        ours = _instance_key(config.SN_INSTANCE)
    except RuntimeError as e:  # now_data ConfigError: nothing to check the snapshot against
        return f"can't check the snapshot against SN_INSTANCE ({e})"
    try:
        snap = json.loads((Path(config.INSTANCE_CONFIG_ROOT).expanduser() / "instance.json").read_text(encoding="utf-8"))
        url = snap["instance_url"]
        if not isinstance(url, str):
            raise TypeError(f"instance_url is a {type(url).__name__}")
        theirs = _instance_key(url)
    except OSError:
        return f"the snapshot has no instance.json, so which instance it is of is unknown; {redo}"
    except (ValueError, KeyError, TypeError, RuntimeError) as e:
        return f"the snapshot's instance.json is unreadable ({type(e).__name__}: {e}); {redo}"
    if theirs != ours:
        return f"the snapshot is of {theirs[0]}, but SN_INSTANCE is {ours[0]}; {redo}"
    return ""


def _check_fields() -> tuple[str, str]:
    """One UI Meta read at startup (#14): every now-data verb checks the table's fields first, and
    fails closed (FieldsUnreadable) when it can't read them. Returns (off, warning).

    Two errors turn the tools off, because every call would fail the same way until the operator
    fixes something and restarts: FieldsUnreadable, and ConfigError (now-data 0.4.3+ raises it at
    once for a 401, a host that doesn't resolve, an untrusted certificate). The operator sees why
    now, not on the first question. Anything else (the wake check giving up on a slow instance)
    leaves them on with a warning: the first call tries again."""
    from now_data import client, query_check
    try:
        query_check.table_columns(client.get_client(), "incident")
    except query_check.FieldsUnreadable as e:
        return f"fields unreadable: {e}", ""
    except client.ConfigError as e:
        return f"cannot connect: {e}", ""
    except Exception as e:
        return "", f"could not check field access at startup: {type(e).__name__}: {e}"
    return "", ""


def _register_data() -> None:
    from now_data import verbs as data

    @register("now_query", group="servicenow", writes=False)
    def now_query(table: str, query: str = "", fields: str = "", limit: int = 10) -> dict:
        """Find records by encoded query (by number: query number=INC0010001). Not for "how many": use now_aggregate.

        Returns {count, truncated, records} from the live instance. count is the rows returned,
        capped by limit; when truncated there are more, and a "total" note says to count instead.

        Args:
          table: table name, e.g. incident, change_request, sys_user
          query: encoded query, e.g. active=true^priority=1 (empty = all rows)
          fields: comma-separated fields to return, e.g. number,short_description,state; keep it short
          limit: maximum rows, default 10
        """
        out = _call(data.now_query, table=table, query=query, fields=fields, limit=limit,
                    display_value=True)
        if isinstance(out, dict) and out.get("truncated"):
            # count is len(rows) after the cap; a model reads it as the total (#9 review)
            out = {**out, "total": f"unknown: more than {out.get('count')} (only the first {out.get('count')} "
                                   "were returned); use now_aggregate for the number"}
        return out

    @register("now_get", group="servicenow", writes=False)
    def now_get(table: str, sys_id: str, fields: str = "") -> dict:
        """Fetch one record by its 32-character hex sys_id. NOT for numbers like INC0010001: use now_query.

        Args:
          table: table name, e.g. incident
          sys_id: the record's 32-character sys_id (not its number; use now_query for a number)
          fields: comma-separated fields to return; empty returns all of them
        """
        return _call(data.now_get, table=table, sys_id=sys_id, fields=fields, display_value=True)

    @register("now_aggregate", group="servicenow", writes=False)
    def now_aggregate(table: str, query: str = "", group_by: str = "") -> dict:
        """How many records match an encoded query: the real total, optionally grouped by a field.

        Args:
          table: table name, e.g. incident
          query: encoded query to filter first, e.g. active=true
          group_by: field to group the counts by, e.g. priority or assignment_group
        """
        return _call(data.now_aggregate, table=table, query=query, group_by=group_by, count=True)

    def _update_preview(table: str, sys_id: str, values: str) -> str:
        p = _call(data.now_update, table=table, sys_id=sys_id, values=_values(values), confirmed=False)
        lines = [f"  on {table} {sys_id}:"]
        lines += [f"    {c['field']}: {json.dumps(c['current'], default=str)} -> {json.dumps(c['proposed'], default=str)}"
                  for c in p.get("changes", [])] or ["    (no field would change)"]
        if p.get("unknown_fields"):
            lines.append(f"    not written, no such field: {', '.join(p['unknown_fields'])}")
        return "\n".join(lines)

    @register("now_update", group="servicenow", writes=True, preview=_update_preview)
    def now_update(table: str, sys_id: str, values: str) -> dict:
        """Change fields on one record on the live instance. The operator approves it on a card first.

        Args:
          table: table name, e.g. incident
          sys_id: the record's 32-character sys_id
          values: the fields to set, as a JSON object in a string, e.g. {"state": "2", "work_notes": "..."}
        """
        return _call(data.now_update, table=table, sys_id=sys_id, values=_values(values), confirmed=True)



def _register_config() -> None:
    from now_config import verbs as cfg

    @register("now_field_exists", group="servicenow", writes=False)
    def now_field_exists(table: str, field: str) -> dict:
        """Check a field exists on a table (inherited fields included), from the instance-config snapshot.

        Args:
          table: table name, e.g. incident
          field: field (column) name, e.g. cmdb_ci
        """
        return _call(cfg.now_field_exists, table=table, field=field)

    @register("now_choice_values", group="servicenow", writes=False)
    def now_choice_values(table: str, field: str) -> list:
        """The valid {value, label} choices for a choice field, from the instance-config snapshot.

        Args:
          table: table name, e.g. incident
          field: choice field name, e.g. state or priority
        """
        return _call(cfg.now_choice_values, table=table, field=field)
