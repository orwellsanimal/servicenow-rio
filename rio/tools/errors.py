# rio/tools/errors.py — the one exception a tool raises to refuse, worded for the model.
#
# Carried from whitcortex tools/errors.py (#990, ADR-0065) and trimmed to the class: rio has no
# circuit breaker, so the failure kinds (#1642) that fed it stay behind. `execute_tool` turns a
# ToolError into "Tool error (<tool>): <message>" and anything else into an "unexpected" crash line.


class ToolError(Exception):
    """A deliberate refusal: the message says what was wrong and, when it can, what to do instead."""
