# tests/test_fake_instance.py — the fake instance means what the instance means (PR #24 review).
#
# The workflow tests are only as good as the fake's reading of an encoded query, so its reading is
# pinned here: precedence, the empty-row rule, columns, and what it refuses to guess at.

import pytest

pytest.importorskip("now_data.query_check")

from now_data.query_check import QueryRefused  # noqa: E402

from fake_instance import FakeInstance  # noqa: E402

ROWS = {"incident": [
    {"sys_id": "1", "number": "INC1", "state": "1", "priority": "1", "short_description": "Printer down",
     "assigned_to": ("u1", "Ann"), "caller_id": "c1", "sys_created_on": "2026-10-01 00:00:00"},
    {"sys_id": "2", "number": "INC2", "state": "2", "priority": "2", "short_description": "VPN slow",
     "assigned_to": ("", ""), "caller_id": "c2", "sys_created_on": "2026-10-03 00:00:00"},
    {"sys_id": "3", "number": "INC3", "state": "10", "priority": "2", "short_description": "printer jam",
     "assigned_to": ("u2", "Bo"), "caller_id": "c1", "sys_created_on": "2026-10-02 00:00:00"},
], "change_request": [
    {"sys_id": "4", "number": "CHG1", "state": "3", "short_description": "Patch printers", "type": "normal"},
]}


@pytest.fixture
def inst():
    return FakeInstance(ROWS)


def numbers(inst, table, query, fields="number"):
    return [r["number"] for r in inst.now_query(table, query, fields, limit=50)["records"]]


def test_or_binds_to_the_term_before_it(inst):
    # state=1 AND (short_description LIKE vpn OR short_description LIKE printer)
    assert numbers(inst, "incident", "state=1^short_descriptionLIKEvpn^ORshort_descriptionLIKEprinter") == ["INC1"]
    assert numbers(inst, "incident", "short_descriptionLIKEvpn^ORshort_descriptionLIKEjam^priority=2") == ["INC2", "INC3"]


def test_not_equal_and_not_in_drop_empty_rows(inst):
    """servicenow-coder lessons, measured: `!=` excludes null. 'Not Ann's' doesn't include unassigned."""
    assert numbers(inst, "incident", "assigned_to!=u1") == ["INC3"]
    assert numbers(inst, "incident", "assigned_toNOT INu1") == ["INC3"]
    assert numbers(inst, "incident", "assigned_toISEMPTY") == ["INC2"]


def test_equality_is_case_sensitive_and_like_is_not(inst):
    assert numbers(inst, "incident", "number=inc1") == []
    assert numbers(inst, "incident", "short_descriptionLIKEPRINTER") == ["INC1", "INC3"]


def test_numbers_compare_as_numbers(inst):
    assert numbers(inst, "incident", "state>=7") == ["INC3"]       # "10" >= "7" as text is False


def test_order_by_more_than_one_key(inst):
    assert numbers(inst, "incident", "ORDERBYDESCpriority^ORDERBYsys_created_on") == ["INC3", "INC2", "INC1"]


def test_a_parent_query_sees_its_children_but_not_their_fields(inst):
    assert numbers(inst, "task", "short_descriptionLIKEprint") == ["INC1", "INC3", "CHG1"]
    with pytest.raises(QueryRefused, match="caller_id"):
        inst.now_query("task", "caller_id=c1")                    # caller_id is incident's own
    with pytest.raises(QueryRefused, match="type"):
        inst.now_query("task", "", "number,type")


@pytest.mark.parametrize("query", ["asigned_toISEMPTY", "asigned_to=u1", "ORDERBYnope"])
def test_an_unknown_field_is_refused_not_widened(inst, query):
    with pytest.raises(QueryRefused):
        inst.now_query("incident", query)


@pytest.mark.parametrize("query", [
    "^ORstate=1", "state=1^NQstate=2", "stateINSTANCEOFtask", "assigned_to=javascript:gs.getUserID()",
    "assigned_to.name=Ann", "state~1", "stateEQUALS1"])
def test_what_it_cannot_evaluate_fails_the_test(inst, query):
    with pytest.raises(AssertionError, match="fake instance"):
        inst.now_query("incident", query)


def test_display_falls_back_to_the_value_as_pysnc_does(inst):
    inst.tables["incident"][0]["assigned_to"] = ("u9", "")
    rec = inst.now_query("incident", "number=INC1", "assigned_to", display_value="all")["records"][0]
    assert rec["assigned_to"] == {"value": "u9", "display_value": "u9"}


def test_every_negative_and_comparison_drops_empty_rows(inst):
    """NULL matches no comparison on the instance (PR #24 round 2): "older than X" or "P1-P2"
    must not pick up rows without the field."""
    inst.tables["incident"].append({"sys_id": "5", "number": "INC5", "short_description": ""})
    assert numbers(inst, "incident", "short_descriptionNOT LIKEvpn") == ["INC1", "INC3"]
    assert numbers(inst, "incident", "priority<3") == ["INC1", "INC2", "INC3"]
    assert numbers(inst, "incident", "sys_created_on<=2026-10-02 00:00:00") == ["INC1", "INC3"]


def test_in_is_case_sensitive(inst):
    assert numbers(inst, "incident", "numberINinc1,INC2") == ["INC2"]


def test_order_by_a_number_is_numeric_and_by_a_reference_is_refused(inst):
    assert numbers(inst, "incident", "ORDERBYDESCstate") == ["INC3", "INC2", "INC1"]   # 10 > 2 > 1
    with pytest.raises(AssertionError, match="orders by 'assigned_to'"):
        inst.now_query("incident", "ORDERBYassigned_to")


def test_an_unknown_table_fails_the_test(inst):
    with pytest.raises(AssertionError, match="no table 'incidnet'"):
        inst.now_query("incidnet", "sys_id=1")
