# tests/test_approval.py — the card runs exactly the call it showed, once, and only on "y".

import pytest

from rio import approval, config
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


def test_read_only_makes_no_card_and_never_previews(monkeypatch):
    calls = []

    def preview(number: str) -> str:
        calls.append(("preview", number))
        return "  would change it"

    @register("fake_update", group="sn", writes=True, preview=preview)
    def fake_update(number: str) -> str:
        calls.append(("write", number))
        return "changed"

    monkeypatch.setattr(config, "READ_ONLY", True)
    out = approval.park("fake_update", {"number": "abc"})
    assert out == "Tool error (fake_update): rio is read-only here (RIO_READ_ONLY); nothing was changed."
    assert calls == []


@pytest.mark.parametrize(
    "result, label",
    [
        ("Not done: nothing was changed.", "declined"),
        ("Tool error (add_note): no incident INC404", "approved, failed"),
        ("Tool error (add_note): rio is read-only here (RIO_READ_ONLY); nothing was changed.", "approved, failed"),
        ("noted on INC1", "approved and run"),
    ],
)
def test_outcome_labels_a_failed_yes_as_failed(result, label):
    assert approval.outcome(result) == label


def test_a_yes_whose_call_fails_is_labelled_failed():
    # The preview found the record; by the time of the "y" it is gone.
    @register("close_it", group="sn", writes=True, preview=lambda number: f"  would close {number}")
    def close_it(number: str) -> str:
        raise ToolError(f"no incident {number}")

    card = approval.park("close_it", {"number": "INC1"})
    result = approval.answer(card, "y")
    assert result == "Tool error (close_it): no incident INC1"
    assert approval.outcome(result) == "approved, failed"
