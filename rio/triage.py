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

from __future__ import annotations

import json
import re
from datetime import datetime

from now_data import verbs as data

from rio import config, llm
from rio.servicenow import incident_record, record_number, rows, ticket_text
from rio.tools.errors import ToolError
from rio.tools.now import call_verb

NONE = "none"               # the enum member meaning "no candidate fits"
MAX_CIS = 15                # CI candidates offered to the model
MIN_CI_NAME = 4             # shorter CI names match too much text to be evidence
GROUP_HISTORY = 500         # recent incidents read to learn which groups are used
CHOICE_LIMIT = 1000         # now_query's own cap; a list this long may be incomplete
NAME_WORDS = 4              # the longest CI name looked for, in words
NAME_CHUNK = 100            # names per `nameIN` query, to keep the URL short
MAX_NAMES = 1000            # word sequences looked up at most (a pasted log would be thousands)
CI_ROWS = 200               # rows read per `nameIN` query (one name can be many CIs)


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
    """The word sequences of the ticket text that could be a CI's name, longest first.

    whitcortex read every operational CI and searched the text for each name. That is the whole
    CMDB per triage, and now_query stops at 1000 rows; a corporate CMDB is far larger. So rio asks
    the other way round: which of the text's own 1-to-4-word sequences are CI names (rio#1 plan of
    record). A sequence with a caret or a comma can't go into `nameIN`, and isn't a name anyway."""
    words = [w.strip(_TOKEN_EDGE) for w in text.split()]
    words = [w for w in words if w]
    seen: set[str] = set()
    out: list[str] = []
    for n in range(NAME_WORDS, 0, -1):
        for i in range(len(words) - n + 1):
            name = " ".join(words[i:i + n])
            if len(name) < MIN_CI_NAME or "^" in name or "," in name or name.lower() in seen:
                continue
            seen.add(name.lower())
            out.append(name)
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
    if caller:
        for r in rows("cmdb_ci", f"assigned_to={caller}^operational_status=1", ("sys_id", "name"), MAX_CIS):
            add(r["sys_id"]["value"], r["name"]["value"], "assigned to the caller")
    names = name_candidates(text)
    named = []
    for i in range(0, min(len(names), MAX_NAMES), NAME_CHUNK):
        chunk = names[i:min(i + NAME_CHUNK, MAX_NAMES)]
        named += rows("cmdb_ci", f"operational_status=1^nameIN{','.join(chunk)}", ("sys_id", "name"), CI_ROWS)
    # `nameIN` matched a whole name; this is whitcortex's whole-word check, kept as the decider.
    named = [r for r in named if len(r["name"]["value"].strip()) >= MIN_CI_NAME
             and re.search(rf"(?<!\w){re.escape(r['name']['value'].strip())}(?!\w)", text, re.I)]
    for r in sorted(named, key=lambda r: -len(r["name"]["value"])):   # most specific name first
        add(r["sys_id"]["value"], r["name"]["value"].strip(), "named in the ticket text")
    return list(found.values()), len(names) > MAX_NAMES


def group_candidates() -> list[dict]:
    """Active, assignable groups WITH MEMBERS that incidents are actually assigned to.

    Measured on dev442835 (2026-10-10): 78 active groups, mostly approvers and Studio users;
    ServiceNow's `itil` type marks only 3 approval groups. What incidents really go to is the
    honest candidate set (whitcortex #2597). Three reads, however many groups: whitcortex asked
    each group for a member in turn, and here the member check is one grouped count."""
    used: dict[str, str] = {}
    for r in rows("incident", "assignment_groupISNOTEMPTY^ORDERBYDESCsys_created_on",
                  ("assignment_group",), GROUP_HISTORY):    # "recent" means ordered (PR #2612 review)
        used.setdefault(r["assignment_group"]["value"], r["assignment_group"]["display"])
    if not used:
        return []
    ids = ",".join(used)
    groups = rows("sys_user_group", f"sys_idIN{ids}^active=true", ("sys_id", "name", "type"), len(used))
    members = call_verb(data.now_aggregate, table="sys_user_grmember", query=f"groupIN{ids}",
                        group_by="group", count=True, display_value=False)
    out = []
    for g in groups:
        sys_id = g["sys_id"]["value"]
        if "Non-Assignable" in g["type"]["display"]:
            continue
        if int((members.get(sys_id) or {}).get("count") or 0) > 0:
            out.append({"sys_id": sys_id, "name": g["name"]["value"], "why": "incidents are assigned here"})
    return sorted(out, key=lambda g: g["name"].lower())


def groups_the_assignee_is_in(groups: list[dict], assignee: str) -> list[dict]:
    """Only the groups the incident's current assignee belongs to (all of them when nobody is
    assigned).

    The instance's stock rule "Abort changes on group" (Incident Management Best Practice) looks up
    sys_user_grmember on (group, assigned_to) and aborts the WHOLE update when there is no row,
    category and CI with it (whitcortex #2613: INC0000019, 2026-10-11). This is the same lookup, so
    a group offered here passes the rule."""
    if not assignee or not groups:
        return groups
    ids = ",".join(g["sys_id"] for g in groups)
    mine = {r["group"]["value"] for r in rows("sys_user_grmember", f"user={assignee}^groupIN{ids}",
                                              ("group",), len(groups))}
    return [g for g in groups if g["sys_id"] in mine]


def gather(record: dict) -> dict:
    usable = group_candidates()
    groups = groups_the_assignee_is_in(usable, record["assigned_to"]["value"])
    categories, categories_capped = category_pairs()
    cis, names_capped = ci_candidates(record)
    return {"categories": categories, "cis": cis, "groups": groups,
            # True only when the ASSIGNEE filter removed groups, so the note blames the assignee
            # only when that is the cause (whitcortex #2613).
            "assignee_excluded_groups": len(groups) < len(usable),
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


def classify(record: dict, candidates: dict, *, chat=llm.chat) -> dict:
    """One local call. Raises on a failed or unparseable reply: no proposal follows it."""
    reply = chat(_prompt(record, candidates), format=triage_schema(candidates), think=False)
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
        proposal[field] = entry
    return proposal


def update_values(proposal: dict) -> dict:
    """now_update values for the PROPOSED fields only."""
    values = {}
    cat = proposal["category"]
    if cat["status"] == "proposed":
        category, _, sub = cat["value"].partition("/")
        values["category"] = category
        if sub:
            values["subcategory"] = sub
    for field in ("cmdb_ci", "assignment_group"):
        if proposal[field]["status"] == "proposed":
            values[field] = proposal[field]["value"]
    return values


def render_note(number: str, proposal: dict, *, applied: bool = False, capped: list[str] = ()) -> str:
    if applied:
        head = "Triage APPLIED (local model, reviewed proposal):"
    elif config.READ_ONLY:
        head = "Triage proposal (DRY RUN, nothing changed; rio is read-only here, so set it by hand):"
    else:
        head = f"Triage proposal (DRY RUN, nothing changed; to apply, type: apply triage {number}):"
    lines = [f"{head} {number}"]
    names = {"category": "Category", "cmdb_ci": "CI", "assignment_group": "Assignment group"}
    for field, e in proposal.items():
        if e["status"] == "not established":
            lines.append(f"- {names[field]}: not established ({e['reason']}).")
        elif e["status"] == "unchanged":
            # Nothing is written for an unchanged pick, so its evidence (which the unchanged
            # branches never check) is not shown as if it were a quote (whitcortex PR #2647 review).
            lines.append(f"- {names[field]}: {e['label']} [unchanged]; {e['reason']}.")
        else:
            lines.append(f"- {names[field]}: {e['label']} [{e['status']}]; evidence: \"{e['evidence']}\"")
    if capped:
        lines.append(f"- Note: the {' and the '.join(capped)} hit a read limit; some candidates may be missing.")
    lines.append("Generated by rio sn_triage on local inference (rio#1, whitcortex #2597).")
    return "\n".join(lines)


# ── Proposal store (local) ───────────────────────────────────────────────────

def _path(number: str):
    return config.STATE_DIR / "triage" / f"{record_number(number)}.json"


def save_proposal(record: dict, proposal: dict) -> None:
    path = _path(record["number"]["value"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "number": record["number"]["value"], "sys_id": record["sys_id"]["value"],
        "sys_mod_count": record["sys_mod_count"]["value"], "proposal": proposal,
        "created": datetime.now().isoformat(timespec="seconds")}, indent=2), encoding="utf-8")


def load_proposal(number: str) -> dict | None:
    path = _path(number)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


# ── The dry run ──────────────────────────────────────────────────────────────

def run(number: str, *, chat=llm.chat) -> dict:
    """Propose and save. Changes nothing on the instance."""
    record = incident_record(number)
    number = record["number"]["value"]
    candidates = gather(record)
    proposal = validate(record, candidates, classify(record, candidates, chat=chat))
    save_proposal(record, proposal)
    return {"number": number, "proposal": proposal,
            "note": render_note(number, proposal, capped=candidates["capped"])}


# ── Apply (from an approved card only) ───────────────────────────────────────

def _plan(number: str) -> tuple[dict, dict, dict]:
    """(saved proposal, the incident now, the values to write), or a refusal. Asked twice: when
    the card is made, and again when the operator says yes, since the card can wait."""
    number = record_number(number)
    saved = load_proposal(number)
    if saved is None:
        raise ToolError(f"no triage proposal for {number}; run `triage {number}` first")
    record = incident_record(number)
    if record["sys_mod_count"]["value"] != saved["sys_mod_count"]:
        raise ToolError(f"{number} has changed since the proposal (sys_mod_count "
                        f"{saved['sys_mod_count']} -> {record['sys_mod_count']['value']}); "
                        f"run `triage {number}` again")
    values = update_values(saved["proposal"])
    if not values:
        raise ToolError(f"nothing to apply on {number}: no field was proposed")
    return saved, record, values


def _labels(proposal: dict) -> dict:
    """What each written value stands for, so the card shows a name beside a sys_id."""
    cat = proposal["category"]["label"]
    return {"category": cat, "subcategory": cat, "cmdb_ci": proposal["cmdb_ci"]["label"],
            "assignment_group": proposal["assignment_group"]["label"]}


def preview(number: str) -> str:
    """The card: what the proposal would set, as now_update's own dry run."""
    saved, record, values = _plan(number)
    p = call_verb(data.now_update, table="incident", sys_id=record["sys_id"]["value"], values=values,
                  confirmed=False)
    labels = _labels(saved["proposal"])
    lines = [f"  apply the saved triage proposal to {record['number']['value']} "
             f"(made {saved.get('created', 'earlier')}):"]
    lines += [f"    {c['field']}: {json.dumps(c['current'], default=str)} -> "
              f"{json.dumps(c['proposed'], default=str)} ({labels.get(c['field'], '')})"
              for c in p.get("changes", [])] or ["    (no field would change)"]
    lines.append("  then a work note on the incident saying it was applied")
    return "\n".join(lines)


def apply(number: str) -> str:
    """Write the SAVED proposal if the incident hasn't changed since, confirm it landed, then note it.

    Runs only from the approved card (sn_triage_apply is operator_only, ADR-0003). now_update
    reads every field back (now-mcp#3), because a business rule can abort the whole update and
    ServiceNow still answers 200: the stock "Abort changes on group" rule did that to category and
    CI together on INC0000019 (whitcortex #2613)."""
    saved, record, values = _plan(number)
    number, sys_id = record["number"]["value"], record["sys_id"]["value"]
    result = call_verb(data.now_update, table="incident", sys_id=sys_id, values=values, confirmed=True)
    landed = sorted(result.get("changed_fields") or [])
    if result.get("action") != "updated":
        missed = "; ".join(f"{m['field']} (asked {m['requested']!r}, the instance kept {m['actual']!r})"
                           for m in result.get("not_applied") or [])
        took = f"these took: {', '.join(landed)}; " if landed else "nothing took; "
        raise ToolError(f"{number} was saved, but {took}these did not: {missed or 'unknown'}. "
                        "A business rule or a data policy may have kept them. No work note was added.")
    # The fields are applied and read back from here on; the proposal is spent (applied once).
    _path(number).unlink(missing_ok=True)
    applied = ", ".join(landed) or ", ".join(sorted(values))
    try:
        noted = call_verb(data.now_update, table="incident", sys_id=sys_id,
                          values={"work_notes": render_note(number, saved["proposal"], applied=True)},
                          confirmed=True)
    except ToolError as e:
        # Say what DID happen: a bare failure here would read as "nothing applied" and invite a retry
        # (whitcortex PR #2612 review).
        raise ToolError(f"Applied to {number} and read back ({applied}), but the 'applied' work note "
                        f"was not written: {e}") from e
    if noted.get("journal_unconfirmed"):
        return (f"Applied to {number} and read back: {applied}. The work note was accepted but could "
                "not be confirmed in the journal (it may not be readable by this account); check "
                "before adding another.")
    return f"Applied to {number} and read back: {applied}. A work note says so."
