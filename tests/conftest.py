# tests/conftest.py — every test starts with an empty tool registry and rio writable.
#
# The registry is one module-level dict that other modules imported by name, so it is emptied and
# refilled in place rather than replaced.

import pytest

from rio import config, tools


@pytest.fixture(autouse=True)
def empty_registry(monkeypatch):
    saved = dict(tools.TOOLS)
    tools.TOOLS.clear()
    monkeypatch.setattr(config, "READ_ONLY", False)
    yield tools.TOOLS
    tools.TOOLS.clear()
    tools.TOOLS.update(saved)
