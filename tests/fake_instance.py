# tests/fake_instance.py — a ServiceNow instance in memory, behind the now-data verbs rio calls.
#
# Carried in spirit from whitcortex's fake GlideRecord clients (tests/test_2597_*, test_2643_*), but
# faked one level up: rio reads through now_data.verbs, so that is the seam. The encoded queries rio
# sends are evaluated, not just recorded, so a test proves the query selects the right rows.
#
# Two ways a query can be wrong on the instance and still pass a naive fake, and what this one does:
#   * an unknown field: ServiceNow drops the term and widens the read (servicenow-coder lessons:
#     "invalid encoded query returns all rows"); now-data's field check refuses it first. The fake
#     knows each table's columns (its rows' fields, the declared COLUMNS, and its parents') and
#     raises now-data's QueryRefused for one it doesn't have, in the query, `fields` or ORDER BY;
#   * a term it can't evaluate (an unknown operator, INSTANCEOF, a javascript: value, a dot-walk):
#     it fails the test instead of guessing.
# Semantics follow the instance where they're measured: `!=`, NOT IN and NOT LIKE drop empty rows
# (servicenow-coder lessons: "GlideRecord != excludes null"); `=` and IN are case-sensitive, the
# safe side, since case-insensitive matching is a MySQL collation, not a guarantee (now-data
# query_check); LIKE ignores case; numbers compare as numbers.
#
# Rows are dicts of field -> value, or field -> (value, display) for references and choices. A table
# query also returns the rows of the tables that extend it (`task` finds incidents and changes), with
# sys_class_name naming the row's own table, as on the instance. A display that is empty falls back
# to the value, as pysnc's does.

from __future__ import annotations

import re

_OPERATORS = ("NOT INSTANCEOF", "INSTANCEOF", "ISNOTEMPTY", "ISEMPTY", "NOT IN", "NOT LIKE",
              "STARTSWITH", "IN", "LIKE", "!=", ">=", "<=", "=", ">", "<")
_UNEVALUATED = ("NOT INSTANCEOF", "INSTANCEOF")
_FIELD = re.compile(r"[a-z_][a-z0-9_.]*")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")

EXTENDS = {"incident": "task", "change_request": "task", "problem": "task",
           "sn_customerservice_case": "task", "sn_onboarding_case": "sn_customerservice_case",
           "cmdb_ci_computer": "cmdb_ci", "cmdb_ci_spkg": "cmdb_ci"}
SYSTEM_COLUMNS = {"sys_id", "sys_class_name", "sys_created_on", "sys_created_by", "sys_updated_on",
                  "sys_updated_by", "sys_mod_count"}
# Columns a table has that no row in a test may happen to carry: what makes `task` a real parent.
COLUMNS = {
    "task": {"number", "state", "priority", "short_description", "description", "assigned_to",
             "assignment_group", "cmdb_ci", "active", "close_notes", "work_notes", "comments",
             "impact", "urgency", "opened_at"},
    "cmdb_ci": {"name", "operational_status", "install_status", "assigned_to", "short_description"},
}


def _parts(raw) -> tuple[str, str]:
    if isinstance(raw, tuple):
        return str(raw[0]), str(raw[1] or raw[0])
    text = "" if raw is None else str(raw)
    return text, text


def _compare(op: str, raw: str, value: str) -> bool:
    if _NUMBER.fullmatch(raw) and _NUMBER.fullmatch(value):
        a, b = float(raw), float(value)
    else:
        a, b = raw, value
    return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op]


def _condition(term: str):
    """(field, a test of one row)."""
    m = _FIELD.match(term)
    if not m:
        raise AssertionError(f"fake instance: can't read a field in query term {term!r}")
    field, rest = m.group(), term[m.end():]
    if "." in field:
        raise AssertionError(f"fake instance: doesn't evaluate the dot-walk in {term!r}")
    op = next((o for o in _OPERATORS if rest.startswith(o)), None)
    if op is None or op in _UNEVALUATED:
        raise AssertionError(f"fake instance: can't evaluate the operator in query term {term!r}")
    value = rest[len(op):]
    if value.lower().startswith("javascript:"):
        raise AssertionError(f"fake instance: doesn't run the script in {term!r}")
    values = value.split(",")

    def test(row) -> bool:
        raw = _parts(row.get(field))[0]
        if op == "ISNOTEMPTY":
            return raw != ""
        if op == "ISEMPTY":
            return raw == ""
        if op in ("!=", "NOT IN", "NOT LIKE"):      # an empty row matches none of these
            return raw != "" and {"!=": raw != value, "NOT IN": raw not in values,
                                  "NOT LIKE": value.lower() not in raw.lower()}[op]
        if op == "=":
            return raw == value
        if op == "IN":
            return raw in values
        if op == "LIKE":
            return value.lower() in raw.lower()
        if op == "STARTSWITH":
            return raw.lower().startswith(value.lower())
        return _compare(op, raw, value)
    return field, test


def parse(query: str):
    """(AND of ORs, [(field, descending)], every field named): `a^b^ORc` is a AND (b OR c), as on
    the instance."""
    clauses: list[list] = []
    order: list[tuple[str, bool]] = []
    named: list[str] = []
    for term in (query or "").split("^"):
        if not term:
            continue
        if term.startswith("ORDERBYDESC"):
            order.append((term[len("ORDERBYDESC"):], True))
            named.append(order[-1][0])
        elif term.startswith("ORDERBY"):
            order.append((term[len("ORDERBY"):], False))
            named.append(order[-1][0])
        elif term.startswith("NQ") or term.startswith("RLQUERY") or term.startswith("ENDRLQUERY"):
            raise AssertionError(f"fake instance: doesn't evaluate {term[:7]!r} in {query!r}")
        elif term.startswith("OR"):     # field names are lowercase, so an uppercase OR is the operator
            if not clauses:
                raise AssertionError(f"fake instance: {query!r} starts with an OR")
            field, test = _condition(term[2:])
            clauses[-1].append(test)
            named.append(field)
        else:
            field, test = _condition(term)
            clauses.append([test])
            named.append(field)
    return clauses, order, named


class FakeInstance:
    def __init__(self, tables: dict[str, list[dict]] | None = None):
        self.tables = {t: [dict(r) for r in rows] for t, rows in (tables or {}).items()}
        self.calls: list[tuple[str, dict]] = []
        self.refuse: dict[str, Exception] = {}      # table -> what now-data raises for it

    @staticmethod
    def _ancestors(table: str) -> list[str]:
        out, cur = [table], table
        while cur in EXTENDS:
            cur = EXTENDS[cur]
            out.append(cur)
        return out

    def columns(self, table: str) -> set[str]:
        """The table's own columns and its parents', never a child's: `task` has no caller_id."""
        cols = set(SYSTEM_COLUMNS)
        for t in self._ancestors(table):
            cols |= COLUMNS.get(t, set())
            for r in self.tables.get(t, []):
                cols |= set(r)
        return cols

    def _rows(self, table: str) -> list[dict]:
        out = []
        for t, rows in self.tables.items():
            if table in self._ancestors(t):
                out += [{"sys_class_name": (t, t), **r} for r in rows]
        return out

    def _check(self, table: str, names) -> None:
        if table in self.refuse:
            raise self.refuse[table]
        from now_data.query_check import QueryRefused
        unknown = sorted(set(n for n in names if n) - self.columns(table))
        if unknown:
            raise QueryRefused(f"{table} has no field {', '.join(unknown)} (fake instance)")

    @staticmethod
    def _serialize(row: dict, fields: list[str], display_value) -> dict:
        names = fields or list(row)
        out = {}
        for f in names:
            value, display = _parts(row.get(f))
            out[f] = ({"value": value, "display_value": display} if display_value == "all"
                      else display if display_value else value)
        return out

    def now_query(self, table, query="", fields="", limit=50, display_value=False):
        self.calls.append(("now_query", {"table": table, "query": query, "fields": fields,
                                         "limit": limit, "display_value": display_value}))
        clauses, order, named = parse(query)
        names = [f.strip() for f in fields.split(",") if f.strip()]
        self._check(table, named + names)
        rows = [r for r in self._rows(table) if all(any(c(r) for c in ors) for ors in clauses)]
        for field, desc in reversed(order):
            rows.sort(key=lambda r: _parts(r.get(field))[0], reverse=desc)
        shown = [self._serialize(r, names, display_value) for r in rows[:limit]]
        return {"count": len(shown), "truncated": len(rows) > limit, "records": shown}

    def now_get(self, table, sys_id, fields="", display_value=False):
        self.calls.append(("now_get", {"table": table, "sys_id": sys_id, "fields": fields,
                                       "display_value": display_value}))
        names = [f.strip() for f in fields.split(",") if f.strip()]
        self._check(table, names)
        for r in self._rows(table):
            if _parts(r.get("sys_id"))[0] == sys_id:
                return self._serialize(r, names, display_value)
        raise ValueError(f"No {table} record with sys_id {sys_id!r}.")

    def queries(self, table: str) -> list[dict]:
        return [kw for verb, kw in self.calls if verb == "now_query" and kw["table"] == table]

    def install(self, monkeypatch) -> "FakeInstance":
        from now_data import verbs
        monkeypatch.setattr(verbs, "now_query", self.now_query)
        monkeypatch.setattr(verbs, "now_get", self.now_get)
        return self
