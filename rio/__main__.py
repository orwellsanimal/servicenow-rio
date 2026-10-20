# rio/__main__.py — `python -m rio`: a terminal chat loop with ServiceNow tools and a write card.
#
# Each reply reports the decode rate (ADR-0002). Reads run inside the turn; a write the model asks
# for parks on a card and the next input answers it (rio.approval, ADR-0003). A message that is an
# operator command (a record number) is answered by rio itself, with no model turn (rio.commands).

from __future__ import annotations

import re
import sys

import requests

from rio import approval, commands, config, turn
from rio.llm import capabilities
from rio.tools import TOOLS, now, sn

SYSTEM = ("You are rio, a ServiceNow engineering companion running locally. Answer briefly and say "
          "when you do not know. Use the tools to look things up on the instance instead of guessing; "
          "never invent table or field names. A change to the instance needs the operator's approval "
          "on a card, so propose it with the tool and let the card ask.")

_PLAIN_NO = re.compile(r"\s*(n|no)\s*[.!]?\s*$", re.IGNORECASE)
# What reaches the screen, whatever its route: the card cleans its own fields, but instance text also
# arrives inside the model's reply, a card's args and tool errors (PR #24 review). Newlines stay.
_UNSAFE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f\u061c\u200b-\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069\ufeff]")
_SURROGATE = re.compile(r"[\ud800-\udfff]")


def screen(text: str) -> str:
    return _SURROGATE.sub("\ufffd", _UNSAFE.sub(" ", text))


def show(*blocks: str) -> None:
    """The one way rio prints: every block through screen(), the banner's error text included."""
    for block in blocks:
        print(screen(block))


def _stats(replies) -> str:
    return " · ".join(f"{r.eval_count} tok {r.tokens_per_s:.1f} tok/s {r.seconds:.1f}s" for r in replies if r.ok)


def main(argv: list[str] | None = None) -> int:
    # A character the console can't encode (an emoji with stdout redirected to a cp1252 file) must
    # not end the conversation and drop a waiting card (PR #24 review).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    show(f"rio · Ollama {config.OLLAMA_URL} · model {config.MODEL} · num_ctx {config.NUM_CTX}")
    try:
        caps = capabilities()
    except (requests.RequestException, RuntimeError, ValueError) as e:
        show(f"cannot start: {e}")
        return 1
    show(f"capabilities: {', '.join(sorted(caps)) or '(none reported)'}")
    problem = config.sn_config_problem()
    show(f"servicenow: {'configured for ' + config.sn_instance_url() if not problem else 'not configured: ' + problem}")
    show(*now.load(), *sn.load())
    if TOOLS and "tools" not in caps:
        show(f"note: {config.MODEL} does not list 'tools', so the tools above are not offered to it")
    show("type a message; /quit to leave\n")

    history: list[dict] = [{"role": "system", "content": SYSTEM}]
    card: approval.Card | None = None
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            continue
        if text in ("/quit", "/exit", "/q"):
            return 0
        shown, card = step(text, history, card, caps)
        show(*shown)


def step(text: str, history: list[dict], card: approval.Card | None, caps: frozenset[str], *,
         run=turn.run) -> tuple[list[str], approval.Card | None]:
    """One operator input: what to print, and the card left waiting for the next input."""
    shown: list[str] = []
    if card is not None:                # the card answers only the input right after it
        result = approval.answer(card, text)
        shown.append(f"rio> {result}\n")
        history.append({"role": "assistant",
                        "content": f"[{card.tool} {approval.outcome(result)}] {result}"})
        if not result.startswith("Not done") or _PLAIN_NO.match(text):
            return shown, None          # a yes ran the call; a plain no is just an answer
        # any other text drops the card and is also a new message

    handled = commands.handle(text)
    if handled is not None:             # an operator command: no model turn (whitcortex #2643)
        label, said, new_card = handled
        shown.append(f"rio> {said}\n")
        # Kept either way, so the next question can refer to it: the answer, or why there was none
        # (whitcortex kept both, PR #2616). Bracketed like a card's outcome, so a work note quoted in
        # it reads as the record's text, not as something rio said.
        history += [{"role": "user", "content": text},
                    {"role": "assistant", "content": f"[{label}] {said}"}]
        return shown, new_card

    before = len(history)
    history.append({"role": "user", "content": text})
    t = run(history, model_caps=caps)
    if not t.ok:
        del history[before:]            # drop the whole failed turn, tool round included
        return shown + [f"rio> {t.text}\n"], None
    tools = f" · tools: {', '.join(t.tools_run)}" if t.tools_run else ""
    shown.append(f"rio> {t.text}\n     [{config.MODEL} · {_stats(t.replies)}{tools}]\n")
    return shown, t.card


if __name__ == "__main__":
    sys.exit(main())
