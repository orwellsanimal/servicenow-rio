# rio/triage.py — propose one incident's category, CI and assignment group on local inference (#1).
#
# Carried from whitcortex routines/graphs/sn_triage.py (#2597) and trimmed. The steps are the same;
# they run as a straight line, which needs no LangGraph:
#   fetch:    the incident as data (sys_ids, choice values), not text
#   gather:   CANDIDATES read from the instance on every run, never hand-listed: valid
#             category/subcategory pairs, the caller's CIs and the CIs the text names, and the groups
#             incidents are actually assigned to (whitcortex ADR-0128)
#   classify: ONE local model call whose JSON-schema ENUMS are those candidates, so the model can't
#             name a value the instance didn't offer; local only, no fallback (ADR-0002)
#   validate: code re-checks every pick against its candidates and every evidence quote against the
#             ticket text; anything else is "not established"
#   propose:  the proposal is shown and saved locally, and NOTHING is written. whitcortex's dry run
#             wrote a work note; here that would be a write from a model-called tool (ADR-0003), and
#             impossible under RIO_READ_ONLY (rio#1 plan of record, 2026-10-19).
# apply() later writes THAT proposal, not a fresh classification, and only from an approved card.
#
# Every read goes through the now-data verbs (rio.servicenow), so each one is field-checked first.
# Ticket text reaches one query (`nameIN`), and only through query_safe() (PR #25 review: a short
# description beginning `javascript:` would otherwise run on the instance).

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime

from now_data import verbs as data

from rio import config, llm
from rio.servicenow import (chunks, incident_record, is_sys_id, query_safe, record_number, rows, safe,
                            ticket_text)
from rio.tools.errors import ToolError
from rio.tools.now import call_verb

NONE = "none"               # the enum member meaning "no candidate fits"
MAX_CIS = 15                # CI candidates offered to the model
MIN_CI_NAME = 4             # shorter CI names match too much text to be evidence
CI_NAME_MAX = 255           # cmdb_ci.name's max_length (UI Meta, dev442835, 2026-10-19): longer can't match
GROUP_HISTORY = 500         # recent incidents read to learn which groups are used
MAX_GROUPS = 40             # groups offered, most used first: each is a 32-character id in the prompt
CHOICE_LIMIT = 1000         # now_query's own cap; a list this long may be incomplete
NAME_WORDS = 4              # the longest CI name looked for, in words
NAME_CHUNK = 100            # names per `nameIN` query, to keep the URL short
NAME_CHUNK_CHARS = 2000     # and characters: 100 long sequences of a pasted trace made 16.7 KB (PR #25)
ID_CHUNK = 100              # sys_ids per IN list, likewise
MAX_NAMES = 1000            # word sequences looked up at most (a pasted log would be thousands)
CI_ROWS = 200               # rows read per `nameIN` query (one name can be many CIs)
# Of num_ctx, what the prompt may use: the reply (num_predict) needs the rest. Ollama drops the head of
# a prompt that doesn't fit, silently, and the candidate lists are the bulk of it (PR #25 review).
PROMPT_SHARE = 0.75
CHARS_PER_TOKEN = 3         # conservative for ids and short labels; a sys_id is ~12 tokens


# ── Candidates (read from the instance) ──────────────────────────────────────

def category_pairs() -> tuple[list[dict], bool]:
    """Every valid category, alone or with a subcategory that DEPENDS on it; and whether a list hit
    the read cap (then some valid pairs may be missing, never an invalid one present)."""
    base = "name=incident^inactive=false^language=en"
    cats = rows("sys_choice", f"{base}^element=category", ("value", "label"), CHOICE_LIMIT)
    subs = rows("sys_choice", f"{base}^element=subcategory", ("value", "label", "dependent_value"),
                CHOICE_LIMIT)
    pairs = []
    for c in sorted(cats, key=lambda r: r["value"]["value"]):
        cv, cl = c["value"]["value"], c["label"]["value"]
        pairs.append({"key": cv, "category": cv, "subcategory": "", "label": cl})
        for s in sorted((s for s in subs if s["dependent_value"]["value"] == cv),
                        key=lambda r: r["value"]["value"]):
            sv = s["value"]["value"]
            pairs.append({"key": f"{cv}/{sv}", "category": cv, "subcategory": sv,
                          "label": f"{cl} / {s['label']['value']}"})
    return pairs, CHOICE_LIMIT in (len(cats), len(subs))


_TOKEN_EDGE = "\"'()[]{}<>.,;:!?"


def name_candidates(text: str) -> list[str]:
    """The word sequences of the ticket text that could be a CI's name, shortest first.

    whitcortex read every operational CI and searched the text for each name. That is the whole
    CMDB per triage, and now_query stops at 1000 rows; a corporate CMDB is far larger. So rio asks
    the other way round: which of the text's own 1-to-4-word sequences are CI names (rio#1 plan of
    record). Single words go first: a hostname is the commonest CI name, and with longest first a
    300-word ticket never got past its 2-word sequences before MAX_NAMES (PR #25 review). A sequence
    that isn't query_safe can't go into `nameIN`, and one longer than a CI name can be can't match."""
    words = [w.strip(_TOKEN_EDGE) for w in text.split()]
    words = [w for w in words if w]
    seen: set[str] = set()
    out: list[str] = []
    for n in range(1, NAME_WORDS + 1):
        for i in range(len(words) - n + 1):
            name = " ".join(words[i:i + n])
            if (not MIN_CI_NAME <= len(name) <= CI_NAME_MAX or name.lower() in seen
                    or not query_safe(name, in_list=True)):
                continue
            seen.add(name.lower())
            out.append(name)
    return out


def name_chunks(names: list[str]) -> list[list[str]]:
    """The names in `nameIN` lists of at most NAME_CHUNK names and NAME_CHUNK_CHARS characters."""
    out: list[list[str]] = []
    size = 0
    for name in names:
        if not out or len(out[-1]) >= NAME_CHUNK or size + len(name) + 1 > NAME_CHUNK_CHARS:
            out.append([])
            size = 0
        out[-1].append(name)
        size += len(name) + 1
    return out


def ci_candidates(record: dict) -> tuple[list[dict], bool]:
    """The current CI, the caller's CIs, then CIs named in the ticket text; and whether the text had
    more word sequences than were looked up."""
    text = ticket_text(record)
    found: dict[str, dict] = {}

    def add(sys_id, name, why):
        if sys_id and sys_id not in found and len(found) < MAX_CIS:
            found[sys_id] = {"sys_id": sys_id, "name": name, "why": why}

    current = record["cmdb_ci"]
    if current["value"]:
        add(current["value"], current["display"], "already set on the incident")
    caller = record["caller_id"]["value"]
    if is_sys_id(caller):
        for r in rows("cmdb_ci", f"assigned_to={caller}^operational_status=1", ("sys_id", "name"), MAX_CIS):
            add(r["sys_id"]["value"], r["name"]["value"], "assigned to the caller")
    names = name_candidates(text)
    named = []
    for chunk in name_chunks(names[:MAX_NAMES]):
        named += rows("cmdb_ci", f"operational_status=1^nameIN{','.join(chunk)}", ("sys_id", "name"), CI_ROWS)
    # `nameIN` matched a whole name under the instance's collation (case- and accent-insensitive on
    # MySQL-family instances); whitcortex's whole-word check against the text still decides.
    named = [r for r in named if len(r["name"]["value"].strip()) >= MIN_CI_NAME
             and re.search(rf"(?<!\w){re.escape(r['name']['value'].strip())}(?!\w)", text, re.I)]
    for r in sorted(named, key=lambda r: -len(r["name"]["value"])):   # most specific name first
        add(r["sys_id"]["value"], r["name"]["value"].strip(), "named in the ticket text")
    return list(found.values()), len(names) > MAX_NAMES


def most_used(groups: list[dict], cap: int | None) -> list[dict]:
    """At most `cap` groups, the most used, listed by name."""
    kept = sorted(groups, key=lambda g: -g["uses"])[:cap] if cap else groups
    return sorted(kept, key=lambda g: g["name"].lower())


_DEFAULT_CAP = object()


def group_candidates(cap=_DEFAULT_CAP) -> list[dict]:
    """Active, assignable groups WITH MEMBERS that incidents are actually assigned to, the most used
    first, at most `cap` (gather() caps after the assignee filter, PR #25 review).

    Measured on dev442835 (2026-10-10): 78 active groups, mostly approvers and Studio users;
    ServiceNow's `itil` type marks only 3 approval groups. What incidents really go to is the
    honest candidate set (whitcortex #2597). The member check is one grouped count per chunk of ids,
    where whitcortex asked each group in turn."""
    used: dict[str, int] = {}
    for r in rows("incident", "assignment_groupISNOTEMPTY^ORDERBYDESCsys_created_on",
                  ("assignment_group",), GROUP_HISTORY):    # "recent" means ordered (PR #2612 review)
        group = r["assignment_group"]["value"]
        if is_sys_id(group):
            used[group] = used.get(group, 0) + 1
    out = []
    for ids in chunks(list(used), ID_CHUNK):
        joined = ",".join(ids)
        groups = rows("sys_user_group", f"sys_idIN{joined}^active=true", ("sys_id", "name", "type"), len(ids))
        members = call_verb(data.now_aggregate, table="sys_user_grmember", query=f"groupIN{joined}",
                            group_by="group", count=True, display_value=False)
        for g in groups:
            sys_id, kind = g["sys_id"]["value"], g["type"]
            # A type this account can't read (a value, no display) may be Non-Assignable: left out,
            # not offered (PR #25 review: whitcortex's check passed it).
            if "Non-Assignable" in kind["display"] or (kind["value"] and kind["display"] == kind["value"]):
                continue
            if int((members.get(sys_id) or {}).get("count") or 0) > 0:
                out.append({"sys_id": sys_id, "name": g["name"]["value"], "why": "incidents are assigned here",
                            "uses": used.get(sys_id, 0)})
    return most_used(out, MAX_GROUPS if cap is _DEFAULT_CAP else cap)     # MAX_GROUPS as it is when called


def groups_the_assignee_is_in(groups: list[dict], assignee: str) -> list[dict]:
    """Only the groups the incident's current assignee belongs to (all of them when nobody is
    assigned).

    The instance's stock rule "Abort changes on group" (Incident Management Best Practice) looks up
    sys_user_grmember on (group, assigned_to) and aborts the WHOLE update when there is no row,
    category and CI with it (whitcortex #2613: INC0000019, 2026-10-11). This is the same lookup, so
    a group offered here passes the rule."""
    if not assignee or not groups:
        return groups
    if not is_sys_id(assignee):         # not a reference rio can look up: no group is known to pass
        return []
    mine: set[str] = set()
    for ids in chunks([g["sys_id"] for g in groups], ID_CHUNK):
        mine |= {r["group"]["value"] for r in rows("sys_user_grmember", f"user={assignee}^groupIN{','.join(ids)}",
                                                   ("group",), len(ids))}
    return [g for g in groups if g["sys_id"] in mine]


def gather(record: dict) -> dict:
    # Filter by the assignee, THEN cap: capped first, an assignee whose one group is rarely used had
    # it cut, and the note blamed them for being in no group (PR #25 review, round 2).
    usable = group_candidates(cap=None)
    filtered = groups_the_assignee_is_in(usable, record["assigned_to"]["value"])
    groups = most_used(filtered, MAX_GROUPS)
    categories, categories_capped = category_pairs()
    cis, names_capped = ci_candidates(record)
    return {"categories": categories, "cis": cis, "groups": groups,
            # True only when the ASSIGNEE filter removed groups, so the note blames the assignee
            # only when that is the cause (whitcortex #2613).
            "assignee_excluded_groups": len(filtered) < len(usable),
            "capped": [w for w, hit in (("category lists", categories_capped),
                                        ("CI names in the text", names_capped)) if hit]}


# ── The model step ───────────────────────────────────────────────────────────

def triage_schema(candidates: dict) -> dict:
    """The JSON schema whose enums ARE the candidates; `none` is always allowed."""
    def pick(values):
        return {"type": "object",
                "properties": {"value": {"type": "string", "enum": [*values, NONE]},
                               "evidence": {"type": "string"}},
                "required": ["value", "evidence"]}
    return {"type": "object",
            "properties": {"category": pick([p["key"] for p in candidates["categories"]]),
                           "cmdb_ci": pick([c["sys_id"] for c in candidates["cis"]]),
                           "assignment_group": pick([g["sys_id"] for g in candidates["groups"]])},
            "required": ["category", "cmdb_ci", "assignment_group"]}


def _prompt(record: dict, candidates: dict) -> list[dict]:
    def listing(items, key, label):
        return "\n".join(f"- {i[key]}: {i[label]}" + (f" ({i['why']})" if i.get("why") else "")
                         for i in items) or "- (none offered)"
    system = (
        "You triage one ServiceNow incident. For each field pick exactly one value from its list, "
        "or 'none' when nothing in the list fits. For each pick, 'evidence' must be words QUOTED "
        "from the ticket text that justify it (copy them exactly); use an empty string for 'none'. "
        "Do not guess: 'none' is a correct answer.")
    user = (
        f"Ticket {record['number']['value']}\n"
        f"Short description: {record['short_description']['value']}\n"
        f"Description: {record['description']['value']}\n\n"
        f"category (category or category/subcategory):\n"
        f"{listing(candidates['categories'], 'key', 'label')}\n\n"
        f"cmdb_ci (configuration item):\n{listing(candidates['cis'], 'sys_id', 'name')}\n\n"
        f"assignment_group:\n{listing(candidates['groups'], 'sys_id', 'name')}")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def classify(record: dict, candidates: dict, *, chat=None) -> dict:
    """One local call. Raises on a failed or unparseable reply, and before the call when the prompt
    and its schema can't fit num_ctx: no proposal follows either."""
    messages, schema = _prompt(record, candidates), triage_schema(candidates)
    size = (sum(len(m["content"]) for m in messages) + len(json.dumps(schema))) // CHARS_PER_TOKEN
    budget = int(config.NUM_CTX * PROMPT_SHARE)
    if size > budget:
        raise ToolError(f"the prompt is too long for num_ctx {config.NUM_CTX} (about {size} tokens, "
                        f"{budget} allowed): the ticket text is {len(ticket_text(record))} characters, with "
                        f"{len(candidates['categories'])} category pairs and {len(candidates['groups'])} "
                        "groups. Raise NUM_CTX, or triage by hand")
    reply = (chat or llm.chat)(messages, format=schema, think=False)
    if not reply.ok or not reply.content.strip():
        raise ToolError(f"the local model gave no usable reply ({reply.error or 'empty'})")
    try:
        return json.loads(reply.content)
    except json.JSONDecodeError as e:
        raise ToolError(f"the local model's reply was not JSON: {e}") from e


# ── Validation (code decides, the model only proposes) ───────────────────────

def _quoted(evidence: str, text: str) -> bool:
    squash = lambda s: re.sub(r"\s+", " ", s).strip().lower()   # noqa: E731
    return bool(evidence.strip()) and squash(evidence) in squash(text)


def validate(record: dict, candidates: dict, picks: dict) -> dict:
    """The proposal: one entry per field, each either a change or 'not established'."""
    text = ticket_text(record)
    by_field = {
        "category": {p["key"]: p for p in candidates["categories"]},
        "cmdb_ci": {c["sys_id"]: c for c in candidates["cis"]},
        "assignment_group": {g["sys_id"]: g for g in candidates["groups"]},
    }
    current = {
        "category": "/".join(v for v in (record["category"]["value"], record["subcategory"]["value"]) if v),
        "cmdb_ci": record["cmdb_ci"]["value"],
        "assignment_group": record["assignment_group"]["value"],
    }
    proposal = {}
    for field, allowed in by_field.items():
        pick = (picks or {}).get(field) if isinstance(picks, dict) else None
        pick = pick if isinstance(pick, dict) else {}
        value, evidence = str(pick.get("value", "")), str(pick.get("evidence", ""))
        entry = {"value": None, "label": "", "evidence": evidence,
                 "evidence_quoted": _quoted(evidence, text), "current": current[field],
                 "status": "not established", "reason": ""}
        if field == "assignment_group" and not allowed and candidates.get("assignee_excluded_groups"):
            # No group passed the assignee filter: say why in the operator's terms, not as a vague
            # "no candidate fits" (whitcortex #2613).
            entry["reason"] = (f"the current assignee {record['assigned_to']['display'] or 'on the incident'} "
                               "is not a member of any group incidents go to, and the instance's "
                               "'Abort changes on group' rule would reject the change")
        elif value == NONE or not value:
            entry["reason"] = "no candidate fits"
        elif value not in allowed:
            entry["reason"] = f"the model named {value!r}, which was not offered"
        elif value == current[field]:
            entry.update(value=value, label=allowed[value].get("label") or allowed[value]["name"],
                         status="unchanged", reason="already set")
        elif (field == "category" and "/" not in value and record["subcategory"]["value"]
              and value == record["category"]["value"]):
            # The current category without its subcategory: writing it changes nothing, so it is
            # not a proposal (whitcortex PR #2612 re-review).
            entry.update(value=value, label=allowed[value].get("label") or value,
                         status="unchanged",
                         reason=f"category already set (its subcategory "
                                f"{record['subcategory']['value']!r} is kept)")
        elif (field == "category" and "/" not in value and record["subcategory"]["value"]
              and value != record["category"]["value"]):
            # A category-only pick that changes the category would leave a subcategory that belongs
            # to the OLD category. Refused rather than written as a stale pair, and not "fixed" by
            # clearing the subcategory: the model didn't propose that (whitcortex PR #2612 review).
            entry["reason"] = (f"the incident's subcategory {record['subcategory']['value']!r} "
                               "belongs to its current category; a category-only change "
                               "would leave it stale (a category/subcategory pair is needed)")
        elif not entry["evidence_quoted"]:
            # whitcortex #2631: a pick the model could not back with a quote from the ticket is NOT
            # written. On INC0000059 (2026-10-11) the unquoted picks were a Software CI "Access",
            # matched on the word "access" alone, and an unsupported group; both would have been
            # applied. The suggestion stays in the note for the operator to set by hand.
            entry["reason"] = ("no evidence quoted from the ticket; the model suggested "
                               f"{allowed[value].get('label') or allowed[value]['name']}")
        else:
            entry.update(value=value, label=allowed[value].get("label") or allowed[value]["name"],
                         status="proposed", reason=allowed[value].get("why", ""))
        if field == "category" and entry["value"]:
            # The pair's own parts, not the key split on "/": a choice value may hold a "/" (PR #25 review)
            entry.update(category=allowed[value]["category"], subcategory=allowed[value]["subcategory"])
        proposal[field] = entry
    return proposal


def update_values(proposal: dict) -> dict:
    """now_update values for the PROPOSED fields only."""
    values = {}
    cat = proposal["category"]
    if cat["status"] == "proposed":
        values["category"] = cat["category"]
        if cat["subcategory"]:
            values["subcategory"] = cat["subcategory"]
    for field in ("cmdb_ci", "assignment_group"):
        if proposal[field]["status"] == "proposed":
            values[field] = proposal[field]["value"]
    return values


def render_note(number: str, proposal: dict, *, applied: bool = False, capped: list[str] = ()) -> str:
    """The proposal as lines of text. Labels, quotes and reasons come from the instance and the ticket,
    so each goes through safe(): a line break in a CI name must not draw a line of its own on the
    card (PR #25 review)."""
    if applied:
        head = "Triage APPLIED (local model, reviewed proposal):"
    elif config.READ_ONLY:
        head = "Triage proposal (DRY RUN, nothing changed; rio is read-only here, so set it by hand):"
    else:
        head = f"Triage proposal (DRY RUN, nothing changed; to apply, type: apply triage {number}):"
    lines = [f"{head} {number}"]
    names = {"category": "Category", "cmdb_ci": "CI", "assignment_group": "Assignment group"}
    for field, e in proposal.items():
        label, reason, evidence = safe(e["label"]), safe(e["reason"]), safe(e["evidence"])
        if e["status"] == "not established":
            lines.append(f"- {names[field]}: not established ({reason}).")
        elif e["status"] == "unchanged":
            # Nothing is written for an unchanged pick, so its evidence (which the unchanged
            # branches never check) is not shown as if it were a quote (whitcortex PR #2647 review).
            lines.append(f"- {names[field]}: {label} [unchanged]; {reason}.")
        else:
            status = "applied" if applied else e["status"]
            lines.append(f"- {names[field]}: {label} [{status}]; evidence: \"{evidence}\"")
    if capped:
        lines.append(f"- Note: the {' and the '.join(capped)} hit a read limit; some candidates may be missing.")
    lines.append("Generated by rio sn_triage on local inference (rio#1, whitcortex #2597).")
    return "\n".join(lines)


# ── Proposal store (local) ───────────────────────────────────────────────────

def _path(number: str):
    return config.STATE_DIR / "triage" / f"{record_number(number)}.json"


def save_proposal(record: dict, proposal: dict) -> dict:
    """Save it with what binds it: an id the card carries, the record's sys_id, and the instance."""
    saved = {"id": secrets.token_hex(6), "number": record["number"]["value"],
             "sys_id": record["sys_id"]["value"], "instance": config.sn_instance_url(),
             "sys_mod_count": record["sys_mod_count"]["value"], "proposal": proposal,
             "created": datetime.now().isoformat(timespec="seconds")}
    path = _path(record["number"]["value"])
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written whole or not at all: an interrupted write left an empty file that crashed every later
    # `apply triage` (PR #25 review, round 2).
    tmp = path.with_suffix(f".{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(saved, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return saved


def load_proposal(number: str) -> dict | None:
    """The saved proposal, None when there is none, or a refusal when the file can't be read."""
    path = _path(number)
    if not path.exists():
        return None
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ToolError(f"the saved triage proposal for {record_number(number)} can't be read ({e}); "
                        f"run `triage {record_number(number)}` again") from e
    if not isinstance(saved, dict):
        raise ToolError(f"the saved triage proposal for {record_number(number)} is not a proposal; "
                        f"run `triage {record_number(number)}` again")
    return saved


def pending_id(number: str) -> str:
    """The id of the saved proposal, for the card the operator's command makes; "" when none or
    unreadable (the card's preview then says why)."""
    try:
        saved = load_proposal(number)
    except ToolError:
        return ""
    return str(saved.get("id") or "") if saved else ""


# ── The dry run ──────────────────────────────────────────────────────────────

def run(number: str, *, chat=None) -> dict:
    """Propose and save. Changes nothing on the instance."""
    record = incident_record(number)
    number = record["number"]["value"]
    # A re-run replaces the proposal; if it fails, the old one must not stay applicable (PR #25 review).
    _path(number).unlink(missing_ok=True)
    candidates = gather(record)
    proposal = validate(record, candidates, classify(record, candidates, chat=chat))
    save_proposal(record, proposal)
    return {"number": number, "proposal": proposal,
            "note": render_note(number, proposal, capped=candidates["capped"])}


# ── Apply (from an approved card only) ───────────────────────────────────────

def _plan(number: str, proposal: str) -> tuple[dict, dict, dict]:
    """(saved proposal, the incident now, the values to write), or a refusal. Asked twice: when
    the card is made, and again when the operator says yes, since the card can wait.

    The card carries the proposal's id, so `y` writes the proposal that card showed and no other (a
    second rio sharing RIO_STATE_DIR may have re-triaged meanwhile), and the proposal must still be
    for this record on this instance (PR #25 review)."""
    number = record_number(number)
    saved = load_proposal(number)
    if saved is None:
        raise ToolError(f"no triage proposal for {number}; run `triage {number}` first")
    if not saved.get("id"):             # saved before proposals had ids: it can't be bound to a card
        raise ToolError(f"the saved triage proposal for {number} is from an older rio; run `triage {number}` again")
    if not proposal or proposal != saved.get("id"):
        raise ToolError(f"the triage proposal for {number} is not the one this card was made for; "
                        f"type `apply triage {number}` again")
    if saved.get("instance") != config.sn_instance_url():
        raise ToolError(f"the triage proposal for {number} was made on {saved.get('instance') or 'another instance'}, "
                        f"not {config.sn_instance_url()}; run `triage {number}` again")
    record = incident_record(number)
    if record["sys_id"]["value"] != saved["sys_id"]:
        raise ToolError(f"{number} is now another record (sys_id {record['sys_id']['value']}, not "
                        f"{saved['sys_id']}); run `triage {number}` again")
    count = record["sys_mod_count"]["value"]
    if not count.isdigit() or count != saved["sys_mod_count"]:
        raise ToolError(f"{number} has changed since the proposal (sys_mod_count "
                        f"{saved['sys_mod_count']} -> {count or 'unreadable'}); run `triage {number}` again")
    values = update_values(saved["proposal"])
    if not values:
        raise ToolError(f"nothing to apply on {number}: no field was proposed")
    # The Table API doesn't enforce reference qualifiers: a group deactivated or a CI retired since the
    # proposal would be written and read back as equal (PR #25 review, round 2). So ask again.
    if "assignment_group" in values and not rows(
            "sys_user_group", f"sys_id={values['assignment_group']}^active=true", ("sys_id",), 1):
        raise ToolError(f"the proposed group is no longer active; run `triage {number}` again")
    if "cmdb_ci" in values and not rows(
            "cmdb_ci", f"sys_id={values['cmdb_ci']}^operational_status=1", ("sys_id",), 1):
        raise ToolError(f"the proposed CI is no longer operational; run `triage {number}` again")
    return saved, record, values


def _labels(proposal: dict) -> dict:
    """What each written value stands for, so the card shows a name beside a sys_id."""
    cat = safe(proposal["category"]["label"])
    return {"category": cat, "subcategory": cat, "cmdb_ci": safe(proposal["cmdb_ci"]["label"]),
            "assignment_group": safe(proposal["assignment_group"]["label"])}


def preview(number: str, proposal: str = "") -> str:
    """The card: what the proposal would set, as now_update's own dry run, and what it can't."""
    saved, record, values = _plan(number, proposal)
    p = call_verb(data.now_update, table="incident", sys_id=record["sys_id"]["value"], values=values,
                  confirmed=False)
    labels = _labels(saved["proposal"])
    lines = [f"  apply triage proposal {saved['id']} to {record['number']['value']} "
             f"(made {saved.get('created', 'earlier')}):"]
    lines += [f"    {c['field']}: {safe(json.dumps(c['current'], default=str))} -> "
              f"{safe(json.dumps(c['proposed'], default=str))} ({labels.get(c['field'], '')})"
              for c in p.get("changes", [])] or ["    (no field would change)"]
    if p.get("unknown_fields"):
        # A field this account can't read is not written; the card says so before "y" (PR #25 review).
        lines.append(f"    not written, not a field this account can read: {', '.join(p['unknown_fields'])}")
    lines.append("  then a work note on the incident saying it was applied")
    return "\n".join(lines)


def _missed(result: dict) -> str:
    """now-data 0.6.0's not_applied, in the operator's words, by reason."""
    why = {"unknown_field": "not a field this account can see; not sent",
           "unreadable_field": "not readable by this account; not sent"}
    def one(m: dict) -> str:
        if m.get("reason") in why:
            return f"{m['field']} ({why[m['reason']]})"
        if m.get("reason") in (None, "value_differs"):
            return f"{m['field']} (asked {m['requested']!r}, the instance kept {m['actual']!r})"
        return f"{m['field']} (not applied: {m.get('reason')})"   # a reason this rio doesn't know yet
    return "; ".join(one(m) for m in result.get("not_applied") or [])


def apply(number: str, proposal: str = "") -> str:
    """Write the SAVED proposal if the incident hasn't changed since, confirm it landed, then note it.

    Runs only from the approved card (sn_triage_apply is operator_only, ADR-0003). now_update
    reads every field back (now-mcp#3), because a business rule can abort the whole update and
    ServiceNow still answers 200: the stock "Abort changes on group" rule did that to category and
    CI together on INC0000019 (whitcortex #2613)."""
    saved, record, values = _plan(number, proposal)
    number, sys_id = record["number"]["value"], record["sys_id"]["value"]
    result = call_verb(data.now_update, table="incident", sys_id=sys_id, values=values, confirmed=True)
    landed = sorted(result.get("changed_fields") or [])
    if result.get("action") != "updated":
        took = f"these took: {', '.join(landed)}; " if landed else "nothing took; "
        # Something landed, so the record changed and the proposal can't apply again: say what to do.
        then = (f"run `triage {number}` again for the rest" if landed else "the proposal is kept")
        raise ToolError(f"{number} was saved, but {took}these did not: {_missed(result) or 'unknown'}. "
                        f"No work note was added; {then}.")
    # The fields are applied and read back from here on; the proposal is spent (applied once).
    _path(number).unlink(missing_ok=True)
    applied = ", ".join(landed) or ", ".join(sorted(values))
    try:
        noted = call_verb(data.now_update, table="incident", sys_id=sys_id,
                          values={"work_notes": render_note(number, saved["proposal"], applied=True)},
                          confirmed=True)
    except Exception as e:
        # Say what DID happen: a bare failure here would read as "nothing applied" and invite a retry
        # (whitcortex PR #2612 review). Any failure, not only a refusal: a dropped connection after the
        # fields landed is the same story (PR #25 review), and "may not" because it may have landed.
        raise ToolError(f"Applied to {number} and read back ({applied}), but the 'applied' work note may "
                        f"not have been written ({type(e).__name__}: {e}); check the journal before adding one.") from e
    if noted.get("journal_unconfirmed") or noted.get("action") != "updated":
        return (f"Applied to {number} and read back: {applied}. The work note was accepted but could "
                "not be confirmed in the journal (it may not be readable by this account); check "
                "before adding another.")
    return f"Applied to {number} and read back: {applied}. A work note says so."
