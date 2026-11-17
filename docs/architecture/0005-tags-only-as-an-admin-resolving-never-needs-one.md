# 0005. Tags only as an admin; resolving never needs one

- **Status:** Accepted
- **Date:** 2026-11-17
- **Deciders:** Jody (owner: "sn_resolve shouldn't depend on the admin role"), agent

## Context

`resolve` and `apply enrich` tag the incident by inserting a `label_entry` row through the Table API
(`rio/tags.py`). As an admin that works. On the laptop the rio account is `itil`, and after #29
granted it `tags_admin`, `resolve INC0010357` still failed: the instance created the `label_entry`
row and stored every field blank (#30).

Measured on dev442835 (2026-11-17), impersonating the rio account in a server script:
`label_entry.canCreate()` is true, and `canCreate()`/`canWrite()` is false for every field. The OOTB
ACLs give `label_entry` create, read and delete rules but no table-level write rule, so only an admin
can set a field. The Table API keeps the row and drops the values without an error. The empty row
can't be deleted by the same account either, because the delete rule looks up `current.label`, which
is blank. The UI tags as a normal user through `LabelsAjax`, a server-side script that skips field
ACLs and needs a browser session. There is no stock REST endpoint for tagging.

So a tag written by a non-admin costs an orphan row that only an admin can clean up.

## Options considered

1. **Grant the rio account `admin`.** Rejected: it makes the least-privileged path the most
   privileged one, and the owner ruled it out.
2. **Add a `label_entry` write ACL on the instance** (for `tags_admin`, say). Rejected: it changes
   platform security for every user, through a global update set, so that one tool can tag. rio is
   meant to work against stock instances.
3. **A scoped tagging endpoint on the instance that rio calls.** A clean write path, but rio would
   then need an app installed before it can resolve anything. Worth revisiting if tags become
   required.
4. **Resolve first, then try the tags and report a failure.** Rejected: every non-admin resolve
   would leave another empty row.
5. **(chosen) Ask before any tag read or write whether rio's account holds `admin`, and tag only
   then.** `tags.can_tag()` reads the account's `sys_user` row by `user_name`, then
   `sys_user_has_role` for that user with the admin role's sys_id and `state=active`. It doesn't
   filter on `role.name=admin`: that is a dot-walk into `sys_user_role`, which a non-admin can't
   read, and a dot-walk on an unreadable target matches nothing.

## Decision

Option 5. A non-admin's resolve card says the tags won't be written, and why, and lists the tags
they would have been. It reads no `label` or `label_entry` row (a non-admin may not read `label`'s
fields at all, #29), and `y` writes the state alone. The enrich card does the same for its one tag.
An admin's path is unchanged: tags first, then the state, so a failed tag still leaves the incident
unresolved.

`y` does what the card said. A card that said "not tagged" never tags, even if the account became
an admin while it waited, and `y` repeats the card's reason. A card that said it would tag checks
again at `y`, before any write. If the account is no longer an admin, `y` is refused and nothing is
written; the operator asks again and gets a "not tagged" card (PR #31 review).

A failed check (the account can't read `sys_user.user_name`, a read error) answers "don't tag",
never "go ahead". Skipping a tag costs a missing tag; guessing wrong costs another empty row. The
rows that come back are checked in code (the user name, user, role and state), so a query term the
instance ignored can't make the wrong account, or any role, read as an admin.

## Consequences

- An `itil` account can resolve and enrich. `tags_admin` is no longer needed for either, so #29's
  workaround role can come off the rio account.
- An instance that adds its own `label_entry` write ACL for non-admins still gets no tags from a
  non-admin rio. Revisit (option 3, or a setting) if someone needs that.
- The admin role's sys_id (`2831a114c611228501d4ea6c309d626d`) is the OOTB one, the same on every
  instance. An instance that recreated `admin` under another sys_id would read as "not an admin":
  no tags, never an empty row.
