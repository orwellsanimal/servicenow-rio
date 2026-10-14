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


def test_an_empty_reply_is_reported_and_left_out_of_history(instance):
    # What Ollama 0.23.2 returns when it can't parse a tool call qwen3 generated.
    history = [{"role": "user", "content": "change it"}]
    t = turn.run(history, chat=FakeChat(Reply(content="")), model_caps=CAPS)
    assert not t.ok and t.text.startswith("(no reply) the model returned neither text nor a tool call")
    assert history == [{"role": "user", "content": "change it"}]


def test_an_empty_answer_after_a_read_is_reported(instance):
    chat = FakeChat(Reply(tool_calls=[_call("now_query", table="incident")]), Reply(content="  "))
    t = turn.run([{"role": "user", "content": "q"}], chat=chat, model_caps=CAPS)
    assert not t.ok and t.tools_run == ["now_query"]
    assert t.text == ("(no reply) the model returned no text after reading with now_query; "
                      "nothing was changed. Try asking again.")


def test_an_empty_answer_after_only_a_refused_write_says_nothing_ran(instance):
    # The write was refused before a card (no preview possible), so no read ran (#12 review).
    @register("bad_write", group="sn", writes=True, preview=lambda: (_ for _ in ()).throw(ValueError("no")))
    def bad_write() -> str:
        raise AssertionError("must not run")

    chat = FakeChat(Reply(tool_calls=[_call("bad_write")]), Reply(content=""))
    t = turn.run([{"role": "user", "content": "q"}], chat=chat, model_caps=CAPS)
    assert (t.ok, t.card, t.tools_run) == (False, None, [])
    assert t.text == "(no reply) the model returned no text; nothing ran or was changed. Try asking again."


# ── A tool call written out as text (#8) ───────────────────────────────────────

# What qwen3:14b printed on the first live run (RIO_READ_ONLY=1): the second call has no menu, and the
# model answered with a write and a "card" from a tool that doesn't exist.
SEEN_ON_8 = ('{"name": "now_update", "arguments": {"sys_id":"0c5f","table":"incident","short_description":"x"}}\n'
             '{"name": "now_card", "arguments": {"message": "Update incident record", "title": "Update", "type": "confirm"}}')


def test_written_out_calls_after_a_read_are_replaced_by_a_note_and_the_turn_is_dropped(instance):
    chat = FakeChat(Reply(tool_calls=[_call("now_query", table="incident")]), Reply(content=SEEN_ON_8))
    history = [{"role": "user", "content": "update it"}]
    t = turn.run(history, chat=chat, model_caps=CAPS)
    assert not t.ok and t.card is None and instance["written"] == []
    assert t.text == ("(After reading with now_query, the model wrote out a call to now_update, now_card as text "
                      "instead of making it. It did not run, and nothing was changed. now_card is not a tool rio "
                      "offers here. Ask again to have a tool run.)")
    assert '"name"' not in t.text


def test_prose_around_a_written_out_call_is_kept_and_only_the_prose_enters_history(instance):
    reply = ("INC0010090 is New.\n\n<tool_call>\n{\n  \"name\": \"now_update\",\n  \"arguments\": {\"sys_id\": \"abc\"}\n}\n"
             "</tool_call>\n\nShall I set it to In Progress?")
    chat = FakeChat(Reply(tool_calls=[_call("now_query", table="incident")]), Reply(content=reply))
    history = [{"role": "user", "content": "what about INC0010090?"}]
    t = turn.run(history, chat=chat, model_caps=CAPS)
    assert t.ok
    assert t.text.startswith("INC0010090 is New.\n\nShall I set it to In Progress?\n\n(After reading with now_query")
    assert history[-1] == {"role": "assistant", "content": "INC0010090 is New.\n\nShall I set it to In Progress?"}
    assert "now_update" in t.text and "not a tool rio offers" not in t.text   # now_update is on this menu


def test_a_written_out_call_in_the_first_reply_is_caught_too(instance):
    reply = '```json\n{"name": "now_query", "parameters": {"table": "incident"}}\n```'
    history = [{"role": "user", "content": "q"}]
    t = turn.run(history, chat=FakeChat(Reply(content=reply)), model_caps=CAPS)
    assert not t.ok and instance["queried"] == []
    assert t.text.startswith("(The model wrote out a call to now_query as text instead of making it.")
    assert history == [{"role": "user", "content": "q"}]


@pytest.mark.parametrize("reply", [
    'The record is {"number": "INC0010090", "state": "New"}.',                       # JSON, but not a call
    "A tool call looks like this:\n```json\n{\"table\": \"incident\"}\n```",          # a fence, no call
    "Use the {braces} carefully: { not json",                                        # stray braces
])
def test_ordinary_replies_with_braces_or_fences_are_left_alone(instance, reply):
    history = [{"role": "user", "content": "q"}]
    t = turn.run(history, chat=FakeChat(Reply(content=reply)), model_caps=CAPS)
    assert (t.ok, t.text) == (True, reply.strip())
    assert history[-1] == {"role": "assistant", "content": reply}


def test_under_read_only_a_written_out_write_is_explained_as_read_only(instance, monkeypatch):
    # The live repro of #8: RIO_READ_ONLY hides now_update from the menu, so the model writes it out.
    from rio import config
    monkeypatch.setattr(config, "READ_ONLY", True)
    reply = '{"name": "now_update", "arguments": {"table": "incident", "sys_id": "0c5f", "values": {"short_description": "x"}}}'
    chat = FakeChat(Reply(tool_calls=[_call("now_query", table="incident")]), Reply(content=reply))
    t = turn.run([{"role": "user", "content": "change it"}], chat=chat, model_caps=CAPS)
    assert "now_update" not in {m["function"]["name"] for m in chat.calls[0]["tools"]}
    assert not t.ok and instance["written"] == []
    assert t.text == ("(After reading with now_query, the model wrote out a call to now_update as text instead of "
                      "making it. It did not run, and nothing was changed. rio is read-only here (RIO_READ_ONLY), "
                      "so now_update isn't offered.)")


def test_a_real_code_block_next_to_a_written_out_call_keeps_its_fences(instance):
    # #19 review: stripping every fence turned ```sql into a stray "sql" line.
    reply = ('Here is the query I used:\n```sql\nSELECT * FROM incident\n```\n'
             '```json\n{"name": "now_update", "arguments": {"sys_id": "a"}}\n```')
    history = [{"role": "user", "content": "q"}]
    t = turn.run(history, chat=FakeChat(Reply(content=reply)), model_caps=CAPS)
    assert t.ok
    assert history[-1]["content"] == "Here is the query I used:\n```sql\nSELECT * FROM incident\n```"
    assert t.text.startswith("Here is the query I used:\n```sql\nSELECT * FROM incident\n```\n\n(The model wrote out")


@pytest.mark.parametrize("reply", [
    '[{"name": "now_update", "arguments": {"sys_id": "a"}}, {"name": "now_card", "parameters": {}}]',  # a list: no "[]" left
    '{"type": "function", "function": {"name": "now_update", "arguments": {"sys_id": "a"}}}',          # the OpenAI shape
])
def test_lists_of_calls_and_the_openai_shape_are_cut_whole(instance, reply):
    history = [{"role": "user", "content": "q"}]
    t = turn.run(history, chat=FakeChat(Reply(content=reply)), model_caps=CAPS)
    assert not t.ok and "[" not in t.text and "now_update" in t.text
    assert history == [{"role": "user", "content": "q"}]


def test_a_model_without_tools_is_told_why_no_tool_is_offered(instance):
    reply = '{"name": "now_query", "arguments": {"table": "incident"}}'
    t = turn.run([{"role": "user", "content": "q"}], chat=FakeChat(Reply(content=reply)),
                 model_caps=frozenset({"completion"}))
    assert t.text == ("(The model wrote out a call to now_query as text instead of making it. It did not run, "
                      "and nothing was changed. rio offers no tools here: this model does not list the tools "
                      "capability.)")


@pytest.mark.parametrize("reply", [
    '{"tool": "now_update", "args": {"sys_id": "a"}}',          # whitcortex's text protocol: not caught, on purpose
    '[{"name": "now_update", "arguments": {}}, 3]',              # a list that isn't only calls
])
def test_shapes_left_alone_on_purpose(instance, reply):
    t = turn.run([{"role": "user", "content": "q"}], chat=FakeChat(Reply(content=reply)), model_caps=CAPS)
    assert (t.ok, t.text) == (True, reply)


@pytest.mark.parametrize("reply", [
    '{"name": "now_update", "arguments": {"sys_id": "a"}}\n</tool_call>',   # seen live: a close it never opened
    '<tool_call>\n{"name": "now_update", "arguments": {"sys_id": "a"}}',    # and the mirror image
    '```json\n<tool_call>\n{"name": "now_update", "arguments": {}}\n</tool_call>\n```',
])
def test_a_lone_tool_call_tag_beside_a_call_goes_with_it(instance, reply):
    history = [{"role": "user", "content": "q"}]
    t = turn.run(history, chat=FakeChat(Reply(content=reply)), model_caps=CAPS)
    assert not t.ok and "tool_call>" not in t.text and "```" not in t.text, t.text
    assert history == [{"role": "user", "content": "q"}]
