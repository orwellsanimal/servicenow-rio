# rio/tools/sn.py — the ServiceNow workflows carried from whitcortex, as rio tools (#1).
#
# They sit on the now-data verbs (rio.servicenow), so they load only when the now-data tools did:
# the same credentials, the same field check, and nothing to offer when the instance is off.

from __future__ import annotations

from rio import config
from rio.tools import TOOLS, register


def load() -> list[str]:
    """Register the workflow tools when now-data is up. Returns one banner line."""
    if "now_query" not in TOOLS:
        return ["record tools: off (they read through the now-data tools, which are off)"]
    from rio import servicenow, triage

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

    @register("sn_triage", group="servicenow", writes=False)
    def sn_triage(number: str) -> str:
        """Triage an incident (DRY RUN): propose its category, CI and assignment group; changes nothing. Reply with the proposal as returned. Only the operator can apply it, by typing `apply triage <number>`; you cannot.

        Every value is chosen from what the instance offers and backed by a quote from the ticket,
        or reported as not established (whitcortex #2597, #2631). The instructions are in the first
        paragraph because only it reaches the model's menu.

        Args:
          number: incident number, e.g. INC0010023
        """
        return triage.run(number)["note"]

    # The model never sees this one (#2614): `apply triage <number>` parks its card (rio.commands).
    @register("sn_triage_apply", group="servicenow", writes=True, preview=triage.preview,
              operator_only=True)
    def sn_triage_apply(number: str, proposal: str = "") -> str:
        """Apply the saved triage proposal for an incident, if it hasn't changed since.

        Args:
          number: incident number, e.g. INC0010023
          proposal: the saved proposal's id, which the card shows and `y` must match
        """
        return triage.apply(number, proposal)

    return ["record tools: sn_record_status (or type a record number), sn_triage (or type "
            "`triage <INC>`)" + ("" if config.READ_ONLY else "; `apply triage <INC>` applies one")]

