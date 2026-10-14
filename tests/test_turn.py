# tests/test_turn.py — one tool round per turn; reads run, a write stops at a card and runs nothing.
#
# `chat` is a fake that returns scripted Replies and records what it was sent; no socket opens.

import pytest

from rio import turn
from rio.llm import Reply
from rio.tools import register

CAPS = frozenset({"completion", "tools"})


class FakeChat:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, messages, tools=None, think=None):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        return self.replies.pop(0)


def _call(name, **args):
    return {"function": {"name": name, "arguments": args}}


@pytest.fixture
def instance():
    state = {"queried": [], "written": []}

    @register("now_query", group="sn", writes=False)
    def now_query(table: str, query: str = "") -> dict:
        """Find records."""
        state["queried"].append((table, query))
        return {"count": 1, "records": [{"number": "INC0010090", "state": "New"}]}

    @register("now_update", group="sn", writes=True,
              preview=lambda table, sys_id, values: f"  {sys_id}: state -> {values.get('state')}")
    def now_update(table: str, sys_id: str, values: dict) -> dict:
        """Change a record."""
        state["written"].append((table, sys_id, values))
        raise AssertionError("a write ran inside a model turn")

    return state


def test_a_plain_answer_is_one_call(instance):
    chat = FakeChat(Reply(content="Hello."))
    history = [{"role": "user", "content": "hi"}]
    t = turn.run(history, chat=chat, model_caps=CAPS)
    assert (t.ok, t.text, t.card) == (True, "Hello.", None)
    assert len(chat.calls) == 1 and chat.calls[0]["tools"]          # the menu was offered
    assert history[-1] == {"role": "assistant", "content": "Hello."}


def test_a_read_runs_and_a_second_call_answers_from_it_without_the_menu(instance):
    chat = FakeChat(Reply(tool_calls=[_call("now_query", table="incident", query="active=true")]),
                    Reply(content="One open incident: INC0010090."))
    history = [{"role": "user", "content": "what's open?"}]
    t = turn.run(history, chat=chat, model_caps=CAPS)
    assert instance["queried"] == [("incident", "active=true")]
    assert t.tools_run == ["now_query"] and t.text == "One open incident: INC0010090."
    assert chat.calls[1]["tools"] is None                            # one tool round per turn
    tool_msg = chat.calls[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_name"] == "now_query"
    assert "INC0010090" in tool_msg["content"]


def test_a_write_parks_on_a_card_ends_the_turn_and_runs_nothing(instance):
    chat = FakeChat(Reply(tool_calls=[_call("now_update", table="incident", sys_id="abc", values={"state": "2"})]))
    history = [{"role": "user", "content": "set INC0010090 to in progress"}]
    t = turn.run(history, chat=chat, model_caps=CAPS)
    assert instance["written"] == []
    assert t.card is not None and t.card.tool == "now_update" and t.card.args["sys_id"] == "abc"
    assert "APPROVAL NEEDED: now_update" in t.text and "abc: state -> 2" in t.text
    assert len(chat.calls) == 1                                      # no summary turn after a card
    assert "has not run" in history[-1]["content"]


def test_only_the_first_write_in_a_turn_gets_a_card(instance):
    chat = FakeChat(Reply(tool_calls=[
        _call("now_update", table="incident", sys_id="a", values={"state": "2"}),
        _call("now_update", table="incident", sys_id="b", values={"state": "6"})]))
    history = [{"role": "user", "content": "update both"}]
    t = turn.run(history, chat=chat, model_caps=CAPS)
    assert t.card.args["sys_id"] == "a"
    assert history[-1]["content"].startswith("Not run: one write per turn")
    assert instance["written"] == []


def test_string_arguments_are_parsed_and_bad_ones_reach_the_model_as_an_error(instance):
    chat = FakeChat(Reply(tool_calls=[{"function": {"name": "now_query", "arguments": '{"table": "incident"}'}},
                                      {"function": {"name": "now_query", "arguments": "not json"}}]),
                    Reply(content="done"))
    history = [{"role": "user", "content": "q"}]
    turn.run(history, chat=chat, model_caps=CAPS)
    assert instance["queried"] == [("incident", "")]
    assert "must be a JSON object" in history[-2]["content"]


def test_a_model_without_tools_gets_no_menu(instance):
    chat = FakeChat(Reply(content="I can't look that up."))
    turn.run([{"role": "user", "content": "q"}], chat=chat, model_caps=frozenset({"completion"}))
    assert chat.calls[0]["tools"] is None


def test_a_failed_model_call_is_reported_not_raised(instance):
    chat = FakeChat(Reply(ok=False, error="Ollama did not answer"))
    t = turn.run([{"role": "user", "content": "q"}], chat=chat, model_caps=CAPS)
    assert (t.ok, t.text) == (False, "(no reply) Ollama did not answer")
