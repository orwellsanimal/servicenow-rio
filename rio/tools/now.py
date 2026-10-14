# rio/tools/now.py — servicenow-coder's now-data / now-config verbs as rio tools (#5).
#
# The verbs come from the now-mcp package (orwellsanimal/now-mcp), imported as a library: its
# `verbs` modules import nothing from the MCP SDK, so the install stays pure Python under App
# Control (ADR-0002). These wrappers add three things the library cannot know:
#   * rio's credentials (SN_IDENTITY, rio.config), handed over with now_data.client.configure();
#   * docstrings with an Args: block, so each argument carries a description on the menu;
#   * the write gate: now_update is a write (ADR-0003). Its preview is the library's own dry run
#     (confirmed=False); only the approved card calls it with confirmed=True.
# now_run_script is deliberately not offered: arbitrary server-side JavaScript is not something a
# chat model should propose on a work instance.

from __future__ import annotations

import json
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


def load() -> list[str]:
    """Register the tools whose prerequisites are present. Returns one line per group for the banner."""
    notes = []
    try:
        from now_config import verbs as cfg  # noqa: F401
        from now_data import client, verbs as data  # noqa: F401
    except ImportError as e:
        return [f"now-* tools: not installed ({e.name}); see requirements.txt"]

    problem = config.sn_config_problem()
    if problem:
        notes.append(f"now-data tools: off ({problem})")
    else:
        client.configure(config.SN_INSTANCE, *config.sn_credentials())  # now-data normalises it
        _register_data()
        notes.append("now-data tools: query, get, aggregate"
                     + ("" if config.READ_ONLY else ", update (approval card)")
                     + (" [read-only]" if config.READ_ONLY else ""))

    if not config.INSTANCE_CONFIG_ROOT:
        notes.append("now-config tools: off (INSTANCE_CONFIG_ROOT not set)")
    else:
        try:
            cfg.use_snapshot(root=config.INSTANCE_CONFIG_ROOT)
            cfg.now_role_exists(role="admin")  # loads one file: proves the path holds a snapshot
        except Exception as e:
            notes.append(f"now-config tools: off ({e})")
        else:
            _register_config()
            notes.append("now-config tools: field_exists, choice_values")
    return notes


def _register_data() -> None:
    from now_data import verbs as data

    @register("now_query", group="servicenow", writes=False)
    def now_query(table: str, query: str = "", fields: str = "", limit: int = 10) -> dict:
        """Find records on the live instance with an encoded query. Returns {count, truncated, records}.

        Args:
          table: table name, e.g. incident, change_request, sys_user
          query: encoded query, e.g. active=true^priority=1 (empty = all rows)
          fields: comma-separated fields to return, e.g. number,short_description,state; keep it short
          limit: maximum rows, default 10
        """
        return _call(data.now_query, table=table, query=query, fields=fields, limit=limit,
                     display_value=True)

    @register("now_get", group="servicenow", writes=False)
    def now_get(table: str, sys_id: str, fields: str = "") -> dict:
        """Fetch one record from the live instance by its 32-character sys_id.

        Args:
          table: table name, e.g. incident
          sys_id: the record's 32-character sys_id (not its number; use now_query for a number)
          fields: comma-separated fields to return; empty returns all of them
        """
        return _call(data.now_get, table=table, sys_id=sys_id, fields=fields, display_value=True)

    @register("now_aggregate", group="servicenow", writes=False)
    def now_aggregate(table: str, query: str = "", group_by: str = "") -> dict:
        """Count records on the live instance, optionally grouped by a field.

        Args:
          table: table name, e.g. incident
          query: encoded query to filter first, e.g. active=true
          group_by: field to group the counts by, e.g. priority or assignment_group
        """
        return _call(data.now_aggregate, table=table, query=query, group_by=group_by, count=True)

    def _update_preview(table: str, sys_id: str, values: dict) -> str:
        p = _call(data.now_update, table=table, sys_id=sys_id, values=values, confirmed=False)
        lines = [f"  on {table} {sys_id}:"]
        lines += [f"    {c['field']}: {json.dumps(c['current'], default=str)} -> {json.dumps(c['proposed'], default=str)}"
                  for c in p.get("changes", [])] or ["    (no field would change)"]
        if p.get("unknown_fields"):
            lines.append(f"    not written, no such field: {', '.join(p['unknown_fields'])}")
        return "\n".join(lines)

    @register("now_update", group="servicenow", writes=True, preview=_update_preview)
    def now_update(table: str, sys_id: str, values: dict) -> dict:
        """Change fields on one record on the live instance. The operator approves it on a card first.

        Args:
          table: table name, e.g. incident
          sys_id: the record's 32-character sys_id
          values: the fields to set, as an object, e.g. {"state": "2", "work_notes": "..."}
        """
        return _call(data.now_update, table=table, sys_id=sys_id, values=values, confirmed=True)


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
