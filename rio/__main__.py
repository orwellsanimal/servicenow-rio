# rio/__main__.py — `python -m rio`: a terminal chat loop that reports what the machine can do.
#
# Day one has no tools: the loop exists to measure the laptop (ADR-0002) and to be the surface the
# ServiceNow tools, the approval card and the operator commands land on (issues #1–#3).

from __future__ import annotations

import sys

import requests

from rio import config
from rio.llm import capabilities, chat

SYSTEM = ("You are rio, a ServiceNow engineering companion running locally. Answer briefly "
          "and say when you do not know.")


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
    print("type a message; /quit to leave\n")
    history: list[dict] = [{"role": "system", "content": SYSTEM}]
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
        history.append({"role": "user", "content": text})
        reply = chat(history, think=False)
        if not reply.ok:
            print(f"rio> (no reply) {reply.error}\n")
            history.pop()
            continue
        history.append({"role": "assistant", "content": reply.content})
        print(f"rio> {reply.content.strip()}")
        print(f"     [{reply.model} · {reply.eval_count} tokens · {reply.tokens_per_s:.1f} tok/s · {reply.seconds:.1f}s total]\n")


if __name__ == "__main__":
    sys.exit(main())
