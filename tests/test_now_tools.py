# tests/test_now_tools.py — the now-* verbs as rio tools (#5), against fake verbs: no instance.
#
# The now-mcp verbs are replaced by recorders, so what is checked is rio's side of the seam: which
# tools load when, that rio's credentials reach the library, and that now_update's card previews
# with confirmed=False and only the approved card writes with confirmed=True.

import pytest

from rio import approval, config
from rio.tools import TOOLS, execute_tool, now, schemas

now_data_verbs = pytest.importorskip("now_data.verbs")
now_data_client = pytest.importorskip("now_data.client")
now_config_verbs = pytest.importorskip("now_config.verbs")


@pytest.fixture
def fake_verbs(monkeypatch):
    calls = []

    def rec(name, result):
        def fn(**kwargs):
            calls.append((name, kwargs))
            if isinstance(result, Exception):
                raise result
            return result
        return fn

    monkeypatch.setattr(now_data_verbs, "now_query", rec("now_query", {"count": 0, "truncated": False, "records": []}))
    monkeypatch.setattr(now_data_verbs, "now_update", rec("now_update", {
        "action": "preview", "changes": [{"field": "state", "current": "1", "proposed": "2"}],
        "no_ops": [], "unknown_fields": ["bogus"]}))
    monkeypatch.setattr(now_data_verbs, "now_aggregate", rec("now_aggregate", NotImplementedError("needs GlideAggregate")))
    monkeypatch.setattr(now_data_client, "configure", lambda *a: calls.append(("configure", a)))
    monkeypatch.setattr(now_config_verbs, "use_snapshot", rec("use_snapshot", None))
    monkeypatch.setattr(now_config_verbs, "now_role_exists", rec("now_role_exists", {"exists": True}))
    monkeypatch.setattr(now_config_verbs, "now_field_exists", rec("now_field_exists", {"exists": True, "inherited_from": "task"}))
    return calls


@pytest.fixture
def with_aggregate(monkeypatch):
    monkeypatch.setattr(now, "_has_glide_aggregate", lambda: True)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(config, "SN_INSTANCE", "dev1")
    monkeypatch.setattr(config, "SN_IDENTITY", "legacy")
    monkeypatch.setenv("SN_USER", "alice")
    monkeypatch.setenv("SN_PASSWORD", "pw")
    monkeypatch.setattr(config, "INSTANCE_CONFIG_ROOT", "C:/snap")


def test_rio_credentials_reach_the_library_and_the_tools_register(fake_verbs, configured, with_aggregate):
    notes = now.load()
    # rio's SN_IDENTITY pair and SN_INSTANCE as given (now-data normalises it), not now-data's env lookup
    assert ("configure", ("dev1", "alice", "pw")) in fake_verbs
    assert ("use_snapshot", {"root": "C:/snap"}) in fake_verbs
    assert set(TOOLS) == {"now_query", "now_get", "now_aggregate", "now_update",
                          "now_field_exists", "now_choice_values"}
    assert any(n.startswith("now-data tools: query") for n in notes)


def test_without_credentials_or_snapshot_nothing_is_offered(fake_verbs, monkeypatch):
    monkeypatch.setattr(config, "SN_INSTANCE", "")
    monkeypatch.setattr(config, "INSTANCE_CONFIG_ROOT", "")
    notes = now.load()
    assert TOOLS == {}
    assert any("now-data tools: off" in n for n in notes) and any("now-config tools: off" in n for n in notes)


def test_now_update_previews_dry_and_only_the_approved_card_confirms(fake_verbs, configured):
    now.load()
    args = {"table": "incident", "sys_id": "abc", "values": {"state": "2", "bogus": "x"}}
    assert execute_tool("now_update", args).startswith("Tool error (now_update): a write runs only")
    assert [c for c in fake_verbs if c[0] == "now_update"] == []

    card = approval.park("now_update", args)
    assert 'state: "1" -> "2"' in card.render() and "no such field: bogus" in card.render()
    assert fake_verbs[-1] == ("now_update", {**args, "confirmed": False})

    approval.answer(card, "n")
    assert [c[1]["confirmed"] for c in fake_verbs if c[0] == "now_update"] == [False]
    approval.answer(card, "y")
    assert fake_verbs[-1] == ("now_update", {**args, "confirmed": True})


def test_a_verb_refusal_reaches_the_model_as_a_tool_error(fake_verbs, configured, with_aggregate):
    now.load()
    assert execute_tool("now_aggregate", {"table": "incident"}) == "Tool error (now_aggregate): needs GlideAggregate"


def test_read_only_hides_and_refuses_now_update(fake_verbs, configured, monkeypatch):
    monkeypatch.setattr(config, "READ_ONLY", True)
    now.load()
    assert "now_update" not in {s["function"]["name"] for s in schemas()}
    assert "read-only" in execute_tool("now_update", {"table": "incident", "sys_id": "a", "values": {}}, approved=True)


def test_the_menu_describes_each_argument(fake_verbs, configured):
    now.load()
    q = next(s["function"] for s in schemas() if s["function"]["name"] == "now_query")
    assert q["parameters"]["required"] == ["table"]
    assert "encoded query" in q["parameters"]["properties"]["query"]["description"]


def test_now_aggregate_is_not_offered_without_glide_aggregate(fake_verbs, configured, monkeypatch):
    # Released PySNC (1.2.1, rio's pin) has no GlideAggregate; offering the tool only gets an error.
    monkeypatch.setattr(now, "_has_glide_aggregate", lambda: False)
    notes = now.load()
    assert "now_aggregate" not in TOOLS
    assert any(n.startswith("now-data tools: query, get, update") for n in notes)


def test_the_installed_pysnc_decides_aggregate():
    pysnc = pytest.importorskip("pysnc")
    assert now._has_glide_aggregate() == hasattr(pysnc.ServiceNowClient, "GlideAggregate")


def test_now_update_takes_values_as_a_json_string(fake_verbs, configured):
    # Ollama 0.23.2 drops a qwen3 tool call holding a nested object, so the menu offers a string.
    now.load()
    u = next(s["function"] for s in schemas() if s["function"]["name"] == "now_update")
    assert u["parameters"]["properties"]["values"]["type"] == "string"

    card = approval.park("now_update", {"table": "incident", "sys_id": "abc", "values": '{"state": "2"}'})
    assert isinstance(card, approval.Card)
    assert fake_verbs[-1] == ("now_update", {"table": "incident", "sys_id": "abc",
                                             "values": {"state": "2"}, "confirmed": False})
    approval.answer(card, "y")
    assert fake_verbs[-1][1]["values"] == {"state": "2"} and fake_verbs[-1][1]["confirmed"] is True


@pytest.mark.parametrize("bad", ["state=2", "[1, 2]", "", 7, "{}", {}])
def test_now_update_values_that_are_not_an_object_get_no_card(fake_verbs, configured, bad):
    now.load()
    out = approval.park("now_update", {"table": "incident", "sys_id": "abc", "values": bad})
    assert isinstance(out, str) and out.startswith("Tool error (now_update): values ")
    assert [c for c in fake_verbs if c[0] == "now_update"] == []


def test_the_number_vs_sys_id_rule_is_in_the_summary_line(fake_verbs, configured):
    # The menu shows each tool's first docstring line. With the rule only in the argument
    # description, qwen3:14b passed INC0008001 to now_get as a sys_id on 5 of 5 seeds; with it in
    # the summary line, it used now_query number=... on 5 of 5.
    now.load()
    d = {s["function"]["name"]: s["function"]["description"] for s in schemas()}
    assert "NOT for numbers" in d["now_get"] and "number=" in d["now_query"]


def test_a_capped_query_says_its_count_is_not_the_total(fake_verbs, configured, monkeypatch):
    # now-data's count is len(rows) after the limit; "how many" must not be answered from it (#9 review).
    monkeypatch.setattr(now_data_verbs, "now_query",
                        lambda **kw: {"count": 10, "truncated": True, "records": [{}] * 10})
    now.load()
    out = execute_tool("now_query", {"table": "incident", "query": "active=true"})
    assert '"total": "unknown: more than 10' in out
    d = next(s["function"]["description"] for s in schemas() if s["function"]["name"] == "now_query")
    assert "not a total" in d


def test_an_uncapped_query_has_no_total_note(fake_verbs, configured):
    now.load()
    assert "total" not in execute_tool("now_query", {"table": "incident"})
