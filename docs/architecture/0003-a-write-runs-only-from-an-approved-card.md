# 0003. A write runs only from an approved card, and every tool declares whether it writes

- **Status:** Accepted
- **Date:** 2026-10-13
- **Deciders:** Jody (operator: corporate instance read-only, lab instance writable), agent

## Context

Rio's tools reach a ServiceNow instance with the operator's own account. whitcortex learned the cost
of a chat model with write tools the hard way: the model called write tools unasked three times in
two days (whitcortex #2609, #2614, #2618), and an approval that was itself a model turn ran nothing
for three months (#2621). Its answer (ADR-0129/0130 there) was a card: the write parks, the next
message answers it, and "approve" runs exactly the parked call with no model turn in between.

Rio carries that, for a terminal with one conversation, and adds a constraint the lab didn't have:
on the corporate instance rio must not change anything at all.

## Options considered

1. **whitcortex's shape verbatim: write tools listed in a set beside the registry, plus a test that
   every tool in the group is classified.** It works, but the classification lives apart from the
   tool, and the guarantee depends on a test per group.
2. **A model-turn approval ("are you sure?").** Rejected for the reason #2621 records: a model turn
   between the operator's yes and the write is where unasked writes come from.
3. **(chosen) `register(..., writes=...)` with no default, and `execute_tool(..., approved=False)`.**
   whitcortex rejected a `writes` flag (ADR-0129) only because it would have been a registry-wide
   change across dozens of existing tools. Rio's registry is new, so the cost doesn't apply, and the
   flag makes "forgot to classify" a `TypeError` at import rather than a test failure.

## Decision

- Every tool says `writes=True` or `writes=False` when it registers. A write must also supply a
  `preview`, the tool's own dry run, which the card shows. For `now_update` that's now-data's
  `confirmed=False` diff.
- `execute_tool` is the only dispatcher. It refuses a write unless called with `approved=True`, and
  only `rio.approval.answer` passes that, for the exact tool and args on the card. A caller that
  forgets to check `is_write` before dispatching still can't run a write.
- A turn runs at most one round of tool calls. The first write in it parks on a card and ends the
  turn, with no summary turn. The next input answers the card: `y`/`yes` runs the call once, and
  anything else runs nothing.
- `RIO_READ_ONLY=true` takes write tools off the menu **and** refuses them in `execute_tool` even when
  approved. The corporate instance runs with it set.
- `now_run_script` is not offered at all.

## Consequences

- **Positive:** an unasked write can't reach the instance from a model turn, by construction, and
  read-only is enforced in code rather than by the prompt. The card shows a real diff (including
  fields that don't exist), so the operator approves a change, not a description of one.
- **Negative:** a write that needs a lookup first (a number to a sys_id) takes two turns, because a
  turn has one tool round. That's measured in #3, along with the cost of a second round on the laptop.
- **Resolved (operator, 2026-10-19, rio#1):** `apply triage <INC>` does **not** bypass the card. The
  typed command parks a card that shows `now_update`'s real diff of the saved proposal, and only `y`
  applies it. The card is one confirmation more than in the lab, and it shows the actual field changes
  rather than the command's intent. The tool it runs is registered `operator_only=True`. That keeps it
  off the menu, and `park()` refuses it unless an operator command asks (`operator=True`), so a model
  that names it anyway gets no card (whitcortex #2614: the model called the old apply tool unasked).
