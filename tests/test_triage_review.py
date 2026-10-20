# tests/test_triage_review.py — what the independent review of PR #25 found, pinned (#1).
#
# One test per finding or surviving mutation, against the same fake instance as test_triage.py.

import json
import re

import pytest

from rio import config
from rio.llm import Reply
from rio.tools import TOOLS, execute_tool, register
from rio.tools.errors import ToolError

pytest.importorskip("now_data.verbs")

from fake_instance import FakeInstance  # noqa: E402
from rio import __main__ as main, approval, triage as g  # noqa: E402
from rio.servicenow import query_safe  # noqa: E402
from test_triage import (CALLER, CAPS, CI_DNS, GRP_HW, GRP_NET, INC_ID, _cands, _incident, _picks,  # noqa: E402
                         _proposed, _rec, inst, loaded, tables)

__all__ = ["inst", "loaded"]           # the fixtures, re-used


def _model(picks):
    return lambda messages, **kw: Reply(content=json.dumps(picks))


def _no_chat(*a, **k):
    pytest.fail("a chat turn ran for an operator command")


# ── 1. Ticket text never puts a script into a query ──────────────────────────

@pytest.mark.parametrize("text", ["javascript:gs.getUserName()// printer is down",
                                  "JavaScript :gs.log(1) dns01", "see javascript:x"])
def test_a_script_in_the_ticket_never_reaches_name_in(inst, text):
    """PR #25 review, blocker: the first sequence of `javascript:gs.getUserName()// printer is down`
    was the start of a `nameIN` list, which the instance would have run."""
    assert not any("javascript" in n.lower() and ":" in n for n in g.name_candidates(text))
    g.ci_candidates(_rec(short_description=text, caller_id=""))      # the fake fails on a script value
    # the word alone is harmless in a list; a script prefix (`javascript` then `:`) never gets there
    assert not any(re.search(r"javascript\s*:", q["query"], re.I) for q in inst.queries("cmdb_ci"))


@pytest.mark.parametrize("value, ok", [("dns01", True), ("javascript:x", False), ("a^b", False),
                                       ("a,b", True), ("two\nlines", False), ("JAVASCRIPT : y", False)])
def test_query_safe(value, ok):
    assert query_safe(value) is ok
    assert query_safe("a,b", in_list=True) is False


# ── 6/7. Short names first, long tokens skipped, id lists chunked ────────────

def test_a_long_ticket_still_looks_up_its_one_word_names(inst, monkeypatch):
    # longest first, 297 four-word and 298 three-word sequences would fill 350 before any single word
    monkeypatch.setattr(g, "MAX_NAMES", 350)
    words = " ".join(f"word{i:04d}" for i in range(300)) + " dns01"
    cis, capped = g.ci_candidates(_rec(short_description=words, caller_id=""))
    assert capped and [c["name"] for c in cis] == ["dns01"]


def test_a_token_longer_than_a_ci_name_is_not_looked_up(inst):
    blob = "x" * 3000
    names = g.name_candidates(f"token {blob} dns01")
    assert all(len(n) <= g.CI_NAME_MAX for n in names) and "dns01" in names


def test_group_id_lists_are_chunked(inst, monkeypatch):
    monkeypatch.setattr(g, "ID_CHUNK", 1)
    assert [x["name"] for x in g.group_candidates()] == ["Hardware", "Network"]
    reads = [q for q in inst.queries("sys_user_group")]
    assert len(reads) == 4 and all(q["query"].count(",") == 0 for q in reads)
    assert [x["name"] for x in g.groups_the_assignee_is_in(g.group_candidates(), "5" * 32)] == ["Hardware"]


# ── 8. The prompt fits num_ctx, or there is no call ──────────────────────────

def test_too_many_candidates_for_num_ctx_is_refused_before_the_model(inst, monkeypatch):
    monkeypatch.setattr(config, "NUM_CTX", 512)
    with pytest.raises(ToolError, match="the candidates are too many for num_ctx 512"):
        g.classify(_rec(), _cands(), chat=lambda *a, **k: pytest.fail("the model was called"))


def test_groups_are_capped_most_used_first(inst, monkeypatch):
    monkeypatch.setattr(g, "MAX_GROUPS", 1)
    for n in (6, 7):                                                    # Network 3 uses, Hardware 2
        inst.tables["incident"].append({"sys_id": f"h{n}", "number": f"INC000000{n}",
                                        "assignment_group": (GRP_NET, "Network"), "sys_created_on": f"2026-10-0{n}"})
    assert [x["name"] for x in g.group_candidates()] == ["Network"]


# ── 2/3. The card is bound to the proposal it showed, on this record and instance ──

def test_a_proposal_replaced_while_the_card_waits_is_not_written(loaded):
    _proposed(loaded)
    _, card = main.step("apply triage INC0000016", [], None, CAPS, run=_no_chat)
    g.run("INC0000016", chat=_model(_picks(category="network/dns")))     # another rio, same state dir
    shown, _ = main.step("y", [], card, CAPS, run=_no_chat)
    assert "is not the one this card was made for" in shown[0] and loaded.writes() == []


def test_a_number_that_is_now_another_record_is_refused(loaded):
    _proposed(loaded)
    _incident(loaded)["sys_id"] = ("f" * 32, "f" * 32)
    with pytest.raises(ToolError, match="INC0000016 is now another record"):
        g.apply("INC0000016", g.pending_id("INC0000016"))
    assert loaded.writes() == []


def test_a_proposal_from_another_instance_is_refused(loaded, monkeypatch):
    _proposed(loaded)
    monkeypatch.setattr(config, "sn_instance_url", lambda: "https://dev2.service-now.com")
    with pytest.raises(ToolError, match="was made on https://dev1.service-now.com, not https://dev2"):
        g.apply("INC0000016", g.pending_id("INC0000016"))


def test_an_unreadable_mod_count_is_a_change_not_a_match(loaded):
    _proposed(loaded)
    saved = g.load_proposal("INC0000016")
    saved["sys_mod_count"] = ""
    g._path("INC0000016").write_text(json.dumps(saved), encoding="utf-8")
    _incident(loaded)["sys_mod_count"] = ("", "")
    with pytest.raises(ToolError, match="has changed since the proposal"):
        g.apply("INC0000016", saved["id"])


def test_a_failed_rerun_leaves_no_old_proposal_to_apply(loaded):
    _proposed(loaded)
    with pytest.raises(ToolError):
        g.run("INC0000016", chat=lambda *a, **k: Reply(ok=False, error="timeout"))
    assert g.load_proposal("INC0000016") is None


# ── 4/5. Failures that hide what landed, and fields that weren't sent ────────

def test_a_failed_apply_keeps_the_proposal(loaded):
    _proposed(loaded)
    loaded.keep = {"category", "subcategory", "cmdb_ci", "assignment_group"}
    with pytest.raises(ToolError):
        g.apply("INC0000016", g.pending_id("INC0000016"))
    assert g.load_proposal("INC0000016") is not None


def test_a_dropped_connection_on_the_note_still_says_the_fields_landed(loaded, monkeypatch):
    import requests
    _proposed(loaded)
    real = loaded.now_update

    def drop(table, sys_id, values, confirmed=False):
        if confirmed and "work_notes" in values:
            raise requests.ConnectionError("connection reset by peer")
        return real(table, sys_id, values, confirmed)
    from now_data import verbs
    monkeypatch.setattr(verbs, "now_update", drop)
    with pytest.raises(ToolError) as caught:
        g.apply("INC0000016", g.pending_id("INC0000016"))
    assert ("Applied to INC0000016 and read back (assignment_group, category, cmdb_ci, subcategory), but the "
            "'applied' work note may not have been written (ConnectionError: connection reset by peer)") in str(caught.value)


def test_a_field_this_account_cannot_see_is_on_the_card_and_fails_the_apply(loaded):
    _proposed(loaded)
    del _incident(loaded)["cmdb_ci"]                                    # a field read ACL hides it
    _, card = main.step("apply triage INC0000016", [], None, CAPS, run=_no_chat)
    assert "not written, not a field this account can read: cmdb_ci" in card.preview
    shown, _ = main.step("y", [], card, CAPS, run=_no_chat)
    assert "cmdb_ci (not a field this account can see; not sent)" in shown[0]
    assert "No work note was added, and the proposal is kept." in shown[0]


# ── Surviving mutations from the review ─────────────────────────────────────

def test_an_inactive_group_is_not_offered(inst):
    inst.tables["sys_user_group"][1]["active"] = "false"                 # Network
    assert [x["name"] for x in g.group_candidates()] == ["Hardware"]


def test_a_group_whose_type_this_account_cannot_read_is_not_offered(inst):
    inst.tables["sys_user_group"][0]["type"] = ("t" * 32, "")           # display falls back to the value
    assert [x["name"] for x in g.group_candidates()] == ["Network"]


def test_a_retired_ci_of_the_caller_is_not_offered(inst):
    inst.tables["cmdb_ci"][0]["operational_status"] = "6"               # ERA02, the caller's
    cis, _ = g.ci_candidates(_rec(caller_id=CALLER))
    assert "ERA02" not in [c["name"] for c in cis]


def test_the_category_cap_flag_is_set_at_the_limit(inst, monkeypatch):
    monkeypatch.setattr(g, "CHOICE_LIMIT", 2)
    assert g.category_pairs()[1] is True


def test_operator_only_is_refused_for_a_read():
    with pytest.raises(ValueError, match="operator_only is for writes"):
        register("x_read", group="t", writes=False, operator_only=True)(lambda: "")


def test_the_whole_word_check_decides_what_name_in_matched(inst, monkeypatch):
    """On a real instance `nameIN` follows the collation (accent-insensitive): `dns01` may match a CI
    named `dnś01`. The whole-word check against the text is what keeps it out."""
    real = g.rows

    def collating(table, query, fields, limit):
        out = real(table, query, fields, limit)
        if "nameIN" in query:
            out.append({"sys_id": {"value": "z" * 32, "display": "z" * 32},
                        "name": {"value": "dnś01", "display": "dnś01"}})
        return out
    monkeypatch.setattr(g, "rows", collating)
    cis, _ = g.ci_candidates(_rec(short_description="dns01 is down", caller_id=""))
    assert "dnś01" not in [c["name"] for c in cis] and "dns01" in [c["name"] for c in cis]


# ── Nits ────────────────────────────────────────────────────────────────────

def test_a_choice_value_with_a_slash_is_written_as_its_own_pair(inst):
    inst.tables["sys_choice"] += [
        {"sys_id": "c9", "name": "incident", "element": "category", "inactive": "false", "language": "en",
         "value": "a/v", "label": "A/V", "dependent_value": ""}]
    proposal = g.validate(_rec(), {**_cands(), "categories": g.category_pairs()[0]},
                          _picks(category="a/v", evidence="DNS"))
    assert g.update_values(proposal) == {"category": "a/v"}


def test_the_applied_note_says_applied_and_instance_text_cannot_add_lines(inst):
    proposal = g.validate(_rec(), _cands(), _picks(category="network/dns", group=GRP_NET))
    proposal["assignment_group"]["label"] = "Net\n- CI: forged line"
    note = g.render_note("INC0000016", proposal, applied=True)
    assert "[applied]" in note and "[proposed]" not in note
    assert "- CI: forged line" not in note.split("\n")


def test_an_unknown_tool_error_does_not_name_operator_only_tools(loaded):
    assert "sn_triage_apply" not in execute_tool("nope", {})
    assert "sn_triage" in execute_tool("nope", {})


def test_read_only_is_said_before_operator_only(loaded, monkeypatch):
    monkeypatch.setattr(config, "READ_ONLY", True)
    assert "rio is read-only here" in approval.park("sn_triage_apply", {"number": "INC0000016"})
