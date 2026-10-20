# tests/conftest.py — every test starts with an empty tool registry and rio writable, and none can
# open a connection.
#
# The registry is one module-level dict that other modules imported by name, so it is emptied and
# refilled in place rather than replaced.

import pytest
import requests

from rio import config, tools


@pytest.fixture(autouse=True)
def empty_registry(monkeypatch):
    saved = dict(tools.TOOLS)
    tools.TOOLS.clear()
    monkeypatch.setattr(config, "READ_ONLY", False)
    yield tools.TOOLS
    tools.TOOLS.clear()
    tools.TOOLS.update(saved)


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    """CLAUDE.md: the suite must not open a socket to Ollama or the instance. A test that reaches the
    network (a default argument bound to the real client, say) fails here at once instead of waiting
    on a timeout; pytest.fail is a BaseException, so no `except Exception` on the way swallows it.
    Tests that stub requests.post themselves never get this far."""
    def refuse(self, request, **kw):
        pytest.fail(f"the test opened a connection: {request.method} {request.url}")
    monkeypatch.setattr(requests.Session, "send", refuse)


@pytest.fixture(autouse=True)
def no_raw_sockets(monkeypatch):
    """The same for anything below requests: now-data's ensure_awake() opens its own urllib
    connection and can wait 120 s (PR #25 review, round 2)."""
    import socket

    def refuse(self, address):
        pytest.fail(f"the test opened a socket to {address}")
    monkeypatch.setattr(socket.socket, "connect", refuse)
