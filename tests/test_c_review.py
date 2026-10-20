# tests/test_c_review.py — what the independent review of PR #26 found, pinned (#1).

import pytest

from rio import config
from rio.llm import Reply
from rio.tools.errors import ToolError

pytest.importorskip("now_data.verbs")

from fake_instance import FakeInstance  # noqa: E402
from rio import __main__ as main, approval, enrich, resolve, tags  # noqa: E402
from test_enrich import CAPS, INC, _main, _no_writes, model  # noqa: E402
from test_enrich import inst as enrich_inst  # noqa: E402,F401
from test_resolve import _card_and_yes, _incident, _load  # noqa: E402


def _no_chat(*a, **k):
    pytest.fail("chat")


@pytest.fixture
def einst(enrich_inst):
    return enrich_inst


@pytest.fixture
def rinst(monkeypatch):
    return _load(monkeypatch)


# ── 1. A note that may have landed can't be posted twice ─────────────────────

def test_a_note_whose_outcome_is_unknown_is_never_posted_again(einst):
    enrich.run("INC0000059", chat=model())
    einst.journal_hidden = True                                       # sent, not confirmed
    with pytest.raises(ToolError, match="MAY have landed"):
        enrich.apply("INC0000059", enrich.pending_id("INC0000059"))
    einst.journal_hidden = False
    shown, card = main.step("apply enrich INC0000059", [], None, CAPS, run=_no_chat)
    assert card is None and "was sent to INC0000059 at" in shown[0] and "check INC0000059's work notes" in shown[0]
    assert len([w for w in einst.writes() if "work_notes" in w["values"]]) == 1


def test_a_dropped_connection_on_the_note_leaves_it_marked_sent(einst, monkeypatch):
    import requests
    enrich.run("INC0000059", chat=model())
    from now_data import verbs
    monkeypatch.setattr(verbs, "now_update", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("reset")))
    with pytest.raises(ToolError, match="may not have been written"):
        enrich.apply("INC0000059", enrich.pending_id("INC0000059"))
    assert enrich.load_note("INC0000059")["sent_at"]


def test_a_refused_note_can_be_sent_again(einst, monkeypatch):
    enrich.run("INC0000059", chat=model())
    real = einst.now_update
    from now_data import verbs
    monkeypatch.setattr(verbs, "now_update", lambda *a, **k: {**real(*a, **k), "action": "not_applied",
                                                               "changed_fields": []} if k.get("confirmed") else real(*a, **k))
    with pytest.raises(ToolError, match="was not written"):
        enrich.apply("INC0000059", enrich.pending_id("INC0000059"))
    assert "sent_at" not in enrich.load_note("INC0000059")


# ── 2. A failed read after the writes doesn't hide them ─────────────────────

def test_a_failed_verification_read_after_both_writes_is_said(einst, monkeypatch):
    import requests
    enrich.run("INC0000059", chat=model())
    note = enrich.pending_id("INC0000059")
    real = enrich.incident_record
    calls = {"n": 0}

    def flaky(number):
        calls["n"] += 1
        if calls["n"] > 1:                                            # after _plan's read
            raise requests.ConnectionError("reset")
        return real(number)
    monkeypatch.setattr(enrich, "incident_record", flaky)
    out = enrich.apply("INC0000059", note)
    assert "the enrichment note is posted" in out and "The verification read after the writes failed (ConnectionError" in out


def test_a_failed_report_read_after_resolving_still_says_resolved(rinst, monkeypatch):
    import requests
    card = approval.park("sn_resolve", {"number": "INC0010049"})
    real = resolve.incident_record
    calls = {"n": 0}

    def flaky(number):
        calls["n"] += 1
        if calls["n"] > 1:
            raise requests.ConnectionError("reset")
        return real(number)
    monkeypatch.setattr(resolve, "incident_record", flaky)
    result = approval.answer(card, "y")
    assert result.startswith("INC0010049 is now Resolved (read back when saved; the report's read failed")


# ── 4/5. Tagging that stops part-way says what happened ──────────────────────

def test_a_tag_failing_part_way_through_resolve_says_it_is_not_resolved(rinst):
    rinst.insert_override = {"label": {"active": "false"}}             # the first NEW tag fails
    _, result = _card_and_yes()
    assert result.startswith("Tool error (sn_resolve): INC0010049 was NOT resolved: tagging INC0010049 stopped at")
    assert _incident(rinst)["state"][0] == "1"


# ── 6. The card shows the whole note ─────────────────────────────────────────

def test_the_enrich_card_shows_every_line_of_the_note(einst):
    out = enrich.run("INC0000059", chat=model())
    _, card = main.step("apply enrich INC0000059", [], None, CAPS, run=_no_chat)
    for line in out["note"].split("\n"):
        assert line in card.preview


# ── 7. Resolve's tags at `y` are the card's ──────────────────────────────────

def test_a_tag_that_changed_while_the_card_waits_is_refused(rinst):
    card = approval.park("sn_resolve", {"number": "INC0010049"})
    _incident(rinst)["cmdb_ci"] = ("c" * 32, "RENAMED-CI")               # a rename doesn't bump the incident
    result = approval.answer(card, "y")
    assert "the tags for INC0010049 changed after the card was shown" in result
    assert rinst.writes() == [] and rinst.writes("now_insert") == []


# ── 8. Read-only keeps nothing ───────────────────────────────────────────────

def test_read_only_enrichment_keeps_nothing(einst, monkeypatch):
    monkeypatch.setattr(config, "READ_ONLY", True)
    enrich.run("INC0000059", chat=model())
    assert enrich.load_note("INC0000059") is None


# ── Nits: guards that had no test ────────────────────────────────────────────

def test_an_unreadable_mod_count_is_a_change_in_enrich_and_resolve(einst, monkeypatch):
    enrich.run("INC0000059", chat=model())
    _main(einst)["sys_mod_count"] = ""
    saved = enrich.load_note("INC0000059")
    enrich._save("INC0000059", {**saved, "sys_mod_count": ""})
    with pytest.raises(ToolError, match="has changed since the enrichment"):
        enrich.apply("INC0000059", saved["id"])


def test_resolve_refuses_an_unreadable_mod_count(rinst):
    card = approval.park("sn_resolve", {"number": "INC0010049"})
    resolve._SHOWN["INC0010049"] = (resolve._SHOWN["INC0010049"][0], "", resolve._SHOWN["INC0010049"][2])
    _incident(rinst)["sys_mod_count"] = ""
    assert "changed after the card was shown" in approval.answer(card, "y")


def test_a_private_or_inactive_tag_of_the_same_name_is_not_reused(rinst):
    rinst.tables["label"] = [{"sys_id": "d1" * 16, "name": "Router", "viewable_by": "me", "type": "standard",
                              "active": "true"},
                             {"sys_id": "d2" * 16, "name": "Router", "viewable_by": "everyone", "type": "standard",
                              "active": "false"}]
    assert tags.existing_tag("Router") == ""


def test_no_alert_lookup_without_a_sys_id(rinst):
    assert resolve.read_alert("x^ORactive=true") is None and rinst.queries("em_alert") == []


def test_a_tag_already_on_the_record_is_planned_as_already(rinst):
    tags.tag("incident", INC if len(INC) == 32 else "a" * 32, "INC0010049", ["Router"])
    plan = tags.plan("incident", "a" * 32, ["Router", "New one"])
    assert plan == {"new": ["New one"], "add": ["New one"], "already": ["Router"]}


def test_a_script_term_never_reaches_a_like_chain():
    assert enrich.ground_terms(["javascript:gs.log(1)", "file share"], "javascript:gs.log(1) file share") == ["file share"]


def test_tag_names_are_safe_text_and_deduped_as_the_instance_matches():
    assert tags.names(["Email", "email", "Net\x1bwork", "A\u202eB", "x\u200by"]) == ["Email", "Net work", "A B", "x y"]


def test_a_term_with_a_query_or_wildcard_character_never_reaches_a_like_chain():
    """`_TERM_CHARS` is the narrower guard: `%`, `*`, `=`, `@`, `!`, `<`, `>` aren't in a term (LIKE
    wildcards and operators), even where query_safe would let them by."""
    text = "50% off a=b x*y me@host !now <tag> file share"
    assert enrich.ground_terms(["50% off", "a=b", "x*y", "me@host", "!now", "<tag>", "file share"], text) == ["file share"]
