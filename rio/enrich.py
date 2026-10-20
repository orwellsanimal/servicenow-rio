# rio/enrich.py — enrich one incident with what the instance already holds, on local inference (#1).
#
# Carried from whitcortex routines/graphs/sn_enrich.py (#2630, ADR-0132 there) and trimmed:
#   read:     the incident as data; refuse when unknown or already resolved/closed/canceled
#   terms:    ONE local model call names search terms; code keeps only the ones that appear in the
#             ticket text, so an invented term never reaches a query
#   gather:   four read-only gatherers, each reporting found / nothing found: similar Resolved/Closed
#             incidents (with close code and close notes), configuration items matching the terms or
#             their words, the caller's context, and published knowledge articles
#   summary:  ONE local model call over the findings, labelled as the model's reading; when it fails
#             the note says "not available" and the findings still stand
#   verdict:  code decides, not the model: `enriched` when the ticket carries detail and a gatherer
#             found something, else `needs detail`
# Then, as rio's triage does (rio#1 plan of record): the note is shown and saved locally and NOTHING
# is written, so enrichment works under RIO_READ_ONLY. `apply enrich <INC>` parks a card for THAT
# note, and `y` writes it as ONE work note, then ONE tag, if the incident hasn't changed since.
# whitcortex's sn_enrich was a single write behind a card; recomputing at "y" would post a note the
# operator never saw (two model calls), which ADR-0003 rules out.
#
# Operator decisions carried (whitcortex #2630, 2026-10-12/13): local inference only; no web search;
# ONE tag, since a tag is a label_entry row nothing retires; similar incidents come from all
# Resolved/Closed, the caller's own and same-category ones first, capped at 5.

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime
from html import unescape

from now_data import verbs as data

from rio import config, llm, tags as tagging
from rio.servicenow import incident_record, is_sys_id, record_number, rows, safe, ticket_text
from rio.tools.errors import ToolError
from rio.tools.now import call_verb

TAG = "enriched-by-rio"
MAX_TERMS = 6
MAX_SIMILAR = 5
MAX_CIS = 8
MAX_KB = 5
MAX_CALLER_CIS = 10
FETCH_LIMIT = 200          # rows read per gatherer before ranking
NOTE_CHARS = 240           # close notes quoted per similar incident
MIN_DETAIL_CHARS = 40      # a description shorter than this is a title, not detail
MIN_WORD_CHARS = 4         # a word of a term that may match a CI name on its own
_DONE_STATES = {"6": "Resolved", "7": "Closed", "8": "Canceled"}
_SOFTWARE_CLASS = "cmdb_ci_spkg"   # installed software packages: named after anything
# Nothing in this set is an encoded-query operator or separator (`^`, `,`, `=`, `!`, `<`, `>`, `*`,
# `@`, `%` are all excluded; LIKE has no wildcard). The apostrophe is inert in a REST encoded query;
# it would need escaping if a term ever reached a server-side script.
_TERM_CHARS = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .'\-_/]{1,39}")

_TERMS_SCHEMA = {"type": "object",
                 "properties": {"terms": {"type": "array", "items": {"type": "string"}}},
                 "required": ["terms"]}


def _squash(s: str) -> str:
    """One line of text: whitespace squashed, and control characters and bidi overrides gone, since
    this text ends up in a note, on a card and on the screen (PR #25 review)."""
    return safe(s)


def refusal(record: dict) -> str:
    """Why this incident is not enriched here, or ""."""
    state = record["state"]["value"]
    if state in _DONE_STATES:
        return f"it is already {_DONE_STATES[state]}"
    return ""


# ── Search terms (one local call, grounded by code) ──────────────────────────

def _terms_prompt(record: dict) -> list[dict]:
    system = (
        "You read one ServiceNow incident and list the search terms a support engineer would use to "
        "find related records: the system, service, device, folder or symptom the ticket is about, "
        "in the ticket's own words, MOST IMPORTANT FIRST: the thing that is broken or wanted comes "
        "before anything mentioned in passing. Each term is one to three words copied from the "
        "ticket text. Give three to six terms. Do not list people's names, ticket numbers, or "
        "generic words such as issue, problem, help, unable, access.")
    user = (f"Ticket {record['number']['value']}\n"
            f"Short description: {record['short_description']['value']}\n"
            f"Description: {record['description']['value']}")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def ground_terms(terms, text: str) -> list[str]:
    """The model's terms that are safe to query and appear in the ticket text."""
    squashed = _squash(text).lower()
    out: list[str] = []
    for raw in terms if isinstance(terms, list) else []:
        term = _squash(str(raw)) if isinstance(raw, str) else ""
        if not term or not _TERM_CHARS.fullmatch(term):    # a '^' or ',' would widen a query
            continue
        if not _present(term, squashed) or term.lower() in (t.lower() for t in out):
            continue
        out.append(term)
        if len(out) == MAX_TERMS:
            break
    return out


def extract_terms(record: dict, *, chat=None) -> list[str]:
    """One local call. Raises when it fails or names nothing from the ticket."""
    reply = (chat or llm.chat)(_terms_prompt(record), format=_TERMS_SCHEMA, think=False)
    if not reply.ok or not reply.content.strip():
        raise ToolError(f"the local model gave no usable reply ({reply.error or 'empty'})")
    try:
        parsed = json.loads(reply.content)
    except json.JSONDecodeError as e:
        raise ToolError(f"the local model's reply was not JSON: {e}") from e
    terms = ground_terms(parsed.get("terms") if isinstance(parsed, dict) else None, ticket_text(record))
    if not terms:
        raise ToolError("the local model named no search term that appears in the ticket text")
    return terms


# ── Gatherers (read-only; each returns [] for nothing found) ─────────────────

def _rows(table: str, fields: str, encoded: str, limit: int | None = None) -> list[dict]:
    """whitcortex's row shape (`f` and `f__display`) over rio's reads, so the carried rankers stay
    as they were measured. The limit defaults to FETCH_LIMIT as it is when called. Every value is
    made one safe line here: all of it is someone else's text, bound for the note and the screen."""
    names = tuple(fields.split(","))
    return [{f: safe(r[f]["value"]) for f in names} | {f"{f}__display": safe(r[f]["display"]) for f in names}
            for r in rows(table, encoded, names, limit or FETCH_LIMIT)]


def _like(fields: tuple[str, ...], terms: list[str]) -> str:
    """`f1LIKEt1^ORf2LIKEt1^ORf1LIKEt2…`: ORed, so it ANDs with what precedes it."""
    return "^OR".join(f"{f}LIKE{t}" for t in terms for f in fields)


def _words(term: str) -> list[str]:
    return [w for w in re.findall(r"[A-Za-z0-9]+", term) if len(w) >= MIN_WORD_CHARS]


def term_words(terms: list[str]) -> list[str]:
    """The words of MIN_WORD_CHARS or more inside the terms, for names that are compounds:
    FileServerFloor1 carries "file" but never "file share". A word that is itself a term is not
    repeated."""
    words: list[str] = []
    for t in terms:
        for w in _words(t):
            if w.lower() not in (x.lower() for x in [*words, *terms]):
                words.append(w)
    return words


def _present(needle: str, blob: str) -> bool:
    """Whole-word presence: "team" is not in "steamdeck"."""
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(needle.lower())}(?![A-Za-z0-9])", blob) is not None


def match(terms: list[str], *texts: str) -> dict:
    """How the texts match the terms, or {} for not at all.

    `rank` orders matches: ANY whole-term match before any word-only match, then the earlier term
    (the model lists the ticket's subject first) before a later one. The first order matters for the
    caps: a word hit on the first term must not push a whole-term hit on a later term out of the
    list, or the verdict would be computed on a list the real match was cut from (whitcortex PR #2642
    review, round 1). `matched` names what was found, for the note; `whole` says a whole term
    matched, which is what the readiness verdict counts (a word alone is a lead, not a finding). The
    LIKE query is a substring match on the instance; this is the whole-word check that decides."""
    blob = _squash(" ".join(texts)).lower()
    whole_at, word_at, matched = None, None, []
    for i, term in enumerate(terms):
        if _present(term, blob):
            whole_at = i if whole_at is None else whole_at
            matched.append(term)
            continue
        for w in _words(term):
            if _present(w, blob):
                word_at = i if word_at is None else word_at
                matched.append(w)
                break
    if not matched:
        return {}
    labels = list(dict.fromkeys(m.lower() for m in matched))
    rank = (0, whole_at) if whole_at is not None else (1, word_at)
    return {"rank": rank, "hits": len(matched), "matched": labels, "whole": whole_at is not None}


def _query_terms(terms: list[str]) -> list[str]:
    return [*terms, *term_words(terms)]


def _capped(report: dict | None, name: str, found: list) -> None:
    """Record that the instance returned the fetch limit: the ranking then ran over the first
    FETCH_LIMIT rows in the instance's own order, and "nothing found" below the caps cannot be told
    apart from "not among them" (whitcortex PR #2642 review, round 1)."""
    if report is not None and len(found) >= FETCH_LIMIT:
        report[name] = True


def similar_incidents(record: dict, terms: list[str], report: dict | None = None) -> list[dict]:
    """Resolved/Closed incidents sharing the terms: the caller's own first, then the same category,
    then by how well they match. Capped at MAX_SIMILAR."""
    found = _rows("incident",
                  "number,short_description,description,state,category,caller_id,close_code,close_notes",
                  f"stateIN6,7^{_like(('short_description', 'description'), _query_terms(terms))}")
    _capped(report, "similar", found)
    own = record["number"]["value"]
    caller = record["caller_id"]["value"]
    category = record["category"]["value"]
    ranked = []
    for r in found:
        if r["number"] == own or r["state"] not in ("6", "7"):   # the OR chain must not widen past the state
            continue
        m = match(terms, r["short_description"], r["description"])
        if not m:
            continue
        same_caller = bool(caller and r["caller_id"] == caller)
        same_category = bool(category and r["category"] == category)
        why = [w for w in ("same caller" if same_caller else "", "same category" if same_category else "") if w]
        why.append("matched: " + ", ".join(m["matched"]))
        ranked.append({"number": r["number"], "short_description": r["short_description"],
                       "state": r["state__display"], "close_code": r["close_code__display"] or r["close_code"],
                       "close_notes": _squash(r["close_notes"])[:NOTE_CHARS], "why": "; ".join(why),
                       "whole": m["whole"],
                       "_key": (not same_caller, not same_category, m["rank"], -m["hits"], r["number"])})
    ranked.sort(key=lambda r: r["_key"])
    return [{k: v for k, v in r.items() if k != "_key"} for r in ranked[:MAX_SIMILAR]]


def matching_cis(terms: list[str], report: dict | None = None) -> list[dict]:
    """Operational CIs whose name or description carries a term or one of its words: best match
    first, installed software packages last, one entry per name (the demo CMDB holds the same
    printer model many times; when a name is both a package and a device, the device is the one
    shown). Capped at MAX_CIS."""
    found = _rows("cmdb_ci", "sys_id,name,sys_class_name,short_description",
                  f"operational_status=1^{_like(('name', 'short_description'), _query_terms(terms))}")
    _capped(report, "cis", found)
    by_name: dict[str, dict] = {}
    for r in found:
        m = match(terms, r["name"], r["short_description"])
        if not m:
            continue
        key = r["name"].strip().lower()
        software = r["sys_class_name"] == _SOFTWARE_CLASS
        entry = {"sys_id": r["sys_id"], "name": r["name"].strip(),
                 "class": r["sys_class_name__display"] or r["sys_class_name"],
                 "matched": m["matched"], "count": 1, "whole": m["whole"],
                 "_software": software, "_key": (m["rank"], -m["hits"], software, key)}
        seen = by_name.get(key)
        if seen is None:
            by_name[key] = entry
        elif seen["_software"] and not software:
            by_name[key] = entry | {"count": seen["count"] + 1}
        else:
            seen["count"] += 1
    ranked = sorted(by_name.values(), key=lambda r: r["_key"])
    return [{k: v for k, v in r.items() if k not in ("_key", "_software")} for r in ranked[:MAX_CIS]]


def caller_context(record: dict) -> dict:
    """The caller's name, department, location and operational CIs, or {}."""
    caller = record["caller_id"]["value"]
    if not is_sys_id(caller):          # a reference goes into a query only as a sys_id
        return {}
    users = _rows("sys_user", "name,department,location,title", f"sys_id={caller}", limit=1)
    if not users:
        return {}
    u = users[0]
    cis = _rows("cmdb_ci", "name,sys_class_name", f"assigned_to={caller}^operational_status=1",
                limit=MAX_CALLER_CIS)
    return {"name": u["name"], "department": u["department__display"],
            "location": u["location__display"], "title": u["title"],
            "cis": [{"name": c["name"], "class": c["sys_class_name__display"] or c["sys_class_name"]}
                    for c in cis]}


def _plain(html: str) -> str:
    """An article's text without its markup: a term must not match a class name, a style or a link
    target (whitcortex PR #2642 review, round 1)."""
    return unescape(re.sub(r"<[^>]*>", " ", html or ""))


def knowledge(terms: list[str], report: dict | None = None) -> list[dict]:
    """Published articles carrying a term, or one of its words, in their title or text (markup
    stripped). Best match first. Capped at MAX_KB."""
    found = _rows("kb_knowledge", "number,short_description,text",
                  f"workflow_state=published^{_like(('short_description', 'text'), _query_terms(terms))}")
    _capped(report, "knowledge", found)
    ranked = []
    for r in found:
        m = match(terms, r["short_description"], _plain(r["text"]))
        if m:
            ranked.append({"number": r["number"], "short_description": _squash(r["short_description"]),
                           "matched": m["matched"], "whole": m["whole"],
                           "_key": (m["rank"], -m["hits"], r["number"])})
    ranked.sort(key=lambda r: r["_key"])
    return [{k: v for k, v in r.items() if k != "_key"} for r in ranked[:MAX_KB]]


def gather(record: dict, terms: list[str]) -> dict:
    capped: dict = {}
    return {"similar": similar_incidents(record, terms, capped),
            "cis": matching_cis(terms, capped),
            "caller": caller_context(record),
            "knowledge": knowledge(terms, capped),
            "capped": sorted(capped)}


# ── Summary (one local call, labelled) and verdict (code) ────────────────────

def _ci_list(cis: list[dict]) -> str:
    out = []
    for c in cis:
        tail = f"; matched {', '.join(c['matched'])}" if c.get("matched") else ""
        times = f" x{c['count']}" if c.get("count", 1) > 1 else ""
        out.append(f"{c['name']} ({c['class']}{tail}){times}")
    return "; ".join(out)


def _findings_text(findings: dict) -> str:
    lines = []
    for s in findings["similar"]:
        lines.append(f"Resolved incident {s['number']} \"{s['short_description']}\" ({s['why']}); "
                     f"close code {s['close_code']}; close notes: {s['close_notes']}")
    for c in findings["cis"]:
        lines.append(f"Configuration item {c['name']} ({c['class']}; matched {', '.join(c['matched'])})")
    if findings["caller"]:
        u = findings["caller"]
        lines.append(f"Caller {u['name']}, {u['department'] or 'no department'}, "
                     f"{u['location'] or 'no location'}; their CIs: "
                     f"{', '.join(c['name'] for c in u['cis']) or 'none'}")
    for k in findings["knowledge"]:
        lines.append(f"Knowledge article {k['number']} \"{k['short_description']}\" (matched {', '.join(k['matched'])})")
    return "\n".join(lines) or "(nothing found)"


def summarise(record: dict, findings: dict, *, chat=None) -> str:
    """One local call over the findings. Raises when the model gives nothing."""
    system = (
        "You are a support engineer reading one incident and the records found for it. In two to "
        "four plain sentences, say which of the records bear on this ticket and what the engineer "
        "should try or ask next. Use ONLY the records listed; if none bears on the ticket, say so. "
        "No headings, no bullets.")
    user = (f"Ticket {record['number']['value']}\n"
            f"Short description: {record['short_description']['value']}\n"
            f"Description: {record['description']['value']}\n\n"
            f"Records found:\n{_findings_text(findings)}")
    reply = (chat or llm.chat)([{"role": "system", "content": system}, {"role": "user", "content": user}],
                               think=False)
    text = _squash(reply.content)
    if not reply.ok or not text:
        raise ToolError(f"the local model gave no usable reply ({reply.error or 'empty'})")
    return text


def verdict(record: dict, findings: dict) -> tuple[str, str]:
    """`enriched` or `needs detail`, with the reason. Only a WHOLE-term match counts: a record
    reached by one word of a term is a lead the note shows, not a finding (on the demo instance
    "file" alone reaches five unrelated articles). The caller's context never counts: it is there for
    every ticket with a caller."""
    detail = len(_squash(record["description"]["value"])) >= MIN_DETAIL_CHARS
    found = [k for k in ("similar", "cis", "knowledge") if any(r.get("whole") for r in findings[k])]
    leads = [k for k in ("similar", "cis", "knowledge") if findings[k]]
    if detail and found:
        return "enriched", "the ticket carries detail and the instance holds records that match its terms"
    reasons = []
    if not detail:
        reasons.append(f"the description is under {MIN_DETAIL_CHARS} characters")
    if not found:
        reasons.append("no similar incident, configuration item or article matched a whole term"
                       + (" (word-level leads only, listed above)" if leads else ""))
    return "needs detail", "; ".join(reasons)


# ── The note ─────────────────────────────────────────────────────────────────

def render_note(record: dict, terms: list[str], findings: dict, summary: str,
                readiness: tuple[str, str]) -> str:
    number = record["number"]["value"]
    lines = [f"Enrichment (rio sn_enrich, local inference, rio#1, whitcortex #2630): {number}",
             f"Search terms, from the ticket text: {', '.join(terms)}",
             f"- Similar resolved incidents (all Resolved/Closed; caller's and same-category first; up to {MAX_SIMILAR}):"]
    for s in findings["similar"]:
        lines.append(f"  {s['number']} \"{s['short_description']}\" [{s['state']}; {s['close_code'] or 'no close code'}] "
                     f"({s['why']}). Close notes: {s['close_notes'] or 'none'}")
    if not findings["similar"]:
        lines.append("  nothing found.")
    lines.append(f"- Configuration items matching the terms or their words (best match first; up to {MAX_CIS}):")
    lines.append("  " + (_ci_list(findings["cis"]) if findings["cis"] else "nothing found."))
    u = findings["caller"]
    if u:
        lines.append(f"- Caller: {u['name']}, {u['department'] or 'no department'}, "
                     f"{u['location'] or 'no location'}. Their CIs: {_ci_list(u['cis']) or 'none'}.")
    else:
        lines.append("- Caller: nothing found (no caller on the incident, or not readable).")
    lines.append(f"- Knowledge (published, up to {MAX_KB}):")
    lines.append("  " + ("; ".join(f"{k['number']} \"{k['short_description']}\" (matched {', '.join(k['matched'])})"
                                    for k in findings["knowledge"])
                         if findings["knowledge"] else "nothing found."))
    if findings.get("capped"):
        lines.append(f"- Note: the instance returned the fetch limit of {FETCH_LIMIT} rows for "
                     f"{', '.join(findings['capped'])}; only those were ranked, more may match.")
    lines.append(f"Model summary (local model, not verified): {summary}")
    lines.append(f"Readiness: {readiness[0]} ({readiness[1]}).")
    lines.append(f"Tag: {TAG}. No field on the incident was changed by this note.")
    return "\n".join(lines)


# ── The note store (local) ───────────────────────────────────────────────────

def _path(number: str):
    return config.STATE_DIR / "enrich" / f"{record_number(number)}.json"


def load_note(number: str) -> dict | None:
    """The saved note, None when there is none, or a refusal when the file can't be read (as
    triage's, PR #25 review round 2: an empty file used to crash rio)."""
    path = _path(number)
    if not path.exists():
        return None
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ToolError(f"the saved enrichment for {record_number(number)} can't be read ({e}); "
                        f"run `enrich {record_number(number)}` again") from e
    if not isinstance(saved, dict) or not saved.get("id"):
        raise ToolError(f"the saved enrichment for {record_number(number)} is not one this rio can post; "
                        f"run `enrich {record_number(number)}` again")
    return saved


def pending_id(number: str) -> str:
    """The id of the saved note, for the card the operator's command makes; "" when none or
    unreadable (the card's preview then says why)."""
    try:
        saved = load_note(number)
    except ToolError:
        return ""
    return str(saved.get("id") or "") if saved else ""


# ── The dry run ──────────────────────────────────────────────────────────────

def run(number: str, *, chat=None) -> dict:
    """Gather, summarise, judge, and save the note. Writes nothing to the instance."""
    record = incident_record(number)          # an unknown number raises
    number = record["number"]["value"]
    why = refusal(record)
    if why:
        raise ToolError(f"{number} was not enriched: {why}. Nothing was changed.")
    # A re-run replaces the note; if it fails, the old one must not stay postable (PR #25 review).
    _path(number).unlink(missing_ok=True)
    terms = extract_terms(record, chat=chat)
    findings = gather(record, terms)
    try:
        summary = summarise(record, findings, chat=chat)
    except ToolError as e:
        summary = f"not available ({e})"
    readiness = verdict(record, findings)
    note = render_note(record, terms, findings, summary, readiness)
    path = _path(number)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{secrets.token_hex(4)}.tmp")    # whole or not at all, as triage's
    tmp.write_text(json.dumps({
        "id": secrets.token_hex(6), "number": number, "sys_id": record["sys_id"]["value"],
        "instance": config.sn_instance_url(), "sys_mod_count": record["sys_mod_count"]["value"],
        "note": note, "readiness": readiness[0],
        "created": datetime.now().isoformat(timespec="seconds")}, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    head = ("Enrichment DRY RUN (nothing written; rio is read-only here)" if config.READ_ONLY else
            f"Enrichment DRY RUN (nothing written; to post it as a work note and tag it, type: apply enrich {number})")
    return {"number": number, "terms": terms, "findings": findings, "readiness": readiness[0],
            "note": note, "shown": f"{head}\n{note}"}


# ── Apply (from an approved card only) ───────────────────────────────────────

def _plan(number: str, proposal: str) -> tuple[dict, dict]:
    """(the saved note, the incident now), or a refusal. Asked when the card is made and again at
    `y`, since the card can wait. The card carries the note's id, and the note must still be for
    this record on this instance (as triage's, PR #25 review)."""
    number = record_number(number)
    saved = load_note(number)
    if saved is None:
        raise ToolError(f"no enrichment for {number}; run `enrich {number}` first")
    if not proposal or proposal != saved.get("id"):
        raise ToolError(f"the enrichment for {number} is not the one this card was made for; "
                        f"type `apply enrich {number}` again")
    if saved.get("instance") != config.sn_instance_url():
        raise ToolError(f"the enrichment for {number} was made on {saved.get('instance') or 'another instance'}, "
                        f"not {config.sn_instance_url()}; run `enrich {number}` again")
    record = incident_record(number)
    if record["sys_id"]["value"] != saved["sys_id"]:
        raise ToolError(f"{number} is now another record; run `enrich {number}` again")
    why = refusal(record)
    if why:
        raise ToolError(f"{number} was not enriched: {why}. Nothing was changed.")
    count = record["sys_mod_count"]["value"]
    if not count.isdigit() or count != saved["sys_mod_count"]:
        raise ToolError(f"{number} has changed since the enrichment (sys_mod_count {saved['sys_mod_count']} "
                        f"-> {count or 'unreadable'}); run `enrich {number}` again")
    return saved, record


def preview(number: str, proposal: str = "") -> str:
    saved, record = _plan(number, proposal)
    note = "\n".join(f"    {line}" for line in saved["note"].split("\n"))
    return (f"  post enrichment {saved['id']} as a work note on {record['number']['value']} "
            f"(made {saved.get('created', 'earlier')}):\n{note}\n  then tag it {TAG}")


def apply(number: str, proposal: str = "") -> str:
    """ONE work note, THEN the tag; then every non-journal field compared with before."""
    saved, record = _plan(number, proposal)
    number, sys_id = record["number"]["value"], record["sys_id"]["value"]
    try:
        noted = call_verb(data.now_update, table="incident", sys_id=sys_id,
                          values={"work_notes": saved["note"]}, confirmed=True)
    except Exception as e:
        # A dropped connection or an error answer may come after the note landed (now-data's
        # WriteUnconfirmed): say so, or a retry adds a second note (whitcortex #2630, PR #25 review).
        raise ToolError(f"The work note on {number} may not have been written ({type(e).__name__}: {e}); "
                        f"{number} was NOT tagged. Check the journal before retrying: if it landed, a retry "
                        "adds a second note.") from e
    if noted.get("journal_unconfirmed"):
        # Accepted, but not confirmed in the journal. Said plainly, or a "did not complete" would
        # read as "nothing happened" and invite a retry that duplicates the note (whitcortex #2630).
        raise ToolError(f"The work note on {number} was ACCEPTED by the instance but could not be confirmed "
                        f"in its journal, so it MAY have landed; {number} was NOT tagged. Check the journal "
                        "before retrying: a retry would add a second note.")
    if noted.get("action") != "updated" or "work_notes" not in (noted.get("changed_fields") or []):
        raise ToolError(f"The work note on {number} was not written ({noted.get('action')}); nothing was tagged.")
    _path(number).unlink(missing_ok=True)      # the note is posted; the saved one is spent
    try:
        tagged = tagging.tag("incident", sys_id, number, [TAG])
    except Exception as e:
        raise ToolError(f"The work note is on {number}, but it was NOT tagged: {e} Do not enrich it again "
                        f"(it would add a second note); tag it {TAG} by hand.") from e
    lines = [f"{number}: the enrichment note is posted (readiness: {saved.get('readiness', '?')}).", tagged]
    compared = sorted(f for f in record if f != "sys_mod_count")
    try:
        after = incident_record(number)
    except ToolError as e:                     # both writes are done and confirmed by now
        lines.append(f"The verification read after the writes failed ({e}); whether any other field "
                     "changed was not established.")
    else:
        changed = sorted(f for f in compared if after[f]["value"] != record[f]["value"])
        if changed:
            lines.append("WARNING: these fields changed during the run, not by this note: "
                         + ", ".join(f"{f} ({record[f]['value']!r} -> {after[f]['value']!r})" for f in changed))
        else:
            # Only the fields read for triage are compared; sys_mod_count is excluded because the two
            # writes bump it. Said as what it is, not as "nothing changed".
            lines.append(f"None of the {len(compared)} compared fields changed.")
    return "\n".join(lines)
