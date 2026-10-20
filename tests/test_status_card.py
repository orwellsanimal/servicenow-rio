# tests/test_status_card.py — any task record's status as a card (#1, carried from whitcortex #2643).
#
# The number, type · state · priority, the short description, assignee and group, the latest work
# note or comment with who and when, and the link. Measured on dev442835 (2026-10-13): one `task`
# query resolves INC/CS/CHG/PRB/RITM/REQ and the task classes, with `sys_class_name` naming the
# record's own table; case comments arrive as `[code]<p>…</p>[/code]`.

import pytest

from rio import config
from rio.tools import TOOLS, execute_tool, register, schemas
from rio.tools.errors import ToolError

pytest.importorskip("now_data.verbs")

from fake_instance import FakeInstance  # noqa: E402
from rio import servicenow  # noqa: E402
from rio.tools import sn  # noqa: E402

INC = "85071a1347c12200e0ef563dbb9a71c1"
CHG = "70ad699e47410200e90d87e8dee4907d"
CS = "c8d3f0a5c3123010a282a539e540dd1e"
BASE = "https://dev1.service-now.com"


def tables():
    return {
        "incident": [
            {"sys_id": INC, "number": "INC0000059", "sys_class_name": ("incident", "Incident"),
             "state": ("1", "New"), "priority": ("3", "3 - Moderate"),
             "short_description": "Unable to access team file share",
             "assigned_to": ("", ""), "assignment_group": ("", "")},
        ],
        "change_request": [
            {"sys_id": CHG, "number": "CHG0000023", "sys_class_name": ("change_request", "Change Request"),
             "state": ("3", "Closed"), "priority": ("4", "4 - Low"),
             "short_description": "Decommission server",
             "assigned_to": ("u" * 32, "ITIL User"), "assignment_group": ("g" * 32, "Network")},
        ],
        "sn_customerservice_case": [
            {"sys_id": CS, "number": "CS0000001", "sys_class_name": ("sn_customerservice_case", "Case"),
             "state": ("10", "Open"), "priority": ("4", "4 - Low"),
             "short_description": "Router keeps rebooting",
             "assigned_to": ("", ""), "assignment_group": ("", "")},
        ],
        "sys_journal_field": [
            {"sys_id": "j1" * 16, "element_id": INC, "element": "comments",
             "value": "[code]<p>Still broken &amp; urgent</p>[/code]",
             "sys_created_on": ("2026-10-11 10:00:00", "2026-10-11 06:00:00"), "sys_created_by": "rick.berzle"},
            {"sys_id": "j2" * 16, "element_id": INC, "element": "work_notes",
             "value": "Triage proposal (DRY RUN):\n  category x",
             "sys_created_on": ("2026-10-12 02:01:42", "2026-10-11 19:01:42"), "sys_created_by": "jody.whitlow"},
            {"sys_id": "j3" * 16, "element_id": INC, "element": "state", "value": "2",   # not a note: never shown
             "sys_created_on": ("2026-10-13 00:00:00", "2026-10-12 20:00:00"), "sys_created_by": "system"},
            {"sys_id": "j4" * 16, "element_id": CS, "element": "comments",
             "value": "[code]<p>Customer called back</p>[/code]",
             "sys_created_on": ("2026-10-14 09:00:00", "2026-10-14 05:00:00"), "sys_created_by": "beth.anglin"},
        ],
    }


@pytest.fixture
def inst(monkeypatch):
    monkeypatch.setattr(config, "sn_instance_url", lambda: BASE)
    return FakeInstance(tables()).install(monkeypatch)


def card(number):
    return servicenow.render_status_card(servicenow.record_status(number))


# ── The card ────────────────────────────────────────────────────────────────

def test_the_card_for_an_incident(inst):
    assert card("inc0000059").split("\n") == [
        "INC0000059 · Incident · New · 3 - Moderate",
        "Unable to access team file share",
        "Assigned: unassigned · Group: none",
        "Last work note (jody.whitlow, 2026-10-11 19:01:42): Triage proposal (DRY RUN): category x",
        f"{BASE}/incident.do?sys_id={INC}",
    ]


def test_the_card_for_a_change_uses_its_own_table_and_says_no_notes(inst):
    text = card("CHG0000023")
    assert text.startswith("CHG0000023 · Change Request · Closed · 4 - Low\n")
    assert "Assigned: ITIL User · Group: Network" in text
    assert "No work notes or comments (none, or not readable by this account)." in text
    assert text.endswith(f"{BASE}/change_request.do?sys_id={CHG}")


def test_the_card_for_a_case_shows_its_comment_without_markup(inst):
    text = card("CS0000001")
    assert text.startswith("CS0000001 · Case · Open · 4 - Low\n")
    assert "Last comment (beth.anglin, 2026-10-14 05:00:00): Customer called back" in text
    assert text.endswith(f"{BASE}/sn_customerservice_case.do?sys_id={CS}")


def test_one_read_on_task_with_values_and_display_then_the_journal(inst):
    card("INC0000059")
    task, = inst.queries("task")
    assert task["query"] == "number=INC0000059" and task["limit"] == 1 and task["display_value"] == "all"
    journal, = inst.queries("sys_journal_field")
    assert journal["query"] == (f"element_id={INC}^elementINwork_notes,comments"
                                "^ORDERBYDESCsys_created_on")
    assert journal["limit"] == servicenow.JOURNAL_FETCH


def test_the_newest_entry_wins_and_a_comment_is_labelled_as_one(inst):
    inst.tables["sys_journal_field"] = [r for r in inst.tables["sys_journal_field"] if r["element"] != "work_notes"]
    assert "Last comment (rick.berzle, 2026-10-11 06:00:00): Still broken & urgent" in card("INC0000059")


def test_a_long_entry_is_cut_at_300_characters(inst):
    inst.tables["sys_journal_field"][1]["value"] = "x" * 400
    text = card("INC0000059")
    assert "x" * 300 + "…" in text and "x" * 301 not in text


def test_same_second_entries_pick_one_deterministically(inst):
    """whitcortex PR #2645 review: sys_created_on is whole seconds, so a note and a comment from
    one action tie and the instance's order is undefined. Ties break on sys_id."""
    j = inst.tables["sys_journal_field"]
    j[0]["sys_created_on"] = ("2026-10-12 02:01:42", "2026-10-11 19:01:42")
    j[0]["sys_id"] = "zz" * 16          # sorts above j2
    assert "Last comment (rick.berzle" in card("INC0000059")
    j[0]["sys_id"] = "aa" * 16          # sorts below j2
    assert "Last work note (jody.whitlow" in card("INC0000059")


def test_instance_text_cannot_drive_the_terminal(inst):
    """The terminal's version of whitcortex PR #2645's markdown escaping: an escape sequence in a
    short description or a note would recolour, retitle or clear the operator's screen."""
    inc = inst.tables["incident"][0]
    inc["short_description"] = "Pay \x1b[2J\x1b]0;owned\x07now"
    # sys_user.name is built from profile fields a user can edit: the assignee is a real route in
    inc["assigned_to"] = ("u" * 32, "Mal\x1b[31mlory")
    inc["assignment_group"] = ("g" * 32, "Net\x9b2Jwork")
    inc["state"] = ("1", "New\x1b[8m")
    inst.tables["sys_journal_field"][1]["value"] = "see \x9b31mred\x1b[0m\r\nnext line \ud83d"
    inst.tables["sys_journal_field"][1]["sys_created_by"] = "eve\x1b[8m"
    text = card("INC0000059")
    assert not any(ord(ch) < 0x20 and ch != "\n" or 0x7f <= ord(ch) <= 0x9f for ch in text)
    assert len(text.split("\n")) == 5    # a CR/LF in a note doesn't add a line
    assert "Assigned: Mal [31mlory · Group: Net 2Jwork" in text
    text.encode("utf-8")                 # a lone surrogate would make print() raise


def test_angle_brackets_outside_the_journal_are_text_not_markup(inst):
    """PR #24 review: only journal text is HTML. An address or a comparison in a short description
    is kept; markup in a comment still goes."""
    inst.tables["incident"][0]["short_description"] = "Email from <jane.doe@example.com> bounced; CPU > 90% when n < 5"
    inst.tables["sys_journal_field"][1]["value"] = "<p>Rebooted &lt;srv1&gt;</p>"
    text = card("INC0000059")
    assert "Email from <jane.doe@example.com> bounced; CPU > 90% when n < 5" in text
    assert "): Rebooted <srv1>" in text


def test_a_link_is_built_only_from_a_table_name_and_a_sys_id(inst):
    inst.tables["incident"][0]["sys_class_name"] = ("incident.do?x=1#", "Incident")
    assert card("INC0000059").endswith("(no link: the instance returned an unexpected table name or sys_id)")


def test_an_unreadable_journal_still_gives_a_card_and_says_so(inst):
    from now_data.query_check import FieldsUnreadable
    inst.refuse["sys_journal_field"] = FieldsUnreadable("could not read the fields of 'sys_journal_field' (HTTP 403)")
    text = card("INC0000059")
    assert text.startswith("INC0000059 · Incident · New")
    assert "Work notes and comments not read: could not read the fields of 'sys_journal_field' (HTTP 403)" in text


def test_a_403_on_the_journal_rows_still_gives_a_card(inst):
    """PR #24 review: pysnc raises RoleException (a plain Exception) for a Table API 403. A
    non-admin corporate account may get exactly that on sys_journal_field."""
    from pysnc.exceptions import RoleException
    inst.refuse["sys_journal_field"] = RoleException("HTTP 403: insufficient rights")
    text = card("INC0000059")
    assert text.startswith("INC0000059 · Incident · New")
    assert ("Work notes and comments not read: the instance refused it (RoleException: HTTP 403: "
            "insufficient rights)") in text


def test_an_unknown_number_is_a_refusal(inst):
    with pytest.raises(ToolError, match=r"^Record INC9999999 not found\.$"):
        servicenow.record_status("INC9999999")


@pytest.mark.parametrize("number", ["INC0000059^ORactive=true", "INC0000059^NQactive=true",
                                    "number=INC0000059", "INC0000059,CHG0000023", "", "   ", None])
def test_a_number_that_would_change_the_query_is_refused_before_any_read(inst, number):
    """The model supplies the number, and it is pasted into `number=<it>`: a caret would widen the
    read and the first row would be shown as this record."""
    with pytest.raises(ToolError, match="is not a record number"):
        servicenow.record_status(number)
    assert inst.calls == []


# ── The tool ────────────────────────────────────────────────────────────────

def _now_data_on():
    @register("now_query", group="servicenow", writes=False)
    def now_query(table: str) -> dict:
        """stand-in for the real now_query"""
        return {}


def test_the_tool_loads_only_with_the_now_data_tools(inst):
    assert sn.load() == ["record tools: off (they read through the now-data tools, which are off)"]
    assert "sn_record_status" not in TOOLS
    _now_data_on()
    assert sn.load() == ["record tools: sn_record_status (or type a record number)"]
    assert not TOOLS["sn_record_status"].writes


def test_it_is_offered_and_runs_when_rio_is_read_only(inst, monkeypatch):
    _now_data_on()
    sn.load()
    monkeypatch.setattr(config, "READ_ONLY", True)
    assert "sn_record_status" in {s["function"]["name"] for s in schemas()}
    assert execute_tool("sn_record_status", {"number": "INC0000059"}).startswith("INC0000059 · Incident")
    assert execute_tool("sn_record_status", {"number": "INC9999999"}) == \
        "Tool error (sn_record_status): Record INC9999999 not found."
