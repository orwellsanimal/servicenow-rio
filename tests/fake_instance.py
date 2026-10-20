# tests/fake_instance.py — a ServiceNow instance in memory, behind the now-data verbs rio calls.
#
# Carried in spirit from whitcortex's fake GlideRecord clients (tests/test_2597_*, test_2643_*), but
# faked one level up: rio reads through now_data.verbs, so that is the seam. The encoded queries rio
# sends are evaluated, not just recorded, so a test proves the query selects the right rows. A query
# this fake can't evaluate fails the test instead of matching everything, because that silent widening
# is exactly what ServiceNow does with a term it can't apply (servicenow-coder lessons: "invalid
# encoded query returns all rows").
#
# Rows are dicts of field -> value, or field -> (value, display) for references and choices. A table
# query also returns the rows of the tables that extend it (`task` finds incidents and changes), with
# sys_class_name naming the row's own table, as on the instance.

from __future__ import annotations

import re

_OPERATORS = ("ISNOTEMPTY", "ISEMPTY", "NOT IN", "NOT LIKE", "STARTSWITH", "IN", "LIKE",
              "!=", ">=", "<=", "=", ">", "<")
_FIELD = re.compile(r"[a-z_][a-z0-9_.]*")

EXTENDS = {"incident": "task", "change_request": "task", "problem": "task",
           "sn_customerservice_case": "task", "cmdb_ci_computer": "cmdb_ci", "cmdb_ci_spkg": "cmdb_ci"}


def _parts(raw) -> tuple[str, str]:
    if isinstance(raw, tuple):
        return str(raw[0]), str(raw[1])
    text = "" if raw is None else str(raw)
    return text, text


def _condition(term: str):
    m = _FIELD.match(term)
    if not m:
        raise AssertionError(f"fake instance: can't read a field in query term {term!r}")
    field, rest = m.group(), term[m.end():]
    op = next((o for o in _OPERATORS if rest.startswith(o)), None)
    if op is None:
        raise AssertionError(f"fake instance: unknown operator in query term {term!r}")
    value = rest[len(op):].lower()

    def test(row) -> bool:
        raw = _parts(row.get(field))[0].lower()
        return {"ISNOTEMPTY": lambda: raw != "", "ISEMPTY": lambda: raw == "",
                "=": lambda: raw == value, "!=": lambda: raw != value,
                "IN": lambda: raw in value.split(","), "NOT IN": lambda: raw not in value.split(","),
                "LIKE": lambda: value in raw, "NOT LIKE": lambda: value not in raw,
                "STARTSWITH": lambda: raw.startswith(value),
                ">": lambda: raw > value, ">=": lambda: raw >= value,
                "<": lambda: raw < value, "<=": lambda: raw <= value}[op]()
    return test


def parse(query: str):
    """(AND of ORs, [(field, descending)]): `a^b^ORc` is a AND (b OR c), as on the instance."""
    clauses: list[list] = []
    order: list[tuple[str, bool]] = []
    for term in (query or "").split("^"):
        if not term:
            continue
        if term.startswith("ORDERBYDESC"):
            order.append((term[len("ORDERBYDESC"):], True))
        elif term.startswith("ORDERBY"):
            order.append((term[len("ORDERBY"):], False))
        elif term.startswith("OR"):     # field names are lowercase, so an uppercase OR is the operator
            if not clauses:
                raise AssertionError(f"fake instance: {query!r} starts with an OR")
            clauses[-1].append(_condition(term[2:]))
        else:
            clauses.append([_condition(term)])
    return clauses, order


class FakeInstance:
    def __init__(self, tables: dict[str, list[dict]] | None = None):
        self.tables = {t: [dict(r) for r in rows] for t, rows in (tables or {}).items()}
        self.calls: list[tuple[str, dict]] = []
        self.refuse: dict[str, Exception] = {}      # table -> what now-data raises for it

    def _rows(self, table: str) -> list[dict]:
        out = []
        for t, rows in self.tables.items():
            chain, cur = [t], t
            while cur in EXTENDS:
                cur = EXTENDS[cur]
                chain.append(cur)
            if table in chain:
                out += [{"sys_class_name": (t, t), **r} for r in rows]
        return out

    def _check(self, table: str) -> None:
        if table in self.refuse:
            raise self.refuse[table]

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
        self._check(table)
        clauses, order = parse(query)
        rows = [r for r in self._rows(table) if all(any(c(r) for c in ors) for ors in clauses)]
        for field, desc in reversed(order):
            rows.sort(key=lambda r: _parts(r.get(field))[0], reverse=desc)
        names = [f.strip() for f in fields.split(",") if f.strip()]
        shown = [self._serialize(r, names, display_value) for r in rows[:limit]]
        return {"count": len(shown), "truncated": len(rows) > limit, "records": shown}

    def now_get(self, table, sys_id, fields="", display_value=False):
        self.calls.append(("now_get", {"table": table, "sys_id": sys_id, "fields": fields,
                                       "display_value": display_value}))
        self._check(table)
        for r in self._rows(table):
            if _parts(r.get("sys_id"))[0] == sys_id:
                return self._serialize(r, [f.strip() for f in fields.split(",") if f.strip()], display_value)
        raise ValueError(f"No {table} record with sys_id {sys_id!r}.")

    def queries(self, table: str) -> list[dict]:
        return [kw for verb, kw in self.calls if verb == "now_query" and kw["table"] == table]

    def install(self, monkeypatch) -> "FakeInstance":
        from now_data import verbs
        monkeypatch.setattr(verbs, "now_query", self.now_query)
        monkeypatch.setattr(verbs, "now_get", self.now_get)
        return self
