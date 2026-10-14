# tests/test_tools.py — the registry, the menu, and execute_tool as the one gate.

import pytest

from rio import config
from rio.tools import MAX_RESULT_CHARS, execute_tool, is_write, register, schemas
from rio.tools.errors import ToolError


def _preview(**kwargs):
    return "would change things"


def test_writes_must_be_declared_and_a_write_needs_a_preview():
    with pytest.raises(TypeError):
        register("t", group="g")(lambda: None)          # no writes=...: refused at import
    with pytest.raises(ValueError, match="preview"):
        register("w", group="g", writes=True)(lambda: None)


def test_a_duplicate_name_from_another_function_is_refused():
    @register("dup", group="g", writes=False)
    def one():
        """One."""

    def two():
        """Two."""

    with pytest.raises(ValueError, match="duplicate"):
        register("dup", group="g", writes=False)(two)


def test_the_menu_types_each_argument_and_carries_its_description():
    @register("lookup", group="sn", writes=False)
    def lookup(table: str, limit: int = 10, values: dict | None = None, exact: bool = False) -> dict:
        """Look something up.

        Args:
          table: table name, e.g. incident
          limit: maximum rows
        """

    (fn,) = [s["function"] for s in schemas()]
    assert fn["name"] == "lookup" and fn["description"] == "Look something up."
    props = fn["parameters"]["properties"]
    assert props["table"] == {"type": "string", "description": "table name, e.g. incident"}
    assert props["limit"] == {"type": "integer", "description": "maximum rows"}
    assert props["exact"] == {"type": "boolean"}
    assert fn["parameters"]["required"] == ["table"]


def test_read_only_takes_writes_off_the_menu(monkeypatch):
    register("r", group="sn", writes=False)(lambda: "ok")
    register("w", group="sn", writes=True, preview=_preview)(lambda: "ok")
    assert {s["function"]["name"] for s in schemas()} == {"r", "w"}
    monkeypatch.setattr(config, "READ_ONLY", True)
    assert {s["function"]["name"] for s in schemas()} == {"r"}


def test_a_write_never_runs_without_approval_even_if_a_caller_forgets_to_check():
    def write(**kwargs):
        raise AssertionError("the write reached the instance")

    register("w", group="sn", writes=True, preview=_preview)(write)
    assert is_write("w")
    out = execute_tool("w", {})
    assert out.startswith("Tool error (w): a write runs only from an approved card")


def test_read_only_refuses_a_write_even_when_approved(monkeypatch):
    def write():
        raise AssertionError("the write reached the instance")

    register("w", group="sn", writes=True, preview=_preview)(write)
    monkeypatch.setattr(config, "READ_ONLY", True)
    assert "read-only" in execute_tool("w", {}, approved=True)


def test_execute_tool_turns_every_failure_into_text_and_tells_refusal_from_crash():
    @register("refuses", group="g", writes=False)
    def refuses():
        raise ToolError("no such table 'incidnet'; did you mean incident?")

    @register("crashes", group="g", writes=False)
    def crashes():
        raise TypeError("internal bug")

    @register("takes_table", group="g", writes=False)
    def takes_table(table: str):
        return table

    assert execute_tool("nope", {}).startswith("Tool error: unknown tool 'nope'")
    assert "must be a JSON object" in execute_tool("takes_table", ["incident"])
    assert execute_tool("refuses", {}) == "Tool error (refuses): no such table 'incidnet'; did you mean incident?"
    # a TypeError INSIDE a tool is a crash, not the model's bad arguments
    assert execute_tool("crashes", {}) == "Tool error (crashes): unexpected TypeError: internal bug"
    assert execute_tool("takes_table", {"tabel": "incident"}).startswith("Tool error (takes_table): bad arguments")


def test_results_are_json_and_capped_for_the_context_window():
    register("small", group="g", writes=False)(lambda: {"count": 1, "records": [{"number": "INC1"}]})
    register("big", group="g", writes=False)(lambda: "x" * (MAX_RESULT_CHARS * 2))
    assert execute_tool("small", {}) == '{"count": 1, "records": [{"number": "INC1"}]}'
    big = execute_tool("big", {})
    assert len(big) < MAX_RESULT_CHARS + 200 and "[truncated:" in big
