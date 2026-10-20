# tests/test_resolve.py — resolve an incident that already carries its resolution, and tag it from
# its own fields (#1, carried from whitcortex #2627).
#
# The worked example is INC0010049 on dev442835 (2026-10-11): close code "Known error" and close
# notes already written, state New, CI GT-AX11000, Network / Router, bound to Alert0010006 (source
# WhitCortex-HostLogs). Resolving it should close the alert (evt_mgmt.incident_closes_alert = true).
# sn_resolve is a write: a card shows plan() and `y` runs exactly that call (ADR-0003).

import pytest

from rio import config
from rio.llm import Reply
from rio.tools import TOOLS, execute_tool, register

pytest.importorskip("now_data.verbs")

from fake_instance import FakeInstance  # noqa: E402
from rio import __main__ as main, approval, resolve as g, tags, turn  # noqa: E402
from rio.tools import sn  # noqa: E402
from rio.tools.errors import ToolError  # noqa: E402

INC = "a" * 32
CAPS = frozenset({"completion", "tools"})


def tables(**incident):
    row = {"sys_id": INC, "number": "INC0010049", "state": ("1", "New"),
           "short_description": "Router reboots", "description": "", "close_code": ("Known error", "Known error"),
           "close_notes": "CAUSE: Not established. ...", "cmdb_ci": ("c" * 32, "GT-AX11000"),
           "category": ("network", "Network"), "subcategory": ("router", "Router"),
           "assignment_group": ("", ""), "assigned_to": ("", ""), "caller_id": ("", ""), "sys_mod_count": "3"}
    row.update(incident)
    return {
        "incident": [row],
        "em_alert": [{"sys_id": "e" * 32, "incident": INC, "number": "Alert0010006", "state": "Open",
                      "source": "WhitCortex-HostLogs"}],
        "label": [{"sys_id": "c1" * 16, "name": "Router", "viewable_by": "everyone", "type": "standard",
                   "active": "true", "sys_created_on": "2026-01-01 00:00:00"}],
        "label_entry": [],
    }


def _load(monkeypatch, **over) -> FakeInstance:
    monkeypatch.setattr(config, "sn_instance_url", lambda: "https://dev1.service-now.com")
    fake = FakeInstance(tables(**over)).install(monkeypatch)
    monkeypatch.setattr(g, "_SHOWN", {})          # what the cards showed, per test

    @register("now_query", group="servicenow", writes=False)
    def now_query(table: str) -> dict:
        """stand-in for the real now_query"""
        return {}
    sn.load()
    return fake


@pytest.fixture
def inst(monkeypatch):
    return _load(monkeypatch)


def _incident(inst):
    return inst.tables["incident"][0]


def _card_and_yes(number="INC0010049"):
    card = approval.park("sn_resolve", {"number": number})
    assert isinstance(card, approval.Card), card
    return card, approval.answer(card, "y")


def test_resolves_and_tags_the_worked_example(inst):
    inst.tables["em_alert"][0]["state"] = "Open"
    card, result = _card_and_yes()
    assert "resolve INC0010049: state New -> Resolved, with its recorded close code 'Known error'" in card.preview
    assert "tags first: GT-AX11000, Router, WhitCortex-HostLogs, Known error" in card.preview
    assert "new tags created: GT-AX11000, WhitCortex-HostLogs, Known error" in card.preview
    assert "alert: Alert0010006 (Open)" in card.preview
    assert result.split("\n") == [
        "INC0010049 is now Resolved (Known error).",
        "Tagged INC0010049: GT-AX11000, Router, WhitCortex-HostLogs, Known error.",
        "Alert Alert0010006: Open before, Open now."]
    assert _incident(inst)["state"][0] == "6"


def test_tags_go_first_then_the_state(inst):
    _card_and_yes()
    order = [verb for verb, kw in inst.calls if kw.get("confirmed")]
    assert order[-1] == "now_update" and set(order[:-1]) == {"now_insert"}


def test_an_existing_tag_is_reused_and_entries_look_like_the_ui_makes_them(inst):
    _card_and_yes()
    labels = [r for r in inst.tables["label"] if r["name"] == "Router"]
    assert len(labels) == 1                              # reused, not duplicated
    entry = next(e for e in inst.tables["label_entry"] if e["label"] == "c1" * 16)
    assert entry == {**entry, "table": "incident", "table_key": INC, "title": "Incident - INC0010049",
                     "url": f"incident.do?sys_id={INC}&sysparm_view="}


def test_the_oldest_same_named_tag_is_the_one_reused(inst):
    inst.tables["label"].append({"sys_id": "c2" * 16, "name": "Router", "viewable_by": "everyone",
                                 "type": "standard", "active": "true", "sys_created_on": "2026-06-01 00:00:00"})
    _card_and_yes()
    assert {e["label"] for e in inst.tables["label_entry"] if e["title"] == "Incident - INC0010049"} >= {"c1" * 16}
    assert not any(e["label"] == "c2" * 16 for e in inst.tables["label_entry"])


def test_derive_tags_falls_back_to_category_and_skips_blanks(inst):
    rec = {"cmdb_ci": {"display": ""}, "subcategory": {"display": ""}, "category": {"display": "Network"},
           "close_code": {"display": "Known error"}}
    assert g.derive_tags(rec, None) == ["Network", "Known error"]
    assert g.derive_tags(rec, {"source": "  "}) == ["Network", "Known error"]


@pytest.mark.parametrize("over, why", [
    ({"state": ("6", "Resolved")}, "it is already Resolved"),
    ({"state": ("7", "Closed")}, "it is already Closed"),
    ({"close_code": ("", "")}, "it has no close code;"),
    ({"close_notes": " "}, "it has no close notes;"),
    ({"close_code": ("", ""), "close_notes": ""}, "it has no close code and no close notes;"),
])
def test_refuses_with_no_card_and_writes_nothing(monkeypatch, over, why):
    fake = _load(monkeypatch, **over)
    out = approval.park("sn_resolve", {"number": "INC0010049"})
    assert isinstance(out, str) and f"INC0010049 was not resolved: {why}" in out
    assert fake.writes() == [] and fake.writes("now_insert") == []


def test_unknown_incident_refuses(inst):
    out = approval.park("sn_resolve", {"number": "INC9999999"})
    assert out == "Tool error (sn_resolve): Incident INC9999999 not found."


def test_a_state_the_instance_kept_says_the_tags_landed(inst):
    """whitcortex #2627: a business rule or ACL can keep the state though the save answered 200;
    now-data 0.6.0 reads it back (now-mcp#3)."""
    inst.keep = {"state"}
    _, result = _card_and_yes()
    assert result.startswith("Tool error (sn_resolve): Tagged INC0010049: GT-AX11000, Router, "
                             "WhitCortex-HostLogs, Known error. But it was NOT resolved: the instance kept '1'.")
    assert approval.outcome(result) == "approved, failed"


def test_tagging_twice_adds_nothing_the_second_time(inst):
    assert tags.tag("incident", INC, "INC0010049", ["Router", "Known error"]) == \
        "Tagged INC0010049: Router, Known error."
    assert tags.tag("incident", INC, "INC0010049", ["Router", "Known error"]) == \
        "No new tags on INC0010049; already tagged: Router, Known error."


def test_an_insert_the_instance_did_not_keep_is_a_failure_that_names_the_row(inst):
    """whitcortex #2611 / PR #2650: a created row is named, so it isn't created twice."""
    inst.insert_override = {"label": {"active": "false"}}
    with pytest.raises(ToolError) as caught:
        tags.tag("incident", INC, "INC0010049", ["Brand new"])
    assert "Tag 'Brand new': created as label " in str(caught.value)
    assert "tagging INC0010049 stopped at 'Brand new'" in str(caught.value)
    assert "active read back 'false', not 'true'" in str(caught.value) and "a retry can create a second one" in str(caught.value)


def test_a_derived_name_with_a_caret_is_dropped_and_a_comma_stays_one_tag(inst):
    """whitcortex PR #2628: a derived name may hold a comma, and stays one tag. A caret is dropped
    (PR #26 review): whether `^^` is one literal caret for `=` is unmeasured, and a trailing one
    made `name=Edge^^^viewable_by…`."""
    _incident(inst)["cmdb_ci"] = ("c" * 32, "Acme ^ R1")
    _incident(inst)["subcategory"] = ("router", "Router, edge")
    _, result = _card_and_yes()
    assert "Tagged INC0010049: Router, edge, WhitCortex-HostLogs, Known error." in result
    assert not any("^^" in q["query"] or "Acme" in q["query"] for q in inst.queries("label"))


def test_a_change_while_the_card_waits_is_refused_at_yes(inst):
    card = approval.park("sn_resolve", {"number": "INC0010049"})
    _incident(inst)["sys_mod_count"] = "4"
    result = approval.answer(card, "y")
    assert "INC0010049 changed after the card was shown (sys_mod_count 3 -> 4); nothing was changed" in result
    assert inst.writes() == [] and inst.writes("now_insert") == []


def test_no_alert_table_is_no_alert(inst):
    from now_data.query_check import FieldsUnreadable
    inst.refuse["em_alert"] = FieldsUnreadable("no em_alert")
    card, result = _card_and_yes()
    assert "no Event Management alert is bound to it" in card.preview
    assert result.endswith("No Event Management alert is bound to it.")


def test_the_command_parks_the_card_and_read_only_refuses_it(inst, monkeypatch):
    shown, card = main.step("resolve INC0010049", [], None, CAPS, run=lambda *a, **k: pytest.fail("chat"))
    assert card is not None and card.tool == "sn_resolve" and "APPROVAL NEEDED: sn_resolve" in shown[0]
    assert inst.writes() == []
    monkeypatch.setattr(config, "READ_ONLY", True)
    shown, card = main.step("resolve INC0010049", [], None, CAPS, run=lambda *a, **k: pytest.fail("chat"))
    assert card is None and "rio is read-only here (RIO_READ_ONLY)" in shown[0]


def test_a_chat_turn_parks_sn_resolve_at_the_card(inst):
    """The model may ask for it; the card is what runs it (whitcortex ADR-0129/0130)."""
    def chat(history, **kw):
        return Reply(tool_calls=[{"function": {"name": "sn_resolve", "arguments": {"number": "INC0010049"}}}])
    t = turn.run([{"role": "user", "content": "resolve INC0010049"}], chat=chat, model_caps=CAPS)
    assert t.card is not None and t.card.tool == "sn_resolve" and inst.writes() == []


def test_an_unapproved_dispatch_runs_nothing(inst):
    assert "runs only from an approved card" in execute_tool("sn_resolve", {"number": "INC0010049"})
    assert inst.writes() == [] and inst.writes("now_insert") == []


def test_an_approval_without_a_shown_card_runs_nothing(inst):
    """The changed-since check needs the count the card was shown with; with none, there is no
    baseline, so nothing runs (a restart drops the card, ADR-0003)."""
    out = execute_tool("sn_resolve", {"number": "INC0010049"}, approved=True)
    assert out == "Tool error (sn_resolve): no card was shown for resolving INC0010049; ask again"
    assert inst.writes() == [] and inst.writes("now_insert") == []


def test_a_tag_name_is_cut_to_what_label_name_holds_and_a_script_is_dropped():
    """label.name holds 40 characters (UI Meta, dev442835); a CI name is someone else's text."""
    long = "A very long configuration item name that goes on"
    assert tags.names([long, "javascript:gs.log(1)", "Router", "Router "]) == [long[:40].strip(), "Router"]


def test_a_number_that_is_now_another_record_is_refused_at_yes(inst):
    card = approval.park("sn_resolve", {"number": "INC0010049"})
    _incident(inst)["sys_id"] = "f" * 32
    for t in ("em_alert",):
        inst.tables[t][0]["incident"] = "f" * 32
    result = approval.answer(card, "y")
    assert "changed after the card was shown" in result and inst.writes() == [] and inst.writes("now_insert") == []


def test_a_dropped_connection_on_the_state_says_the_tags_landed(inst, monkeypatch):
    import requests
    real = inst.now_update

    def drop(table, sys_id, values, confirmed=False):
        if confirmed:
            raise requests.ConnectionError("reset")
        return real(table, sys_id, values, confirmed)
    from now_data import verbs
    monkeypatch.setattr(verbs, "now_update", drop)
    _, result = _card_and_yes()
    assert result.startswith("Tool error (sn_resolve): Tagged INC0010049: ")
    assert "The resolve may not have been saved (ConnectionError: reset); check INC0010049's state" in result
