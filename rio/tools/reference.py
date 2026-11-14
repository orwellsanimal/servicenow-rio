# rio/tools/reference.py — the ServiceNow documentation and the Fluent SDK samples as rio tools (#27).
#
# Three reads over files on this machine (rio.reference, ADR-0004): look something up in the
# documentation, look something up in the samples, read more of either. They need no instance and
# no credentials, so they load whether or not ServiceNow is configured, and each is offered only
# when its folder is there.
#
# A search returns the text of its best match, not only paths: a turn has one tool round (ADR-0003),
# so the model can't search and then read before it answers.

from __future__ import annotations

from rio import reference
from rio.tools import register
from rio.tools.errors import ToolError


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except reference.Refusal as e:
        raise ToolError(str(e)) from e


def load() -> list[str]:
    """Register the reference tools whose folders are present. Returns one banner line."""
    docs_off, examples_off = reference.docs_problem(), reference.examples_problem()
    on = []

    if not docs_off:
        @register("docs_search", group="reference", writes=False)
        def docs_search(query: str) -> str:
            """Look something up in the ServiceNow product documentation (APIs, scripting, Flow Designer, administration). Returns the text of the best page and lists other pages. Answer from the returned text; say so if it does not cover the question.

            Pages are found by title, description and file name, so name the thing (GlideRecord,
            business rule, ACL) and add the method or setting you want: the page opens there.

            Args:
              query: one to four keywords, e.g. GlideRecord addEncodedQuery or business rule or flow designer trigger
            """
            return _call(reference.look_up_docs, query)

        header = reference.index_header()
        on.append(f"docs_search ({header.get('pages')} pages @ {str(header.get('key'))[:7]})")

    if not examples_off:
        @register("examples_search", group="reference", writes=False)
        def examples_search(query: str = "") -> str:
            """Look up Fluent SDK sample code (.now.ts): tables, business rules, ACLs, flows, REST APIs, ATF tests, UI pages. Returns the best sample file's code and lists other files. Quote the returned code; do not write Fluent code from memory.

            Searches the text of every sample file for files that hold every keyword. An empty query
            lists the samples.

            Args:
              query: keywords that appear in the code, e.g. BusinessRule or ClientScript onChange; empty lists the samples
            """
            return _call(reference.look_up_examples, query)

        on.append(f"examples_search ({len(reference.sample_names())} samples)")

    if on:
        @register("reference_read", group="reference", writes=False)
        def reference_read(path: str, find: str = "", start_line: int = 1) -> str:
            """Read more of a documentation page or sample file, by a path docs_search or examples_search returned: the next part of a long page, another result, or the part that holds a word.

            Returns about 4500 characters and says which lines they are and how to get the next part.

            Args:
              path: the path from a search result, e.g. docs/api-reference/server-api-reference/c_GlideRecordScopedAPI.md
              find: a word or name to jump to, e.g. addQuery; empty starts at start_line
              start_line: line to start from, default 1; the previous part says the next value
            """
            return _call(reference.read, path, find=find, start_line=start_line)

        on.append("reference_read")

    off = [why for why in (docs_off, examples_off) if why]
    return ["reference tools: " + (", ".join(on) if on else "off")
            + (f" ({'; '.join(dict.fromkeys(off))})" if off else "")]
