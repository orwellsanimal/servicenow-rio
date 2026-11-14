# tests/test_reference.py — the documentation index, the two searches, the windowed read, and the
# reference tools' menu (#27). Everything runs on a small tree under tmp_path: the suite needs
# neither submodule checked out.

import json
import subprocess

import pytest

from rio import config, reference
from rio.tools import MAX_RESULT_CHARS, TOOLS, execute_tool, schemas
from rio.tools import reference as reference_tools

PAGE = """---
title: {title}
description: "{description}"
canonical_url: https://www.servicenow.com/docs/r/{name}.html
product: {product}
---

# {title}

{body}
"""


def page(root, rel, title, description="", product="", body="Body."):
    f = root / "markdown" / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(PAGE.format(title=title, description=description, product=product, body=body,
                             name=f.stem), encoding="utf-8")
    return f


@pytest.fixture
def tree(tmp_path, monkeypatch):
    docs, examples = tmp_path / "servicenow-docs", tmp_path / "servicenow-sdk-examples"
    monkeypatch.setattr(config, "DOCS_ROOT", docs)
    monkeypatch.setattr(config, "EXAMPLES_ROOT", examples)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "ROOT", tmp_path)
    reference._loaded.clear()
    reference._examples.clear()

    (docs / "README.md").parent.mkdir(parents=True)
    (docs / "README.md").write_text("docs", encoding="utf-8")
    page(docs, "api-reference/server-api-reference/c_GlideRecordScopedAPI.md", "GlideRecord - Scoped",
         "Scoped GlideRecord is used for database operations.", "Server API reference",
         body="\n".join(["## Scoped GlideRecord - addQuery\\(String name, Object value\\)", "",
                         "Adds a filter. See [addEncodedQuery](https://raw.githubusercontent.com/x/y.md).", "",
                         "Set **glide.invalid\\_query.returns\\_no\\_rows** to true.", "",
                         "```", "gr.addQuery('active\\_flag', true); // [not a link](https://e.example/x)", "```", ""]
                        + [f"filler line {n}" for n in range(400)]))
    page(docs, "platform-security/access-control/acl-rule-types.md", "ACL types", "The types of access control rule.")
    page(docs, "integrate-applications/oracle-zcc.md", "Oracle", "Zero copy connector for Oracle.")
    page(docs, "api-reference/business-rules-classic/t_CreatingABusinessRule.md", "Create a business rule",
         "Create a business rule that runs when a record changes.")
    page(docs, "build-workflows/business-rules-classic/t_CreatingABusinessRule.md", "Create a business rule",
         "Create a business rule that runs when a record changes.")
    page(docs, "application-development/servicenow-sdk/business-rule-api-now-ts.md",
         "Business Rule API - ServiceNow Fluent", "Define business rules in source code.")

    br = examples / "businessrule-sample" / "src" / "fluent"
    br.mkdir(parents=True)
    (br / "business-rule-1.now.ts").write_text(
        "import { BusinessRule } from '@servicenow/sdk/core'\n\nBusinessRule({\n    table: 'sc_req_item',\n    when: 'before',\n})\n",
        encoding="utf-8")
    (examples / "businessrule-sample" / "README.md").write_text("# BusinessRule Sample\n", encoding="utf-8")
    (examples / "businessrule-sample" / "pnpm-lock.yaml").write_text("BusinessRule: lock\n", encoding="utf-8")
    nm = examples / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("BusinessRule vendored\n", encoding="utf-8")
    (examples / "table-sample" / "src").mkdir(parents=True)
    (examples / "table-sample" / "src" / "table.now.ts").write_text("export const t = Table({ name: 'x_t' })\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def indexed(tree):
    reference.build_index()
    return tree


def paths(out):
    return [r["path"] for r in out["results"]]


# ── The index ────────────────────────────────────────────────────────────────

def test_the_index_holds_each_pages_front_matter_and_its_header_is_the_first_line(tree):
    header = reference.build_index()
    assert header["pages"] == 6 and header["unread"] == 0
    first, *rows = reference.index_path().read_text(encoding="utf-8").splitlines()
    assert json.loads(first) == header
    assert ["platform-security/access-control/acl-rule-types.md", "ACL types",
            "The types of access control rule.", ""] in [json.loads(r) for r in rows]


def test_a_page_without_front_matter_is_indexed_by_its_heading_then_its_file_name(tree):
    (tree / "servicenow-docs" / "markdown" / "bare.md").write_text("# Only a heading\n\ntext\n", encoding="utf-8")
    (tree / "servicenow-docs" / "markdown" / "nothing-at-all.md").write_text("text\n", encoding="utf-8")
    reference.build_index()
    assert paths(reference.search_docs("only heading"))[0] == "docs/bare.md"
    assert reference.search_docs("nothing-at-all")["results"][0]["title"] == "nothing-at-all"


def test_the_docs_are_off_until_indexed_and_off_again_when_the_checkout_moves(tree):
    assert "not indexed yet" in reference.docs_problem()
    reference.build_index()
    assert reference.docs_problem() == ""
    (tree / "servicenow-docs" / "README.md").write_text("docs, refreshed copy", encoding="utf-8")
    assert "changed since it was indexed" in reference.docs_problem()


def test_no_checkout_says_how_to_get_one(tree, monkeypatch):
    monkeypatch.setattr(config, "DOCS_ROOT", tree / "absent")
    monkeypatch.setattr(config, "EXAMPLES_ROOT", tree / "absent-too")
    assert "python -m rio.reference" in reference.docs_problem()
    assert "python -m rio.reference" in reference.examples_problem()
    with pytest.raises(reference.Refusal):
        reference.build_index()


def test_a_folder_holding_only_git_metadata_is_not_a_checkout(tmp_path, monkeypatch):
    """What an un-initialised submodule looks like: the folder exists and is empty."""
    (tmp_path / "servicenow-sdk-examples").mkdir()
    monkeypatch.setattr(config, "EXAMPLES_ROOT", tmp_path / "servicenow-sdk-examples")
    assert "not checked out" in reference.examples_problem()


@pytest.mark.parametrize("head, ref_file", [
    ("0123456789abcdef0123456789abcdef01234567\n", None),            # detached: submodule update
    ("ref: refs/heads/brazil\n", "loose"),                           # on a branch: submodule add
    ("ref: refs/heads/brazil\n", "packed"),
])
def test_the_commit_is_read_from_the_submodules_git_files(tmp_path, head, ref_file):
    sha = "0123456789abcdef0123456789abcdef01234567"
    gitdir = tmp_path / ".git" / "modules" / "servicenow-docs"
    (gitdir / "refs" / "heads").mkdir(parents=True)
    (gitdir / "HEAD").write_text(head, encoding="utf-8")
    if ref_file == "loose":
        (gitdir / "refs" / "heads" / "brazil").write_text(sha + "\n", encoding="utf-8")
    if ref_file == "packed":
        (gitdir / "packed-refs").write_text(f"# pack-refs with: peeled\n{sha} refs/heads/brazil\n", encoding="utf-8")
    work = tmp_path / "servicenow-docs"
    work.mkdir()
    (work / ".git").write_text("gitdir: ../.git/modules/servicenow-docs\n", encoding="utf-8")
    assert reference.commit_of(work) == sha
    assert reference.commit_of(tmp_path / "not-a-repo") == ""


# ── Searching the documentation ──────────────────────────────────────────────

def test_a_whole_word_in_the_title_beats_the_same_letters_inside_another_word(indexed):
    """Measured on brazil: `ACL` put "Oracle" first when a match was any substring."""
    out = reference.search_docs("ACL")
    assert paths(out)[0] == "docs/platform-security/access-control/acl-rule-types.md"


def test_words_match_inside_a_camel_case_name_and_question_words_are_dropped(indexed):
    assert paths(reference.search_docs("how do I use glide record"))[0].endswith("c_GlideRecordScopedAPI.md")


def test_a_page_published_under_two_folders_is_listed_once(indexed):
    got = paths(reference.search_docs("create business rule"))
    assert got[:2] == ["docs/api-reference/business-rules-classic/t_CreatingABusinessRule.md",
                       "docs/application-development/servicenow-sdk/business-rule-api-now-ts.md"]
    assert "docs/build-workflows/business-rules-classic/t_CreatingABusinessRule.md" not in got


def test_a_search_says_when_nothing_or_not_everything_matched(indexed):
    assert reference.search_docs("zzzz")["count"] == 0 and "no page matches" in reference.search_docs("zzzz")["note"]
    partial = reference.search_docs("oracle zzzz")
    assert paths(partial) == ["docs/integrate-applications/oracle-zcc.md"] and "every keyword" in partial["note"]
    assert "note" not in reference.search_docs("oracle")
    with pytest.raises(reference.Refusal, match="empty"):
        reference.search_docs("  ")


# ── Searching the samples ────────────────────────────────────────────────────

def test_an_empty_query_lists_the_samples_and_not_node_modules(tree):
    assert reference.search_examples("")["samples"] == ["businessrule-sample", "table-sample"]


def test_sample_search_returns_matching_lines_source_first_and_skips_vendored_and_lock_files(tree):
    out = reference.search_examples("BusinessRule")
    assert paths(out) == ["examples/businessrule-sample/src/fluent/business-rule-1.now.ts",
                          "examples/businessrule-sample/README.md"]
    assert out["results"][0]["lines"][0] == "1: import { BusinessRule } from '@servicenow/sdk/core'"
    assert reference.search_examples("BusinessRule sc_req_item")["count"] == 1     # every keyword, not any
    assert reference.search_examples("zzzz")["count"] == 0


# ── Reading ──────────────────────────────────────────────────────────────────

GR = "docs/api-reference/server-api-reference/c_GlideRecordScopedAPI.md"


def test_a_read_is_a_window_that_says_how_to_get_the_next_one(indexed):
    first = reference.read(GR)
    assert first.startswith("GlideRecord - Scoped — https://www.servicenow.com/docs/r/c_GlideRecordScopedAPI.html")
    assert "title:" not in first                       # the front matter is not page text
    assert len(first) < MAX_RESULT_CHARS               # so execute_tool never cuts the footer off
    footer = first.splitlines()[-1]
    assert footer.startswith("[lines 1-") and f'continue with reference_read("{GR}", start_line=' in footer
    nxt = int(footer.split("start_line=")[1].rstrip(")]"))
    second = reference.read(GR, start_line=nxt)
    assert second.splitlines()[-1].startswith(f"[lines {nxt}-")
    last = reference.read(GR, start_line=400)
    assert last.splitlines()[-1].endswith("end of file]")


def test_prose_loses_link_urls_and_markdown_escapes_but_code_is_untouched(indexed):
    text = reference.read(GR)
    assert "## Scoped GlideRecord - addQuery(String name, Object value)" in text
    assert "See addEncodedQuery." in text and "raw.githubusercontent.com" not in text
    assert "glide.invalid_query.returns_no_rows" in text
    assert "gr.addQuery('active\\_flag', true); // [not a link](https://e.example/x)" in text


def test_find_jumps_to_the_first_line_holding_the_word_with_one_line_of_lead_in(indexed):
    text = reference.read(GR, find="filler line 200")
    body = text.split("\n\n", 1)[1]
    assert body.startswith("filler line 199\nfiller line 200\n")
    with pytest.raises(reference.Refusal, match="Its headings: GlideRecord - Scoped; Scoped GlideRecord - addQuery"):
        reference.read(GR, find="deleteMultiple")


def test_a_sample_file_is_read_as_it_is(tree):
    text = reference.read("examples/businessrule-sample/src/fluent/business-rule-1.now.ts")
    assert text.startswith("import { BusinessRule } from '@servicenow/sdk/core'")
    assert text.splitlines()[-1] == "[lines 1-6 of 6; end of file]"


@pytest.mark.parametrize("path", [
    "docs/../../.env", "examples/../secret.txt", "examples/../../secret.txt", "docs\\..\\..\\secret.txt",
    "/etc/passwd", "secret.txt", "docs", "examples/.git/config",
    "docs/api-reference",                               # a folder
    "docs/no-such-page.md",
])
def test_a_path_outside_the_two_folders_reads_nothing(tree, path):
    (tree / "secret.txt").write_text("SN_PASSWORD=hunter2", encoding="utf-8")
    (tree / ".env").write_text("SN_PASSWORD=hunter2", encoding="utf-8")
    (tree / "servicenow-sdk-examples" / ".git").mkdir()
    (tree / "servicenow-sdk-examples" / ".git" / "config").write_text("[remote]", encoding="utf-8")
    with pytest.raises(reference.Refusal):
        reference.read(path)


# ── Search and read in one call ──────────────────────────────────────────────

def test_a_docs_lookup_carries_the_best_pages_text_opened_at_the_word_the_index_did_not_match(indexed):
    """One tool round per turn (ADR-0003): a search that returned only paths left qwen3:14b to
    answer from memory, and it invented Fluent properties."""
    page(indexed / "servicenow-docs", "api-reference/c_GlideRecordClientSideAPI.md", "GlideRecord - Client",
         body="## Client side GlideRecord - addQuery(String name)\n\nNo encoded queries here.")
    reference.build_index()
    out = reference.look_up_docs("GlideRecord addQuery")
    lines = out.splitlines()
    assert lines[0] == f"Best match: {GR}"
    assert lines[3] == "## Scoped GlideRecord - addQuery(String name, Object value)"     # the section, not the top
    assert ("- docs/api-reference/c_GlideRecordClientSideAPI.md — GlideRecord - Client"
            in out.split("Other pages (read one with reference_read):")[1])
    assert len(out) < MAX_RESULT_CHARS


def test_a_docs_lookup_passes_over_a_page_that_does_not_hold_the_word(indexed):
    page(indexed / "servicenow-docs", "api-reference/c_GlideRecordClientSideAPI.md", "GlideRecord - Client",
         body="## Client side GlideRecord - addQuery(String name)\n\nNothing about that property.")
    reference.build_index()
    out = reference.look_up_docs("GlideRecord returns_no_rows")
    assert out.startswith(f"Best match: {GR}\nGlideRecord - Scoped — https://")
    assert out.splitlines()[3] == "Set **glide.invalid_query.returns_no_rows** to true."


def test_a_docs_lookup_with_nothing_to_jump_to_opens_at_the_first_line_of_text(indexed):
    out = reference.look_up_docs("ACL")
    assert out.splitlines()[:4] == ["Best match: docs/platform-security/access-control/acl-rule-types.md",
                                    "ACL types — https://www.servicenow.com/docs/r/acl-rule-types.html", "",
                                    "# ACL types"]
    assert reference.look_up_docs("zzzz").startswith("no page matches")


def test_a_sample_lookup_carries_the_best_files_code_and_lists_the_rest(tree):
    out = reference.look_up_examples("BusinessRule")
    assert out.startswith("Best match: examples/businessrule-sample/src/fluent/business-rule-1.now.ts\n\n"
                          "import { BusinessRule } from '@servicenow/sdk/core'")
    assert "    table: 'sc_req_item'," in out and "[lines 1-6 of 6; end of file]" in out
    assert out.endswith("Other files (read one with reference_read):\n- examples/businessrule-sample/README.md")
    assert reference.look_up_examples("").startswith("The samples: businessrule-sample, table-sample.")
    assert reference.look_up_examples("zzzz").startswith("no sample file holds every keyword")


def test_the_longest_lookup_and_the_longest_read_both_fit_the_tool_result_cap(indexed):
    """execute_tool cuts a result at MAX_RESULT_CHARS, and the footer that says how to continue is
    at the end. Eight results with paths as long as the docs' longest (197) is the worst case."""
    docs = indexed / "servicenow-docs"
    for n in range(10):
        page(docs, f"platform-security/{'x' * 150}/filler-page-{n}-{'y' * 20}.md", f"Filler page {n} {'t' * 80}",
             body="\n".join(f"filler line {i} " + "z" * 80 for i in range(200)))
    reference.build_index()
    out = reference.look_up_docs("filler page")
    assert "continue with reference_read(" in out
    assert 1 <= out.count("\n- docs/") < 7              # as many of the other seven as fit, each whole
    assert out.splitlines()[-1].endswith("t" * 80)
    assert len(out) < MAX_RESULT_CHARS
    assert len(reference.read(paths(reference.search_docs("filler page"))[0])) < MAX_RESULT_CHARS


# ── The tools ────────────────────────────────────────────────────────────────

def test_the_tools_load_without_servicenow_and_none_of_them_writes(indexed):
    (note,) = reference_tools.load()
    assert note.startswith("reference tools: docs_search (6 pages @ copy:") and "examples_search (2 samples)" in note
    assert set(TOOLS) == {"docs_search", "examples_search", "reference_read"}
    assert not any(t.writes for t in TOOLS.values())
    menu = {s["function"]["name"]: s["function"] for s in schemas()}
    assert menu["docs_search"]["parameters"]["required"] == ["query"]
    assert menu["reference_read"]["parameters"]["properties"]["start_line"]["type"] == "integer"
    assert all("description" in p for f in menu.values() for p in f["parameters"]["properties"].values())


def test_only_what_is_on_disk_is_offered_and_the_banner_says_how_to_get_the_rest(tree):
    (note,) = reference_tools.load()                     # samples there, docs not indexed
    assert set(TOOLS) == {"examples_search", "reference_read"}
    assert "not indexed yet" in note and "python -m rio.reference" in note


def test_nothing_on_disk_offers_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DOCS_ROOT", tmp_path / "a")
    monkeypatch.setattr(config, "EXAMPLES_ROOT", tmp_path / "b")
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    (note,) = reference_tools.load()
    assert note.startswith("reference tools: off (") and not TOOLS


def test_a_refusal_reaches_the_model_as_a_tool_error_it_can_act_on(indexed):
    reference_tools.load()
    assert execute_tool("docs_search", {"query": "ACL"}).startswith(
        "Best match: docs/platform-security/access-control/acl-rule-types.md")
    out = execute_tool("reference_read", {"path": "docs/../../.env"})
    assert out == "Tool error (reference_read): 'docs/../../.env' is outside the docs folder."
    assert execute_tool("reference_read", {"path": GR, "find": "filler line 5"}).startswith("GlideRecord - Scoped")
    assert execute_tool("examples_search", {}).startswith("The samples: businessrule-sample, table-sample.")
    assert "[truncated:" not in execute_tool("docs_search", {"query": "GlideRecord"})


# ── python -m rio.reference ──────────────────────────────────────────────────

class Done:
    def __init__(self, returncode=0, stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, "", stderr


def test_the_command_checks_out_the_pinned_commits_shallow_then_indexes(tree, monkeypatch, capsys):
    (tree / ".git").mkdir()
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)) or Done())
    assert reference.main([]) == 0
    (cmd, kw), = calls
    assert cmd == ["git", "-c", "core.longpaths=true", "submodule", "update", "--init", "--depth", "1", "--",
                   "servicenow-docs", "servicenow-sdk-examples"]
    assert "--remote" not in cmd and kw["cwd"] == tree       # the pin, not upstream's head
    out = capsys.readouterr().out
    assert "Indexed 6 pages" in out and "Restart rio" in out
    assert reference.docs_problem() == ""


def test_a_failed_checkout_indexes_nothing_and_says_why(tree, monkeypatch, capsys):
    (tree / ".git").mkdir()
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: Done(128, "fatal: unable to access 'https://github.com/'"))
    assert reference.main([]) == 1
    assert "unable to access" in capsys.readouterr().err
    assert not reference.index_path().exists()


def test_without_git_the_command_names_the_way_out(tree, monkeypatch, capsys):
    assert reference.main([]) == 1                           # tmp_path has no .git: a zip download
    assert "RIO_DOCS_ROOT" in capsys.readouterr().err
    (tree / ".git").mkdir()

    def no_git(cmd, **kw):
        raise FileNotFoundError("git")
    monkeypatch.setattr(subprocess, "run", no_git)
    assert reference.main([]) == 1
    assert "Git is not installed" in capsys.readouterr().err


def test_a_copied_folder_set_in_env_is_indexed_without_git(tree, monkeypatch, capsys):
    """RIO_DOCS_ROOT / RIO_EXAMPLES_ROOT: the operator's own copies, which Git here doesn't manage."""
    for name in ("servicenow-docs", "servicenow-sdk-examples"):
        (tree / name).rename(tree / f"copy-of-{name}")
    monkeypatch.setattr(config, "DOCS_ROOT", tree / "copy-of-servicenow-docs")
    monkeypatch.setattr(config, "EXAMPLES_ROOT", tree / "copy-of-servicenow-sdk-examples")

    def no_git(cmd, **kw):
        raise AssertionError("ran git for a folder it doesn't manage")
    monkeypatch.setattr(subprocess, "run", no_git)
    assert reference.main([]) == 0
    assert reference.main(["--status"]) == 0
    assert "usable:  yes" in capsys.readouterr().out


def test_status_changes_nothing_and_exits_nonzero_when_something_is_missing(tree, capsys):
    assert reference.main(["--status"]) == 1
    assert "not indexed yet" in capsys.readouterr().out
    assert not reference.index_path().exists()
