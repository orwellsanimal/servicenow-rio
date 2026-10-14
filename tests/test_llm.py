# tests/test_llm.py — the client sends only what the model supports, and never a socket from here.
#
# requests.post is stubbed at the client's edge; the fake answers /api/show with the capabilities
# the test chooses and /api/chat with a canned reply, recording every body it was sent.

import json

import pytest
import requests

from rio import config, llm


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code, self._payload, self.text = status_code, payload, text or json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise json.JSONDecodeError("no json", "", 0)
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class FakeOllama:
    def __init__(self, caps=("completion", "tools", "thinking"), reply=None, status=200):
        self.caps, self.reply, self.status, self.calls = list(caps), reply, status, []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url.rsplit("/", 2)[-2] + "/" + url.rsplit("/", 1)[-1], json))
        if url.endswith("/api/show"):
            if json["model"] == "missing:1b":
                return FakeResponse(404, {"error": "model not found"})
            return FakeResponse(200, {"capabilities": self.caps})
        if url.endswith("/api/chat"):
            if self.status != 200:
                return FakeResponse(self.status, None, text="model does not support thinking")
            return FakeResponse(200, self.reply or {
                "model": json["model"], "message": {"role": "assistant", "content": "hello", "tool_calls": []},
                "eval_count": 20, "eval_duration": 4_000_000_000, "total_duration": 5_000_000_000})
        raise AssertionError(url)


@pytest.fixture
def ollama(monkeypatch):
    fake = FakeOllama()
    monkeypatch.setattr(requests, "post", fake.post)
    monkeypatch.setattr(llm, "_CAPABILITIES", {})
    monkeypatch.setattr(config, "MODEL", "qwen3:14b")
    return fake


def _chat_bodies(fake):
    return [body for path, body in fake.calls if path == "api/chat"]


def test_a_reply_carries_the_measured_decode_rate(ollama):
    reply = llm.chat([{"role": "user", "content": "hi"}], think=False)
    assert reply.ok and reply.content == "hello" and reply.model == "qwen3:14b"
    assert reply.tokens_per_s == 5.0 and reply.seconds == 5.0


def test_think_is_sent_only_to_a_model_that_lists_thinking(ollama):
    llm.chat([{"role": "user", "content": "hi"}], think=False)
    assert _chat_bodies(ollama)[-1]["think"] is False
    ollama.caps = ["completion", "tools"]
    llm.capabilities(refresh=True)
    llm.chat([{"role": "user", "content": "hi"}], think=False)
    assert "think" not in _chat_bodies(ollama)[-1]


def test_tools_are_refused_with_a_reason_when_the_model_has_none(ollama):
    ollama.caps = ["completion"]
    reply = llm.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {"name": "x"}}])
    assert not reply.ok and "does not list the 'tools' capability" in reply.error
    assert _chat_bodies(ollama) == []          # nothing was sent


def test_num_ctx_and_num_predict_are_sent_and_num_thread_never(ollama):
    llm.chat([{"role": "user", "content": "hi"}])
    options = _chat_bodies(ollama)[-1]["options"]
    assert options == {"num_ctx": config.NUM_CTX, "num_predict": config.NUM_PREDICT}


def test_a_missing_model_is_named_with_the_pull_command(ollama):
    reply = llm.chat([{"role": "user", "content": "hi"}], model="missing:1b")
    assert not reply.ok and "ollama pull missing:1b" in reply.error


def test_a_non_200_is_a_reply_not_an_exception(ollama):
    ollama.status = 400
    reply = llm.chat([{"role": "user", "content": "hi"}])
    assert not reply.ok and reply.error.startswith("Ollama answered 400:")


def test_capabilities_are_cached_per_model(ollama):
    llm.capabilities()
    llm.capabilities()
    assert sum(1 for path, _ in ollama.calls if path == "api/show") == 1


def test_a_structured_format_is_passed_through(ollama):
    schema = {"type": "object", "properties": {"terms": {"type": "array"}}}
    llm.chat([{"role": "user", "content": "hi"}], format=schema)
    assert _chat_bodies(ollama)[-1]["format"] == schema


def test_tool_calls_are_returned(ollama):
    ollama.reply = {"model": "qwen3:14b", "message": {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "sn_record_status", "arguments": {"number": "INC0000059"}}}]},
                    "eval_count": 5, "eval_duration": 1_000_000_000, "total_duration": 1_000_000_000}
    reply = llm.chat([{"role": "user", "content": "INC0000059"}], tools=[{"type": "function", "function": {"name": "sn_record_status"}}])
    assert reply.tool_calls[0]["function"]["name"] == "sn_record_status"
