# tests/test_approval.py — the card runs exactly the call it showed, once, and only on "y".

import pytest

from rio import approval
from rio.tools import register
from rio.tools.errors import ToolError


@pytest.fixture
def note_tool():
    ran = []

    def preview(number: str, text: str) -> str:
        if number == "INC404":
            raise ToolError("no incident INC404")
        return f"  would add a work note to {number}: {text!r}"

    @register("add_note", group="sn", writes=True, preview=preview)
    def add_note(number: str, text: str) -> str:
        ran.append((number, text))
        return f"noted on {number}"

    return ran


def test_the_card_shows_the_preview_and_runs_nothing(note_tool):
    card = approval.park("add_note", {"number": "INC1", "text": "checked"})
    assert isinstance(card, approval.Card)
    assert "would add a work note to INC1" in card.render()
    assert note_tool == []


@pytest.mark.parametrize("yes", ["y", "Y", "yes", " yes. "])
def test_y_runs_exactly_that_call_once(note_tool, yes):
    card = approval.park("add_note", {"number": "INC1", "text": "checked"})
    assert approval.answer(card, yes) == "noted on INC1"
    assert note_tool == [("INC1", "checked")]


@pytest.mark.parametrize("other", ["n", "no", "", "yes please change INC2 instead", "approve", "sure"])
def test_anything_but_y_runs_nothing(note_tool, other):
    card = approval.park("add_note", {"number": "INC1", "text": "checked"})
    assert approval.answer(card, other) == "Not done: nothing was changed."
    assert note_tool == []


def test_a_call_that_cannot_work_is_not_parked(note_tool):
    out = approval.park("add_note", {"number": "INC404", "text": "x"})
    assert out == "Tool error (add_note): no incident INC404"
    assert "bad arguments" in approval.park("add_note", {"numbr": "INC1"})


def test_a_read_cannot_be_parked():
    register("read_it", group="sn", writes=False)(lambda: "ok")
    assert "not a write tool" in approval.park("read_it", {})
