# rio/__main__.py — `python -m rio`: a terminal chat loop with ServiceNow tools and a write card.
#
# Each reply reports the decode rate (ADR-0002). Reads run inside the turn; a write the model asks
# for parks on a card and the next input answers it (rio.approval, ADR-0003).

from __future__ import annotations

import re
import sys

import requests

from rio import approval, config, turn
from rio.llm import capabilities
from rio.tools import TOOLS, now

SYSTEM = ("You are rio, a ServiceNow engineering companion running locally. Answer briefly and say "
          "when you do not know. Use the tools to look things up on the instance instead of guessing; "
          "never invent table or field names. A change to the instance needs the operator's approval "
          "on a card, so propose it with the tool and let the card ask.")

_PLAIN_NO = re.compile(r"\s*(n|no)\s*[.!]?\s*$", re.IGNORECASE)


def _stats(replies) -> str:
    return " · ".join(f"{r.eval_count} tok {r.tokens_per_s:.1f} tok/s {r.seconds:.1f}s" for r in replies if r.ok)


def main(argv: list[str] | None = None) -> int:
    print(f"rio · Ollama {config.OLLAMA_URL} · model {config.MODEL} · num_ctx {config.NUM_CTX}")
    try:
        caps = capabilities()
    except (requests.RequestException, RuntimeError, ValueError) as e:
        print(f"cannot start: {e}")
        return 1
    print(f"capabilities: {', '.join(sorted(caps)) or '(none reported)'}")
    problem = config.sn_config_problem()
    print(f"servicenow: {'configured for ' + config.sn_instance_url() if not problem else 'not configured: ' + problem}")
    for note in now.load():
        print(note)
    if TOOLS and "tools" not in caps:
        print(f"note: {config.MODEL} does not list 'tools', so the tools above are not offered to it")
    print("type a message; /quit to leave\n")

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

        if card is not None:            # the card answers only the input right after it
            result = approval.answer(card, text)
            declined = result.startswith("Not done")
            print(f"rio> {result}\n")
            history.append({"role": "assistant",
                            "content": f"[{card.tool} {approval.outcome(result)}] {result}"})
            card = None
            if not declined or _PLAIN_NO.match(text):
                continue                # a yes ran the call; a plain no is just an answer
            # any other text drops the card and is also a new message

        before = len(history)
        history.append({"role": "user", "content": text})
        t = turn.run(history, model_caps=caps)
        if not t.ok:
            print(f"rio> {t.text}\n")
            del history[before:]        # drop the whole failed turn, tool round included
            continue
        card = t.card
        print(f"rio> {t.text}")
        tools = f" · tools: {', '.join(t.tools_run)}" if t.tools_run else ""
        print(f"     [{config.MODEL} · {_stats(t.replies)}{tools}]\n")


if __name__ == "__main__":
    sys.exit(main())
