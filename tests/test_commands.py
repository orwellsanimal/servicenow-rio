# tests/test_commands.py — what rio answers itself, before any model turn (#1, whitcortex #2643).

import pytest

from rio import __main__ as main, approval, commands, config
from rio.tools import register

pytest.importorskip("now_data.verbs")

from fake_instance import FakeInstance  # noqa: E402
from rio.tools import sn  # noqa: E402
from test_status_card import BASE, tables  # noqa: E402

CAPS = frozenset({"completion", "tools"})


@pytest.mark.parametrize("text, number", [
    ("INC0000059", "INC0000059"),
    ("  inc0000059 ", "INC0000059"),
    ("status INC0000059", "INC0000059"),
    ("Status of chg0000023.", "CHG0000023"),
    ("sn status for PRB0000050!", "PRB0000050"),
    ("RITM0010001", "RITM0010001"),
    # a hex id that happens to start with two letters is read as a record number; the cost is one
    # "not found" refusal (whitcortex PR #2645 review): pinned, not prevented
    ("ab123456", "AB123456"),
])
def test_the_command_is_recognised(text, number):
    assert commands.status_number(text) == number


@pytest.mark.parametrize("text", [
    "what is the status of INC0000059",      # a sentence: the model's job
    "INC0000059 is still open",
    "status",
    "status 5f13027f",                        # a git sha, not a record
    "INC59",                                  # too few digits
    "",
])
def test_anything_else_is_not_the_command(text):
    assert commands.status_number(text) is None


@pytest.fixture
def instance(monkeypatch):
    monkeypatch.setattr(config, "sn_instance_url", lambda: BASE)
    inst = FakeInstance(tables()).install(monkeypatch)

    @register("now_query", group="servicenow", writes=False)
    def now_query(table: str) -> dict:
        """stand-in for the real now_query"""
        return {}

    sn.load()
    return inst


def no_model(history, **kw):
    pytest.fail("the model was called for an operator command")


def test_a_bare_number_is_the_card_with_no_model_turn(instance):
    history = [{"role": "system", "content": "s"}]
    shown, card = main.step("INC0000059", history, None, CAPS, run=no_model)
    assert card is None
    assert shown[0].startswith("rio> INC0000059 · Incident · New")
    card_text = shown[0][len("rio> "):].rstrip("\n")
    # bracketed: a work note quoted in the card is the record's text, not something rio said
    assert history[1:] == [{"role": "user", "content": "INC0000059"},
                           {"role": "assistant", "content": f"[sn_record_status INC0000059] {card_text}"}]


def test_a_refusal_is_said_and_kept_so_a_follow_up_has_context(instance):
    """whitcortex kept both rows (PR #2616): "why not?" after a refusal needs the refusal."""
    history = [{"role": "system", "content": "s"}]
    shown, _ = main.step("status INC9999999", history, None, CAPS, run=no_model)
    assert shown == ["rio> No status card for INC9999999: Tool error (sn_record_status): "
                     "No task record numbered INC9999999.\n"]
    assert history[-1]["content"] == ("[sn_record_status INC9999999] No status card for INC9999999: "
                                      "Tool error (sn_record_status): No task record numbered INC9999999.")


def test_with_the_instance_off_the_command_says_why(monkeypatch):
    shown, _ = main.step("INC0000059", [], None, CAPS, run=no_model)
    assert shown == ["rio> No status card for INC0000059: the record tools are off (see the startup banner).\n"]


def test_what_reaches_the_screen_has_no_control_characters():
    """The card cleans its own fields; this is the net under every other route: a model reply
    quoting a now_query result, a card's args, a tool error (PR #24 review)."""
    out = main.screen("rio> said \x1b[2Jhi\x9b31m \ud83d\nnext\ttab\r")
    assert out == "rio> said  [2Jhi 31m \ufffd\nnext tab "
    out.encode("utf-8")                                              # no lone surrogate left


def test_a_sentence_goes_to_the_model(instance):
    from rio.turn import Turn
    seen = []
    shown, _ = main.step("what changed on INC0000059?", [], None, CAPS,
                         run=lambda h, **kw: seen.append(h[-1]) or Turn(text="it moved"))
    assert seen == [{"role": "user", "content": "what changed on INC0000059?"}]
    assert shown[0].startswith("rio> it moved")


def test_a_number_typed_at_a_card_declines_it_and_shows_the_record(instance):
    """The card answers only the input right after it (ADR-0003); anything but yes runs nothing,
    and that input is still read as a new message."""
    ran = []

    def write_preview(**kw):
        return "  would write"

    @register("now_update", group="servicenow", writes=True, preview=write_preview)
    def now_update(table: str, sys_id: str, values: str) -> dict:
        """stand-in write"""
        ran.append(1)
        return {}

    parked = approval.park("now_update", {"table": "incident", "sys_id": "x", "values": "{}"})
    history = []
    shown, card = main.step("INC0000059", history, parked, CAPS, run=no_model)
    assert ran == [] and card is None
    assert shown[0] == "rio> Not done: nothing was changed.\n"
    assert shown[1].startswith("rio> INC0000059 · Incident")
    assert history[0]["content"].startswith("[now_update declined]")


def test_main_prints_everything_through_screen(monkeypatch, capsys):
    """PR #24 round 2: screen() being right proves nothing unless main() uses it, for the
    banner's error text and every reply alike."""
    monkeypatch.setattr(main, "capabilities", lambda: CAPS)
    monkeypatch.setattr(main.now, "load", lambda: ["now-data tools: off (\x1b]0;owned\x07)"])
    monkeypatch.setattr(main.sn, "load", lambda: ["record tools: \x9b2J"])
    inputs = iter(["INC0000059", "/quit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    monkeypatch.setattr(main, "step", lambda text, history, card, caps: (["rio> \x1b[31mred \ud83d\n"], None))
    assert main.main([]) == 0
    out = capsys.readouterr().out
    assert "now-data tools: off ( ]0;owned )" in out and "rio>  [31mred \ufffd" in out
    assert not any(ord(ch) < 0x20 and ch != "\n" or 0x7f <= ord(ch) <= 0x9f for ch in out)
