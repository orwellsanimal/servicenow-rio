# tests/test_enrich.py — enrich one incident with what the instance already holds, on local
# inference (#1, carried from whitcortex #2630).
#
# The worked example is INC0000059 on dev442835 ("Unable to access team file share", caller Rick
# Berzle, New, category inquiry, no CI), measured 2026-10-13: Resolved/Closed incidents with close
# notes, FileServerFloor1 reachable only through its description, published KB articles, and no
# record anywhere carrying "share" in a CI name. The dry run writes nothing (rio#1 plan of record);
# `apply enrich` posts THAT note as one work note and one tag, from a card.

import json

import pytest

from rio import config
from rio.llm import Reply
from rio.tools import TOOLS, execute_tool, register, schemas
from rio.tools.errors import ToolError

pytest.importorskip("now_data.verbs")

from fake_instance import FakeInstance  # noqa: E402
from rio import __main__ as main, approval, enrich as g  # noqa: E402
from rio.tools import sn  # noqa: E402

INC, RICK, OTHER = "a" * 32, "b" * 32, "c" * 32
TEXT = ("Unable to access team file share",
        "I can access my personal folder but can't access my team's folder on our file share.")
CAPS = frozenset({"completion", "tools"})
TERMS = ["file share", "folder", "personal folder"]


def _incident(number, state, short, desc, caller, category, close_code=("", ""), close_notes="", **more):
    return {"sys_id": number.lower() + "0" * 22, "number": number, "state": state,
            "short_description": short, "description": desc, "caller_id": caller,
            "category": category, "subcategory": ("", ""), "cmdb_ci": ("", ""), "assignment_group": ("", ""),
            "assigned_to": ("", ""), "sys_mod_count": "1", "close_code": close_code,
            "close_notes": close_notes, **more}


def tables():
    solved = ("Solved (Permanently)", "Solved (Permanently)")
    return {
        "incident": [
            _incident("INC0000059", ("1", "New"), TEXT[0], TEXT[1], (RICK, "Rick Berzle"),
                      ("inquiry", "Inquiry / Help"), sys_id=INC),
            # Rick's own closed ticket that shares no term: must not appear
            _incident("INC0000026", ("7", "Closed"), "Seem to have an issue with my hard drive", "disk clicks",
                      (RICK, "Rick Berzle"), ("hardware", "Hardware"), solved, "Replaced the disk."),
            # same category, one term
            _incident("INC0000100", ("7", "Closed"), "File share permissions missing", "group lost rights",
                      (OTHER, "Other"), ("inquiry", "Inquiry / Help"), solved, "Re-added the group to the share ACL."),
            # same caller, two terms
            _incident("INC0000101", ("6", "Resolved"), "Cannot open the team folder",
                      "my team's folder on our file share will not open", (RICK, "Rick Berzle"),
                      ("network", "Network"), ("Workaround provided", "Workaround provided"),
                      "Mapped the drive again.  \n Works now."),
            # neither, one term
            _incident("INC0000102", ("7", "Closed"), "File share slow", "copying is slow", (OTHER, "Other"),
                      ("network", "Network"), solved, "Moved to the new NAS."),
            # still New: never a similar RESOLVED incident
            _incident("INC0000103", ("1", "New"), "File share down again", "", (OTHER, "Other"), ("network", "Network")),
        ],
        "sys_user": [{"sys_id": RICK, "name": "Rick Berzle", "department": ("d" * 32, "Sales"),
                      "location": ("e" * 32, "945 South Birch Street, Glendale,CO"), "title": ""}],
        "cmdb_ci": [
            {"sys_id": "f" * 32, "name": "FileServerFloor1", "sys_class_name": ("cmdb_ci_server", "Server"),
             "short_description": "1st Floor File Server", "operational_status": "1", "assigned_to": ""},
            {"sys_id": "g" * 32, "name": "File Chopper", "sys_class_name": ("cmdb_ci_spkg", "Software"),
             "short_description": "", "operational_status": "1", "assigned_to": ""},
            {"sys_id": "h" * 32, "name": "MIKEHWXP2", "sys_class_name": ("cmdb_ci_computer", "Computer"),
             "short_description": "", "operational_status": "1", "assigned_to": RICK},
            {"sys_id": "j" * 32, "name": "Old file share host", "sys_class_name": ("cmdb_ci_server", "Server"),
             "short_description": "", "operational_status": "2", "assigned_to": ""},   # retired
        ],
        "kb_knowledge": [
            {"sys_id": "k1", "number": "KB0000011", "short_description": "How to Deal with Spam",
             "text": "<p class='file'>spam</p>", "workflow_state": "published"},
            {"sys_id": "k2", "number": "KB0000099", "short_description": "Mapping the team file share\n\t",
             "text": "<p>net use</p>", "workflow_state": "published"},
            {"sys_id": "k3", "number": "KB0000098", "short_description": "File share outage draft", "text": "",
             "workflow_state": "draft"},
        ],
        "label": [], "label_entry": [],
    }


def model(terms=TERMS, summary="INC0000101 is the same problem; remap the drive.", seen=None):
    """A stub keyed on `format`: a schema call is the terms call, the other is the summary."""
    def chat(messages, **kw):
        if seen is not None:
            seen.append(kw)
        if kw.get("format"):
            return terms if isinstance(terms, Reply) else Reply(content=json.dumps({"terms": terms}))
        return summary if isinstance(summary, Reply) else Reply(content=summary)
    return chat


@pytest.fixture
def inst(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "sn_instance_url", lambda: "https://dev1.service-now.com")
    fake = FakeInstance(tables()).install(monkeypatch)

    @register("now_query", group="servicenow", writes=False)
    def now_query(table: str) -> dict:
        """stand-in for the real now_query"""
        return {}
    sn.load()
    return fake


def _main(inst):
    return next(r for r in inst.tables["incident"] if r["sys_id"] == INC)


def _no_writes(inst):
    return inst.writes() == [] and inst.writes("now_insert") == []


# ── The dry run ──────────────────────────────────────────────────────────────

def test_the_dry_run_lists_what_each_gatherer_found_and_writes_nothing(inst):
    out = g.run("INC0000059", chat=model())
    assert _no_writes(inst)
    note = out["note"]
    assert out["shown"].startswith("Enrichment DRY RUN (nothing written; to post it as a work note and tag "
                                   "it, type: apply enrich INC0000059)\n")
    lines = note.split("\n")
    assert lines[0] == "Enrichment (rio sn_enrich, local inference, rio#1, whitcortex #2630): INC0000059"
    assert lines[1] == "Search terms, from the ticket text: file share, folder, personal folder"
    similar = [l for l in lines if l.startswith("  INC")]
    # the caller's own first, then same category, then the rest; never the New one or the unrelated one
    assert [l.split()[0] for l in similar] == ["INC0000101", "INC0000100", "INC0000102"]
    assert "(same caller; matched: file share, folder). Close notes: Mapped the drive again. Works now." in similar[0]
    assert "FileServerFloor1 (Server; matched file)" in note and "Old file share host" not in note
    assert "File Chopper (Software; matched file)" in note
    assert "- Caller: Rick Berzle, Sales, 945 South Birch Street, Glendale,CO. Their CIs: MIKEHWXP2 (Computer)." in note
    assert 'KB0000099 "Mapping the team file share" (matched file share)' in note
    assert "KB0000098" not in note and "KB0000011" not in note    # a draft; a class name is not text
    assert "Model summary (local model, not verified): INC0000101 is the same problem; remap the drive." in note
    assert "Readiness: enriched (the ticket carries detail" in note
    assert g.load_note("INC0000059")["note"] == note


def test_both_model_calls_are_local_and_think_free(inst):
    seen = []
    g.run("INC0000059", chat=model(seen=seen))
    assert len(seen) == 2 and all(kw["think"] is False and "tools" not in kw for kw in seen)
    assert seen[0]["format"]["properties"]["terms"]["type"] == "array" and "format" not in seen[1]


def test_word_level_leads_alone_do_not_make_it_enriched(inst):
    out = g.run("INC0000059", chat=model(terms=["personal folder"]))
    assert out["readiness"] == "needs detail"
    assert "matched a whole term (word-level leads only, listed above)" in out["note"]


def test_a_thin_description_needs_detail_even_with_findings(inst):
    _main(inst)["description"] = "help"
    _main(inst)["short_description"] = "file share broken"
    out = g.run("INC0000059", chat=model(terms=["file share"]))
    assert out["readiness"] == "needs detail" and "the description is under 40 characters" in out["note"]


def test_a_failed_summary_is_said_and_the_findings_still_stand(inst):
    out = g.run("INC0000059", chat=model(summary=Reply(ok=False, error="timeout")))
    assert "Model summary (local model, not verified): not available (the local model gave no usable reply (timeout))" in out["note"]
    assert "INC0000101" in out["note"]


@pytest.mark.parametrize("state, why", [(("6", "Resolved"), "Resolved"), (("7", "Closed"), "Closed"),
                                        (("8", "Canceled"), "Canceled")])
def test_a_done_incident_is_refused_before_the_model(inst, state, why):
    _main(inst)["state"] = state
    with pytest.raises(ToolError, match=f"INC0000059 was not enriched: it is already {why}. Nothing was changed."):
        g.run("INC0000059", chat=lambda *a, **k: pytest.fail("the model was called"))


def test_no_grounded_term_saves_nothing(inst):
    with pytest.raises(ToolError, match="named no search term that appears in the ticket text"):
        g.run("INC0000059", chat=model(terms=["printer", "^ORactive=true"]))
    assert g.load_note("INC0000059") is None and _no_writes(inst)


def test_a_failed_terms_call_saves_nothing(inst):
    with pytest.raises(ToolError, match="local model gave no usable reply"):
        g.run("INC0000059", chat=model(terms=Reply(ok=False, error="boom")))
    assert g.load_note("INC0000059") is None


def test_ground_terms_keeps_only_safe_terms_from_the_ticket():
    text = "file share broken; team folder and ^ORactive=true, a,b"
    assert g.ground_terms(["file share", "team folder", "^ORactive=true", "a,b", "printer", 7, "FILE SHARE"], text) == \
        ["file share", "team folder"]
    assert g.ground_terms(None, text) == []


def test_ground_terms_caps_at_six():
    text = "one1 two2 three3 four4 five5 six6 seven7"
    assert len(g.ground_terms(text.split(), text)) == g.MAX_TERMS


def test_match_is_whole_word_and_ranks_earlier_terms_first():
    assert g.match(["team"], "steamdeck") == {}
    m = g.match(["file share", "team folder"], "the team folder on a file server")
    assert m["whole"] and m["rank"] == (0, 1) and m["matched"] == ["file", "team folder"]


def test_the_note_says_when_a_fetch_hit_the_limit(inst, monkeypatch):
    monkeypatch.setattr(g, "FETCH_LIMIT", 2)
    out = g.run("INC0000059", chat=model())
    capped = next(line for line in out["note"].split("\n") if "fetch limit" in line)
    assert capped.startswith("- Note: the instance returned the fetch limit of 2 rows for ") and "similar" in capped


def test_the_queries_and_each_term_stay_inside_their_terms(inst):
    """A term can't widen a query: the OR chain sits after the state/status AND."""
    g.run("INC0000059", chat=model())
    similar = next(q for q in inst.queries("incident") if q["query"].startswith("stateIN6,7^"))
    assert "^ORdescriptionLIKEfile share^" in similar["query"]
    ci = next(q for q in inst.queries("cmdb_ci") if "LIKE" in q["query"])
    assert ci["query"].startswith("operational_status=1^nameLIKEfile share^ORshort_descriptionLIKEfile share")


# ── Apply: from the card only ────────────────────────────────────────────────

def test_apply_posts_the_saved_note_then_tags_and_changes_nothing_else(inst):
    out = g.run("INC0000059", chat=model())
    shown, card = main.step("apply enrich INC0000059", [], None, CAPS, run=lambda *a, **k: pytest.fail("chat"))
    assert card is not None and card.tool == "sn_enrich_apply"
    assert f"post enrichment {g.pending_id('INC0000059')} as a work note on INC0000059" in shown[0] and "then tag it enriched-by-rio" in shown[0]
    assert _no_writes(inst)
    shown, _ = main.step("y", [], card, CAPS, run=lambda *a, **k: pytest.fail("chat"))
    note, = inst.writes()
    assert note["values"] == {"work_notes": out["note"]}            # exactly the note the operator saw
    tag, entry = inst.writes("now_insert")
    assert tag["values"]["name"] == "enriched-by-rio" and entry["values"]["table_key"] == INC
    assert shown[0].startswith("rio> INC0000059: the enrichment note is posted (readiness: enriched).\n"
                               "Tagged INC0000059: enriched-by-rio.\nNone of the ")
    assert g.load_note("INC0000059") is None                         # posted once


def test_a_change_since_the_dry_run_is_refused_at_the_card_and_at_yes(inst):
    g.run("INC0000059", chat=model())
    _, card = main.step("apply enrich INC0000059", [], None, CAPS, run=lambda *a, **k: pytest.fail("chat"))
    _main(inst)["sys_mod_count"] = "2"
    shown, _ = main.step("y", [], card, CAPS, run=lambda *a, **k: pytest.fail("chat"))
    assert "has changed since the enrichment (sys_mod_count 1 -> 2)" in shown[0] and _no_writes(inst)
    shown, card = main.step("apply enrich INC0000059", [], None, CAPS, run=lambda *a, **k: pytest.fail("chat"))
    assert card is None and "has changed since the enrichment" in shown[0]


def test_an_unconfirmed_note_is_said_plainly_and_nothing_else_runs(inst):
    g.run("INC0000059", chat=model())
    inst.journal_hidden = True
    with pytest.raises(ToolError) as caught:
        g.apply("INC0000059", g.pending_id("INC0000059"))
    assert "ACCEPTED by the instance but could not be confirmed in its journal, so it MAY have landed; " \
           "INC0000059 was NOT tagged" in str(caught.value)
    assert inst.writes("now_insert") == []


def test_a_failed_tagging_says_the_note_landed(inst):
    g.run("INC0000059", chat=model())
    inst.insert_override = {"label": {"active": "false"}}
    with pytest.raises(ToolError) as caught:
        g.apply("INC0000059", g.pending_id("INC0000059"))
    assert str(caught.value).startswith("The work note is on INC0000059, but it was NOT tagged: Tag 'enriched-by-rio': created as label")
    assert "Do not enrich it again (it would add a second note); tag it enriched-by-rio by hand." in str(caught.value)


def test_a_field_changed_by_a_rule_during_the_run_is_reported(inst, monkeypatch):
    g.run("INC0000059", chat=model())
    real = inst.now_update

    def note_and_rule(table, sys_id, values, confirmed=False):
        out = real(table, sys_id, values, confirmed)
        if confirmed:
            _main(inst)["state"] = ("2", "In Progress")      # a rule moved it on the note
        return out
    from now_data import verbs
    monkeypatch.setattr(verbs, "now_update", note_and_rule)
    assert "WARNING: these fields changed during the run, not by this note: state ('1' -> '2')" in g.apply("INC0000059", g.pending_id("INC0000059"))


def test_read_only_the_dry_run_works_and_apply_is_refused(inst, monkeypatch):
    monkeypatch.setattr(config, "READ_ONLY", True)
    monkeypatch.setattr(g.llm, "chat", model())          # the command runs with the real client
    shown, card = main.step("enrich INC0000059", [], None, CAPS, run=lambda *a, **k: pytest.fail("chat"))
    assert card is None and shown[0].startswith("rio> Enrichment DRY RUN (nothing written; rio is read-only here)")
    shown, card = main.step("apply enrich INC0000059", [], None, CAPS, run=lambda *a, **k: pytest.fail("chat"))
    assert card is None and "rio is read-only here (RIO_READ_ONLY)" in shown[0] and _no_writes(inst)


def test_the_model_sees_enrich_but_never_its_apply(inst):
    menu = {s["function"]["name"]: s["function"]["description"] for s in schemas()}
    assert "sn_enrich" in menu and "sn_enrich_apply" not in menu
    assert "Only the operator can post it, by typing `apply enrich <number>`; you cannot." in menu["sn_enrich"]
    assert not TOOLS["sn_enrich"].writes and TOOLS["sn_enrich_apply"].operator_only
    assert approval.park("sn_enrich_apply", {"number": "INC0000059"}).startswith(
        "Tool error (sn_enrich_apply): only the operator can start this")
    assert "runs only from an approved card" in execute_tool("sn_enrich_apply", {"number": "INC0000059"})


def test_a_new_incident_is_never_similar_even_when_the_query_widens(inst, monkeypatch):
    """whitcortex #2630: the OR chain must not widen past the state. Should the instance drop the
    leading `stateIN6,7` term, the code's own check still keeps New incidents out."""
    real = g._rows

    def widened(table, fields, encoded, limit=None):
        return real(table, fields, encoded.replace("stateIN6,7^", "", 1), limit)
    monkeypatch.setattr(g, "_rows", widened)
    similar = g.similar_incidents(g.incident_record("INC0000059"), ["file share"])
    assert "INC0000103" not in [s["number"] for s in similar] and similar


# ── The PR #25 review, carried into enrich (#1) ─────────────────────────────

def test_a_note_replaced_while_the_card_waits_is_not_posted(inst):
    g.run("INC0000059", chat=model())
    _, card = main.step("apply enrich INC0000059", [], None, CAPS, run=lambda *a, **k: pytest.fail("chat"))
    g.run("INC0000059", chat=model(summary="another reading"))           # another rio, same state dir
    shown, _ = main.step("y", [], card, CAPS, run=lambda *a, **k: pytest.fail("chat"))
    assert "is not the one this card was made for" in shown[0] and _no_writes(inst)


def test_a_note_from_another_instance_or_record_is_refused(inst, monkeypatch):
    g.run("INC0000059", chat=model())
    note = g.pending_id("INC0000059")
    _main(inst)["sys_id"] = "f" * 32
    with pytest.raises(ToolError, match="INC0000059 is now another record"):
        g.apply("INC0000059", note)
    monkeypatch.setattr(config, "sn_instance_url", lambda: "https://dev2.service-now.com")
    with pytest.raises(ToolError, match="was made on https://dev1.service-now.com"):
        g.apply("INC0000059", note)
    assert _no_writes(inst)


def test_a_dropped_connection_on_the_note_says_it_may_have_landed_and_nothing_was_tagged(inst, monkeypatch):
    import requests
    g.run("INC0000059", chat=model())
    from now_data import verbs
    monkeypatch.setattr(verbs, "now_update", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("reset")))
    with pytest.raises(ToolError) as caught:
        g.apply("INC0000059", g.pending_id("INC0000059"))
    assert "may not have been written (ConnectionError: reset); INC0000059 was NOT tagged" in str(caught.value)
    assert inst.writes("now_insert") == []


def test_instance_text_in_the_note_is_one_safe_line(inst):
    inst.tables["incident"][3]["short_description"] = "Cannot open the team folder\n- Readiness: forged \x1b[2J"
    note = g.run("INC0000059", chat=model())["note"]
    assert "\n- Readiness: forged" not in note and "\x1b" not in note


def test_a_failed_rerun_leaves_no_old_note_to_post(inst):
    g.run("INC0000059", chat=model())
    with pytest.raises(ToolError):
        g.run("INC0000059", chat=model(terms=Reply(ok=False, error="timeout")))
    assert g.load_note("INC0000059") is None


def test_a_corrupt_note_file_is_a_refusal_not_a_crash(inst):
    g.run("INC0000059", chat=model())
    g._path("INC0000059").write_text("", encoding="utf-8")               # an interrupted save
    shown, card = main.step("apply enrich INC0000059", [], None, CAPS, run=lambda *a, **k: pytest.fail("chat"))
    assert card is None and "the saved enrichment for INC0000059 can't be read" in shown[0]
    assert [p.name for p in g._path("INC0000059").parent.iterdir()] == ["INC0000059.json"]


def test_a_caller_that_is_not_a_sys_id_is_not_looked_up(inst):
    assert g.caller_context({"caller_id": {"value": "x^ORactive=true", "display": ""}}) == {}
    assert inst.queries("sys_user") == []
