# tests/test_triage.py — the triage dry run proposes only what the instance offers, says "not
# established" for anything else, and changes nothing (#1, carried from whitcortex #2597, #2613,
# #2614, #2631).
#
# The design these pin (whitcortex ADR-0128, measured on dev442835 2026-10-10):
#   * candidates are READ from the instance on every run: valid category/subcategory pairs, the
#     current CI + the caller's + the CIs the text names, the groups incidents actually go to;
#   * ONE local call whose JSON-schema enums ARE the candidates;
#   * code re-validates every pick and every evidence quote;
#   * rio's dry run writes nothing (rio#1 plan of record), where whitcortex wrote a work note.

import json

import pytest

from rio import config
from rio.llm import Reply
from rio.tools import TOOLS, register, schemas
from rio.tools.errors import ToolError

pytest.importorskip("now_data.verbs")

from fake_instance import FakeInstance  # noqa: E402
from rio import __main__ as main, approval, commands, triage as g  # noqa: E402
from rio.tools import sn  # noqa: E402

GRP_HW, GRP_NET, GRP_EMPTY, GRP_NOASSIGN = "1" * 32, "2" * 32, "3" * 32, "4" * 32
CI_LAPTOP, CI_DNS, CI_WIN = "a" * 32, "b" * 32, "c" * 32
CALLER, INC_ID, ASSIGNEE = "d" * 32, "e" * 32, "5" * 32
CAPS = frozenset({"completion", "tools"})


def _rec(**over):
    base = {"sys_id": INC_ID, "number": "INC0000016", "state": "2",
            "short_description": "Rain is leaking on main DNS Server", "description": "",
            "category": "hardware", "subcategory": "", "cmdb_ci": "", "assignment_group": GRP_HW,
            "assigned_to": "", "caller_id": CALLER, "close_code": "", "close_notes": "",
            "sys_mod_count": "4"}
    base.update(over)
    display = {"cmdb_ci": {CI_DNS: "dns01", CI_WIN: "Windows"}.get(base["cmdb_ci"], ""),
               "assignment_group": {GRP_HW: "Hardware", GRP_NET: "Network"}.get(base["assignment_group"], ""),
               "assigned_to": {ASSIGNEE: "Hal Hardware"}.get(base["assigned_to"], ""),
               "caller_id": "Bow Ruggeri"}
    return {k: {"value": v, "display": display.get(k, v)} for k, v in base.items()}


def _row(rec: dict) -> dict:
    """A record from _rec() as a fake-instance row."""
    return {k: (v["value"], v["display"]) for k, v in rec.items()}


def _choice(element, value, label, dependent=""):
    return {"sys_id": f"ch-{element}-{value}", "name": "incident", "element": element,
            "inactive": "false", "language": "en", "value": value, "label": label,
            "dependent_value": dependent}


def tables(**over):
    return {
        "sys_choice": [
            _choice("category", "hardware", "Hardware"), _choice("category", "network", "Network"),
            _choice("subcategory", "dns", "DNS", "network"), _choice("subcategory", "disk", "Disk", "hardware"),
            {**_choice("category", "retired", "Retired"), "inactive": "true"},
            {**_choice("category", "hardware", "Matériel"), "sys_id": "fr", "language": "fr"},
        ],
        "cmdb_ci": [
            {"sys_id": CI_LAPTOP, "name": "ERA02", "assigned_to": CALLER, "operational_status": "1"},
            {"sys_id": CI_DNS, "name": "dns01", "assigned_to": "", "operational_status": "1"},
            {"sys_id": CI_WIN, "name": "Windows", "assigned_to": "", "operational_status": "1"},
            {"sys_id": "f" * 32, "name": "ERA", "assigned_to": "", "operational_status": "1"},
            {"sys_id": "9" * 32, "name": "Main DNS Server", "assigned_to": "", "operational_status": "6"},
        ],
        "incident": [
            _row(_rec(**over)),
            {"sys_id": "h1", "number": "INC0000001", "assignment_group": (GRP_HW, "Hardware"), "sys_created_on": "2026-10-01"},
            {"sys_id": "h2", "number": "INC0000002", "assignment_group": (GRP_NET, "Network"), "sys_created_on": "2026-10-02"},
            {"sys_id": "h3", "number": "INC0000003", "assignment_group": (GRP_EMPTY, "Ghost Group"), "sys_created_on": "2026-10-03"},
            {"sys_id": "h4", "number": "INC0000004", "assignment_group": (GRP_NOASSIGN, "Approvers"), "sys_created_on": "2026-10-04"},
            {"sys_id": "h5", "number": "INC0000005", "assignment_group": ("", ""), "sys_created_on": "2026-10-05"},
        ],
        "sys_user_group": [
            {"sys_id": GRP_HW, "name": "Hardware", "active": "true", "type": ("", "")},
            {"sys_id": GRP_NET, "name": "Network", "active": "true", "type": ("x", "itil")},
            {"sys_id": GRP_EMPTY, "name": "Ghost Group", "active": "true", "type": ("", "")},
            {"sys_id": GRP_NOASSIGN, "name": "Approvers", "active": "true", "type": ("y", "Non-Assignable")},
        ],
        "sys_user_grmember": [
            {"sys_id": "m1", "group": GRP_HW, "user": ASSIGNEE}, {"sys_id": "m2", "group": GRP_NET, "user": "6" * 32},
            {"sys_id": "m3", "group": GRP_NOASSIGN, "user": "7" * 32}],
    }


@pytest.fixture
def inst(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "sn_instance_url", lambda: "https://dev1.service-now.com")
    return FakeInstance(tables()).install(monkeypatch)


def _cands():
    return {"categories": g.category_pairs()[0],
            "cis": [{"sys_id": CI_DNS, "name": "dns01", "why": "named in the ticket text"}],
            "groups": g.group_candidates()}


def _picks(category="none", ci="none", group="none", evidence="DNS"):
    def pick(v):
        return {"value": v, "evidence": "" if v == "none" else evidence}
    return {"category": pick(category), "cmdb_ci": pick(ci), "assignment_group": pick(group)}


# ── Candidates ───────────────────────────────────────────────────────────────

def test_category_pairs_keep_subcategory_inside_its_category(inst):
    pairs, capped = g.category_pairs()
    assert {p["key"] for p in pairs} == {"hardware", "hardware/disk", "network", "network/dns"}
    assert not capped   # the retired and the French rows are not offered


def test_ci_candidates_are_current_caller_and_text_named(inst):
    cis, capped = g.ci_candidates(_rec(short_description="Windows update broke dns01", cmdb_ci=CI_DNS))
    assert {c["name"]: c["why"] for c in cis} == {
        "dns01": "already set on the incident", "ERA02": "assigned to the caller",
        "Windows": "named in the ticket text"}
    assert not capped


def test_a_short_or_partial_ci_name_is_not_text_evidence(inst):
    """'ERA' is under MIN_CI_NAME and is also inside 'ERA02': neither may match."""
    cis, _ = g.ci_candidates(_rec(short_description="ERA02 and the era of doom", caller_id=""))
    assert [c["name"] for c in cis] == ["ERA02"]


def test_a_ci_named_in_the_text_is_found_by_name_not_by_reading_the_cmdb(inst):
    """rio#1 plan of record: the text's word sequences are looked up with nameIN, so a corporate
    CMDB is never read whole. Punctuation around a name doesn't hide it; a retired CI isn't offered."""
    cis, _ = g.ci_candidates(_rec(short_description="Main DNS Server (dns01) is down!", caller_id=""))
    assert [c["name"] for c in cis] == ["dns01"]
    named = [q for q in inst.queries("cmdb_ci") if "nameIN" in q["query"]]
    assert named and all(q["query"].startswith("operational_status=1^nameIN") for q in named)
    assert "dns01" in named[0]["query"].split("nameIN")[1].split(",")


def test_name_candidates_are_shortest_first_and_never_carry_a_query_separator():
    """PR #25 review: single words (hostnames) first, so a long ticket still looks them up."""
    names = g.name_candidates("Main DNS, server^x (dns01).")
    assert names[0].count(" ") <= names[-1].count(" ")
    assert not any("^" in n or "," in n for n in names)
    assert "dns01" in names and "Main DNS" in names


def test_a_long_ticket_is_looked_up_in_chunks_and_capped(inst, monkeypatch):
    monkeypatch.setattr(g, "MAX_NAMES", 250)
    words = " ".join(f"word{i:04d}" for i in range(400))
    cis, capped = g.ci_candidates(_rec(short_description=words, caller_id=""))
    named = [q for q in inst.queries("cmdb_ci") if "nameIN" in q["query"]]
    assert len(named) == 3 and capped
    assert all(len(q["query"].split("nameIN")[1].split(",")) <= g.NAME_CHUNK for q in named)


def test_groups_are_used_active_staffed_and_assignable_only(inst):
    """Ghost Group has no members; Approvers is typed Non-Assignable (and staffed). Hardware has NO
    type at all and is offered: untyped means assignable (whitcortex PR #2612)."""
    assert [x["name"] for x in g.group_candidates()] == ["Hardware", "Network"]


def test_group_history_is_read_newest_first_and_members_in_one_count(inst):
    """whitcortex PR #2612 review: '500 recent incidents' must be ordered, or it is an arbitrary
    sample. The member check is one grouped count, not a read per group."""
    g.group_candidates()
    history, = inst.queries("incident")
    assert history["query"] == "assignment_groupISNOTEMPTY^ORDERBYDESCsys_created_on"
    assert history["limit"] == g.GROUP_HISTORY
    counts = [kw for verb, kw in inst.calls if verb == "now_aggregate"]
    assert len(counts) == 1 and counts[0]["group_by"] == "group" and counts[0]["display_value"] is False


# ── The assignee rule (whitcortex #2613) ─────────────────────────────────────

def test_with_an_assignee_only_their_groups_are_offered(inst):
    groups = g.group_candidates()
    assert [x["name"] for x in g.groups_the_assignee_is_in(groups, ASSIGNEE)] == ["Hardware"]
    assert g.groups_the_assignee_is_in(groups, "") == groups
    assert g.groups_the_assignee_is_in(groups, "8" * 32) == []


def test_the_group_is_not_established_with_the_assignee_as_the_reason(inst):
    rec = _rec(assigned_to="8" * 32)
    cands = g.gather(rec)
    assert cands["groups"] == [] and cands["assignee_excluded_groups"]
    proposal = g.validate(rec, cands, _picks(category="network/dns"))
    assert "is not a member of any group incidents go to" in proposal["assignment_group"]["reason"]
    assert proposal["category"]["status"] == "proposed"     # the rest still stands


def test_an_empty_list_for_another_reason_does_not_blame_the_assignee(inst):
    cands = {"categories": [], "cis": [], "groups": [], "assignee_excluded_groups": False}
    proposal = g.validate(_rec(), cands, _picks())
    assert proposal["assignment_group"]["reason"] == "no candidate fits"


# ── The model call ───────────────────────────────────────────────────────────

def test_the_schema_enums_are_exactly_the_candidates_plus_none():
    cands = {"categories": [{"key": "network/dns"}], "cis": [{"sys_id": CI_DNS}], "groups": [{"sys_id": GRP_NET}]}
    props = g.triage_schema(cands)["properties"]
    assert props["category"]["properties"]["value"]["enum"] == ["network/dns", "none"]
    assert props["cmdb_ci"]["properties"]["value"]["enum"] == [CI_DNS, "none"]
    assert props["assignment_group"]["properties"]["value"]["enum"] == [GRP_NET, "none"]


def test_the_model_call_is_schema_constrained_without_thinking(inst):
    seen = {}

    def chat(messages, **kw):
        seen.update(kw, messages=messages)
        return Reply(content=json.dumps(_picks()))

    g.classify(_rec(), _cands(), chat=chat)
    assert seen["format"]["properties"]["category"]["properties"]["value"]["enum"][-1] == "none"
    assert seen["think"] is False and "tools" not in seen
    assert "Rain is leaking on main DNS Server" in seen["messages"][1]["content"]


@pytest.mark.parametrize("reply", [Reply(ok=False, error="timeout"), Reply(content="not json"), Reply(content=" ")])
def test_a_failed_model_reply_raises_and_nothing_follows(inst, reply):
    with pytest.raises(ToolError, match="local model"):
        g.classify(_rec(), _cands(), chat=lambda *a, **k: reply)


# ── Validation (code decides) ────────────────────────────────────────────────

def test_a_pick_outside_the_offer_is_not_established(inst):
    proposal = g.validate(_rec(), _cands(), {
        "category": {"value": "facilities/roof", "evidence": "Rain is leaking"},
        "cmdb_ci": {"value": CI_DNS, "evidence": "main DNS Server"},
        "assignment_group": {"value": "9" * 32, "evidence": "DNS"}})
    assert proposal["category"]["status"] == "not established" and "not offered" in proposal["category"]["reason"]
    assert proposal["assignment_group"]["status"] == "not established"
    assert proposal["cmdb_ci"]["status"] == "proposed"


def test_a_malformed_reply_is_not_established_not_a_crash(inst):
    proposal = g.validate(_rec(), _cands(), {"category": "network/dns", "cmdb_ci": None})
    assert {e["status"] for e in proposal.values()} == {"not established"}
    assert g.validate(_rec(), _cands(), ["not", "a", "dict"])["category"]["status"] == "not established"


def test_evidence_must_be_a_quote_from_the_ticket(inst):
    proposal = g.validate(_rec(), _cands(), {
        "category": {"value": "network/dns", "evidence": "rain   is leaking on MAIN dns"},
        "cmdb_ci": {"value": CI_DNS, "evidence": "the server is a DNS host"},
        "assignment_group": {"value": "none", "evidence": ""}})
    assert proposal["category"]["evidence_quoted"] is True       # case/space-insensitive
    assert proposal["cmdb_ci"]["evidence_quoted"] is False       # a paraphrase
    # whitcortex #2631: an unquoted pick is not established; the note names the suggestion.
    assert proposal["cmdb_ci"]["status"] == "not established"
    assert "no evidence quoted from the ticket; the model suggested dns01" in g.render_note("INC0000016", proposal)
    assert "cmdb_ci" not in g.update_values(proposal)


def test_an_empty_evidence_string_is_unquoted_too(inst):
    proposal = g.validate(_rec(), _cands(), _picks(ci=CI_DNS, evidence=""))
    assert proposal["cmdb_ci"]["status"] == "not established"


def test_an_already_set_value_is_unchanged_not_proposed(inst):
    proposal = g.validate(_rec(), _cands(), _picks(category="hardware", group=GRP_HW, evidence=""))
    assert proposal["category"]["status"] == proposal["assignment_group"]["status"] == "unchanged"
    assert g.update_values(proposal) == {}


def test_a_category_only_change_that_would_strand_a_subcategory_is_refused(inst):
    """whitcortex PR #2612 review: hardware/disk -> plain 'network' would leave 'disk' under network."""
    proposal = g.validate(_rec(category="hardware", subcategory="disk"), _cands(), _picks(category="network"))
    assert proposal["category"]["status"] == "not established"
    assert "would leave it stale" in proposal["category"]["reason"]
    assert "category" not in g.update_values(proposal)


def test_the_current_category_without_its_subcategory_is_unchanged_not_proposed(inst):
    proposal = g.validate(_rec(category="network", subcategory="dns"), _cands(), _picks(category="network"))
    assert proposal["category"]["status"] == "unchanged"
    assert "category" not in g.update_values(proposal)


@pytest.mark.parametrize("pick,current_sub", [("network/dns", "disk"), ("network", "")])
def test_a_category_change_that_strands_nothing_is_proposed(inst, pick, current_sub):
    proposal = g.validate(_rec(category="hardware", subcategory=current_sub), _cands(), _picks(category=pick))
    assert proposal["category"]["status"] == "proposed"


def test_update_values_split_the_pair_and_carry_only_proposed_fields(inst):
    proposal = g.validate(_rec(), _cands(), _picks(category="network/dns", group=GRP_NET))
    assert g.update_values(proposal) == {"category": "network", "subcategory": "dns", "assignment_group": GRP_NET}


# ── The dry run, end to end against the fake instance ────────────────────────

def _model(picks):
    return lambda messages, **kw: Reply(content=json.dumps(picks))


def test_the_dry_run_saves_the_proposal_and_writes_nothing(inst):
    out = g.run("inc0000016", chat=_model(_picks(category="network/dns", ci=CI_DNS, group=GRP_NET)))
    assert out["note"].startswith("Triage proposal (DRY RUN, nothing changed; to apply, type: "
                                  "apply triage INC0000016): INC0000016")
    assert "- Category: Network / DNS [proposed]; evidence: \"DNS\"" in out["note"]
    assert {verb for verb, _ in inst.calls} <= {"now_query", "now_aggregate"}   # reads only
    saved = g.load_proposal("INC0000016")
    assert saved["sys_id"] == INC_ID and saved["sys_mod_count"] == "4"
    assert saved["proposal"]["assignment_group"]["value"] == GRP_NET


def test_read_only_the_note_says_to_set_it_by_hand(inst, monkeypatch):
    monkeypatch.setattr(config, "READ_ONLY", True)
    out = g.run("INC0000016", chat=_model(_picks()))
    assert "rio is read-only here, so set it by hand" in out["note"] and "apply triage" not in out["note"]


def test_an_unknown_incident_is_refused_before_the_model(inst):
    with pytest.raises(ToolError, match="Incident INC0009999 not found"):
        g.run("INC0009999", chat=lambda *a, **k: pytest.fail("the model was called"))


# ── Tools and commands (whitcortex #2614) ────────────────────────────────────

@pytest.fixture
def loaded(inst):
    @register("now_query", group="servicenow", writes=False)
    def now_query(table: str) -> dict:
        """stand-in for the real now_query"""
        return {}
    sn.load()
    return inst


def test_the_model_sees_triage_but_never_apply(loaded):
    menu = {s["function"]["name"]: s["function"]["description"] for s in schemas()}
    assert "sn_triage" in menu and "sn_triage_apply" not in menu
    assert "Only the operator can apply it, by typing `apply triage <number>`; you cannot." in menu["sn_triage"]
    assert not TOOLS["sn_triage"].writes
    assert TOOLS["sn_triage_apply"].writes and TOOLS["sn_triage_apply"].operator_only


def test_a_model_that_names_apply_anyway_gets_no_card(loaded):
    """Models call tools they weren't offered. park() without operator=True refuses an
    operator_only write, and execute_tool refuses any unapproved write."""
    assert approval.park("sn_triage_apply", {"number": "INC0000016"}).startswith(
        "Tool error (sn_triage_apply): only the operator can start this")
    from rio.tools import execute_tool
    assert "runs only from an approved card" in execute_tool("sn_triage_apply", {"number": "INC0000016"})


@pytest.mark.parametrize("text,number", [
    ("apply triage INC0000016", "INC0000016"), ("Apply Triage inc0000016.", "INC0000016"),
    ("triage apply INC0000016!", "INC0000016")])
def test_the_apply_command_is_recognised(text, number):
    assert commands.apply_number(text) == number and commands.triage_number(text) is None


@pytest.mark.parametrize("text", [
    "please apply triage INC0000016", "apply triage INC0000016 now", "should I apply triage INC0000016?",
    "apply triage", "apply triage CHG0000016"])
def test_anything_else_is_not_the_apply_command(text):
    assert commands.apply_number(text) is None


def test_triage_typed_runs_the_dry_run_with_no_chat_turn(loaded, monkeypatch):
    monkeypatch.setattr(g.llm, "chat", _model(_picks(category="network/dns")))
    monkeypatch.setattr(g, "classify", lambda r, c, chat=None: _picks(category="network/dns"))
    shown, card = main.step("triage INC0000016", [], None, CAPS,
                            run=lambda *a, **k: pytest.fail("a chat turn ran for the command"))
    assert card is None and shown[0].startswith("rio> Triage proposal (DRY RUN")


def test_apply_typed_without_a_proposal_says_so_and_parks_nothing(loaded):
    shown, card = main.step("apply triage INC0000016", [], None, CAPS, run=lambda *a, **k: pytest.fail("chat"))
    assert card is None
    assert shown == ["rio> Not applied: Tool error (sn_triage_apply): no triage proposal for INC0000016; "
                     "run `triage INC0000016` first\n"]


def test_apply_typed_read_only_is_refused(loaded, monkeypatch):
    g.run("INC0000016", chat=_model(_picks(category="network/dns")))
    monkeypatch.setattr(config, "READ_ONLY", True)
    shown, card = main.step("apply triage INC0000016", [], None, CAPS, run=lambda *a, **k: pytest.fail("chat"))
    assert card is None and "rio is read-only here (RIO_READ_ONLY)" in shown[0]


# ── Apply: from the card only, the saved proposal, read back (whitcortex #2597, #2612, #2613) ──

def _incident(inst):
    return next(r for r in inst.tables["incident"] if r["sys_id"][0] == INC_ID)


def _proposed(loaded):
    """A proposal for all three fields: the description names dns01, so it is a CI candidate."""
    _incident(loaded)["description"] = "The main DNS Server is dns01."
    out = g.run("INC0000016", chat=_model(_picks(category="network/dns", ci=CI_DNS, group=GRP_NET,
                                                 evidence="main DNS Server")))
    assert {e["status"] for e in out["proposal"].values()} == {"proposed"}, out["note"]


def test_the_card_shows_the_real_diff_with_names(loaded):
    _proposed(loaded)
    shown, card = main.step("apply triage INC0000016", [], None, CAPS, run=lambda *a, **k: pytest.fail("chat"))
    assert card is not None and card.tool == "sn_triage_apply" and card.args == {"number": "INC0000016", "proposal": g.pending_id("INC0000016")}
    text = shown[0]
    assert "APPROVAL NEEDED: sn_triage_apply" in text
    assert 'category: "hardware" -> "network" (Network / DNS)' in text
    assert f'cmdb_ci: "" -> "{CI_DNS}" (dns01)' in text
    assert f'assignment_group: "{GRP_HW}" -> "{GRP_NET}" (Network)' in text
    assert loaded.writes() == []                                   # the card wrote nothing


def test_yes_applies_the_saved_proposal_reads_it_back_and_notes_it(loaded):
    _proposed(loaded)
    history = []
    _, card = main.step("apply triage INC0000016", history, None, CAPS, run=lambda *a, **k: pytest.fail("chat"))
    shown, card = main.step("y", history, card, CAPS, run=lambda *a, **k: pytest.fail("chat"))
    assert card is None
    assert shown == ["rio> Applied to INC0000016 and read back: assignment_group, category, cmdb_ci, "
                     "subcategory. A work note says so.\n"]
    fields, note = loaded.writes()
    assert fields["values"] == {"category": "network", "subcategory": "dns", "cmdb_ci": CI_DNS,
                                "assignment_group": GRP_NET}
    assert note["values"]["work_notes"].startswith("Triage APPLIED (local model, reviewed proposal): INC0000016")
    assert g.load_proposal("INC0000016") is None                  # applied once
    assert history[-1]["content"].startswith("[sn_triage_apply approved and run]")


def test_anything_but_yes_applies_nothing(loaded):
    _proposed(loaded)
    _, card = main.step("apply triage INC0000016", [], None, CAPS, run=lambda *a, **k: pytest.fail("chat"))
    shown, _ = main.step("n", [], card, CAPS, run=lambda *a, **k: pytest.fail("chat"))
    assert shown == ["rio> Not done: nothing was changed.\n"] and loaded.writes() == []
    assert g.load_proposal("INC0000016") is not None


def test_apply_refuses_an_incident_changed_since_the_proposal(loaded):
    _proposed(loaded)
    _incident(loaded)["sys_mod_count"] = ("5", "5")
    shown, card = main.step("apply triage INC0000016", [], None, CAPS, run=lambda *a, **k: pytest.fail("chat"))
    assert card is None and "has changed since the proposal (sys_mod_count 4 -> 5)" in shown[0]


def test_a_change_while_the_card_waits_is_caught_at_yes(loaded):
    """The card can sit; the check runs again when the operator answers it."""
    _proposed(loaded)
    _, card = main.step("apply triage INC0000016", [], None, CAPS, run=lambda *a, **k: pytest.fail("chat"))
    _incident(loaded)["sys_mod_count"] = ("5", "5")
    shown, _ = main.step("yes", [], card, CAPS, run=lambda *a, **k: pytest.fail("chat"))
    assert "has changed since the proposal" in shown[0] and loaded.writes() == []


def test_a_field_a_business_rule_keeps_is_a_failure_and_no_note_is_written(loaded):
    """whitcortex #2613: the stock 'Abort changes on group' rule aborts the whole update and the
    instance still answers 200. now_update's read-back is what says so."""
    _proposed(loaded)
    loaded.keep = {"category", "subcategory", "cmdb_ci", "assignment_group"}
    _, card = main.step("apply triage INC0000016", [], None, CAPS, run=lambda *a, **k: pytest.fail("chat"))
    shown, _ = main.step("y", [], card, CAPS, run=lambda *a, **k: pytest.fail("chat"))
    assert "INC0000016 was saved, but nothing took; these did not: category (asked 'network'" in shown[0]
    assert "No work note was added; the proposal is kept." in shown[0]
    assert [w for w in loaded.writes() if "work_notes" in w["values"]] == []


def test_a_partial_apply_names_what_took(loaded):
    _proposed(loaded)
    loaded.keep = {"assignment_group"}
    with pytest.raises(ToolError) as caught:
        g.apply("INC0000016", g.pending_id("INC0000016"))
    assert "these took: category, cmdb_ci, subcategory; these did not: assignment_group" in str(caught.value)


def test_a_failed_applied_note_says_the_fields_were_applied(loaded, monkeypatch):
    """whitcortex PR #2612 review: a bare failure here would read as 'nothing applied'."""
    _proposed(loaded)
    real = loaded.now_update

    def note_fails(table, sys_id, values, confirmed=False):
        if confirmed and "work_notes" in values:
            raise ValueError("journal write refused")
        return real(table, sys_id, values, confirmed)
    from now_data import verbs
    monkeypatch.setattr(verbs, "now_update", note_fails)
    with pytest.raises(ToolError) as caught:
        g.apply("INC0000016", g.pending_id("INC0000016"))
    assert str(caught.value).startswith("Applied to INC0000016 and read back (assignment_group, category, "
                                        "cmdb_ci, subcategory), but the 'applied' work note may "
                                        "not have been written")


def test_an_unconfirmed_note_is_said_as_unconfirmed_not_failed(loaded):
    _proposed(loaded)
    loaded.journal_hidden = True
    assert "accepted but could not be confirmed in the journal" in g.apply("INC0000016", g.pending_id("INC0000016"))


def test_nothing_proposed_means_no_card(loaded):
    g.run("INC0000016", chat=_model(_picks()))
    shown, card = main.step("apply triage INC0000016", [], None, CAPS, run=lambda *a, **k: pytest.fail("chat"))
    assert card is None and "nothing to apply on INC0000016: no field was proposed" in shown[0]
