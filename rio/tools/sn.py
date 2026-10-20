# rio/tools/sn.py — the ServiceNow workflows carried from whitcortex, as rio tools (#1).
#
# They sit on the now-data verbs (rio.servicenow), so they load only when the now-data tools did:
# the same credentials, the same field check, and nothing to offer when the instance is off.

from __future__ import annotations

from rio.tools import TOOLS, register


def load() -> list[str]:
    """Register the workflow tools when now-data is up. Returns one banner line."""
    if "now_query" not in TOOLS:
        return ["record tools: off (they read through the now-data tools, which are off)"]
    from rio import servicenow

    @register("sn_record_status", group="servicenow", writes=False)
    def sn_record_status(number: str) -> str:
        """A status card for ANY ServiceNow task record by number (INC, CS, CHG, PRB, RITM, REQ, tasks). Reply with the card exactly as returned; do not reword it or drop its link.

        The card shows the number, type, state and priority, the short description, assignee and
        group, the latest work note or comment, and a link. Only this first paragraph reaches the
        model's menu (rio.tools.schemas), so the instruction is there (PR #24 review).

        Args:
          number: record number, e.g. INC0000059 or CHG0000023
        """
        return servicenow.render_status_card(servicenow.record_status(number))

    return ["record tools: sn_record_status (or type a record number)"]
