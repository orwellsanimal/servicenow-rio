# tests/test_snapshot.py — making the instance-config snapshot from rio (#22), and refusing one that
# can't be trusted for SN_INSTANCE. No instance: now-data's exporter and client are replaced.

import json

import pytest

from rio import config, snapshot
from rio.tools import TOOLS, now

from test_now_tools import make_snapshot

now_data_client = pytest.importorskip("now_data.client")
now_data_export = pytest.importorskip("now_data.export")
now_config_verbs = pytest.importorskip("now_config.verbs")
now_config_loader = pytest.importorskip("now_config.loader")
REAL_CONFIGURE = now_data_client.configure  # captured before any test patches it


@pytest.fixture
def identity(monkeypatch):
    monkeypatch.setattr(config, "SN_INSTANCE", "dev1")
    monkeypatch.setattr(config, "SN_IDENTITY", "human")
    monkeypatch.setenv("SN_HUMAN_USER", "jo")
    monkeypatch.setenv("SN_HUMAN_PASSWORD", "pw")


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(now_data_client, "configure", lambda *a: seen.append(("configure", a)))
    return seen


def exporter(monkeypatch, calls, code, writes=None):
    """now-data's CLI, faked: records argv, optionally writes a snapshot into --out, returns code."""
    def main(argv):
        calls.append(("export", argv))
        if writes:
            writes(argv[argv.index("--out") + 1])
        return code
    monkeypatch.setattr(now_data_export, "main", main)


def full(out):
    from pathlib import Path
    make_snapshot(Path(out))


# --- python -m rio.snapshot --------------------------------------------------------------

def test_rio_identity_reaches_the_exporter_before_it_runs(identity, calls, monkeypatch, capsys, tmp_path):
    # now-mcp-export alone reads SN_USER/SN_PASSWORD; rio's .env holds an SN_IDENTITY pair.
    exporter(monkeypatch, calls, 0, writes=full)
    out = tmp_path / "snap"
    assert snapshot.main(["--out", str(out)]) == 0
    assert calls == [("configure", ("dev1", "jo", "pw")), ("export", ["--out", str(out.resolve())])]
    assert f"INSTANCE_CONFIG_ROOT={out.resolve()}" in capsys.readouterr().out


def test_defaults_to_snapshot_and_passes_only(identity, calls, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    exporter(monkeypatch, calls, 0)
    snapshot.main(["--only", "schema"])
    assert calls[-1] == ("export", ["--out", str(tmp_path / "snapshot"), "--only", "schema"])


def test_a_tilde_is_expanded_as_the_exporter_expands_it(identity, calls, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path.parent)  # unexpanded, `~/snap` would land in a literal ./~ here, not in the repo
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
    monkeypatch.setenv("HOME", str(tmp_path))
    exporter(monkeypatch, calls, 0, writes=full)
    assert snapshot.main(["--out", "~/snap"]) == 0
    assert calls[-1] == ("export", ["--out", str((tmp_path / "snap").resolve())])
    assert f"INSTANCE_CONFIG_ROOT={(tmp_path / 'snap').resolve()}" in capsys.readouterr().out


@pytest.mark.parametrize("only", ["security", "schema"])
def test_no_env_line_unless_the_schema_rio_needs_is_there(identity, calls, monkeypatch, capsys, tmp_path, only):
    # --only schema writes tables + columns + choices, which is all rio needs; roles.json etc. are optional.
    def writes(out):
        from pathlib import Path
        if only == "schema":
            make_snapshot(Path(out))
    exporter(monkeypatch, calls, 0, writes=writes)
    assert snapshot.main(["--out", str(tmp_path / "s"), "--only", only]) == 0
    out = capsys.readouterr().out
    if only == "schema":
        assert "INSTANCE_CONFIG_ROOT=" in out
    else:
        assert "INSTANCE_CONFIG_ROOT=" not in out and "Not usable by rio yet" in out and "schema/tables.json" in out


def test_a_failed_export_is_a_failed_command_with_no_env_line(identity, calls, monkeypatch, capsys, tmp_path):
    exporter(monkeypatch, calls, 1, writes=full)  # now-mcp#16: 1 also when nothing was written
    assert snapshot.main(["--out", str(tmp_path)]) == 1
    assert "INSTANCE_CONFIG_ROOT=" not in capsys.readouterr().out


def test_without_credentials_nothing_runs(calls, monkeypatch, capsys):
    monkeypatch.setattr(config, "SN_INSTANCE", "")
    exporter(monkeypatch, calls, 0)
    assert snapshot.main([]) == 1
    assert calls == [] and "SN_INSTANCE is not set" in capsys.readouterr().err


def test_a_password_of_spaces_is_an_error_not_a_traceback(identity, monkeypatch, capsys):
    monkeypatch.setenv("SN_HUMAN_PASSWORD", "   ")  # gets past sn_config_problem(); configure() refuses it
    assert snapshot.main([]) == 1
    assert "Can't export: configure() needs" in capsys.readouterr().err


# --- the startup check: a snapshot answers only for its own instance ----------------------

@pytest.fixture
def snap(tmp_path, monkeypatch, identity):
    monkeypatch.setattr(config, "INSTANCE_CONFIG_ROOT", str(tmp_path))
    monkeypatch.setattr(now_data_client, "configure", lambda *a: None)
    monkeypatch.setattr(now, "_check_fields", lambda: ("off for this test", ""))  # no socket
    monkeypatch.setattr(now_config_verbs, "use_snapshot", lambda root: None)
    monkeypatch.setattr(now_config_verbs, "now_field_exists", lambda table, field: {"exists": True})

    def no_roles_file(role):  # as on a login that can't read sys_user_role: the exporter skipped it
        raise now_config_loader.SnapshotNotFound(f"Missing snapshot file: {tmp_path / 'security' / 'roles.json'}")
    monkeypatch.setattr(now_config_verbs, "now_role_exists", no_roles_file)
    return tmp_path


def config_line(notes):
    return next(n for n in notes if n.startswith("now-config tools"))


def instance_json(root, value):
    (root / "instance.json").write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")


@pytest.mark.parametrize("url", [
    "https://dev1.service-now.com", "https://DEV1.service-now.com/", "dev1",
    "https://dev1.service-now.com:443", "http://dev1.service-now.com",  # as now-data's exporter compares
])
def test_a_snapshot_of_this_instance_is_used(snap, url):
    make_snapshot(snap, url)
    assert config_line(now.load()) == "now-config tools: field_exists, choice_values"
    assert {"now_field_exists", "now_choice_values"} <= set(TOOLS)


def test_optional_files_are_not_needed(snap):
    # A corporate login may not read sys_user_role: the exporter skips roles.json, rio doesn't care.
    make_snapshot(snap)
    assert not (snap / "security" / "roles.json").exists()
    assert config_line(now.load()) == "now-config tools: field_exists, choice_values"


def test_a_snapshot_of_another_instance_is_refused(snap):
    # A lab snapshot would answer confidently, and wrongly, about the work instance's choices.
    make_snapshot(snap, "https://dev442835.service-now.com")
    line = config_line(now.load())
    assert line.startswith("now-config tools: off (the snapshot is of dev442835.service-now.com, "
                           "but SN_INSTANCE is dev1.service-now.com")
    assert "python -m rio.snapshot" in line
    assert not {"now_field_exists", "now_choice_values"} & set(TOOLS)


@pytest.mark.parametrize("bad,why", [
    (None, "has no instance.json"),                    # a re-export may have died part-way
    ("not json", "instance.json is unreadable"),
    (["https://dev1.service-now.com"], "instance.json is unreadable"),
    ({"instance_url": 123}, "instance_url is a int"),
    ({"instance_url": None}, "instance_url is a NoneType"),
    ({}, "instance.json is unreadable"),
])
def test_a_snapshot_rio_cannot_place_is_refused(snap, bad, why):
    make_snapshot(snap)
    if bad is None:
        (snap / "instance.json").unlink()
    else:
        instance_json(snap, bad)
    line = config_line(now.load())
    assert line.startswith("now-config tools: off (") and why in line, line
    assert not {"now_field_exists", "now_choice_values"} & set(TOOLS)


def test_an_unreadable_sn_instance_refuses_the_snapshot(snap, monkeypatch):
    make_snapshot(snap)
    monkeypatch.setattr(config, "SN_INSTANCE", "https://")
    assert config_line(now.load()).startswith("now-config tools: off (can't check the snapshot against SN_INSTANCE")


def test_a_snapshot_without_its_schema_is_refused(snap):
    make_snapshot(snap)
    (snap / "schema" / "choices.json").unlink()
    line = config_line(now.load())
    assert "has no schema/choices.json" in line and "without --only" in line


def test_no_snapshot_says_how_to_make_one(snap, monkeypatch):
    monkeypatch.setattr(config, "INSTANCE_CONFIG_ROOT", "")
    line = config_line(now.load())
    assert "INSTANCE_CONFIG_ROOT not set" in line and "python -m rio.snapshot" in line


def test_a_password_of_spaces_turns_the_data_tools_off_not_a_traceback(snap, monkeypatch):
    # The real configure() refuses blank values before it changes anything, so nothing leaks.
    monkeypatch.setattr(now_data_client, "configure", REAL_CONFIGURE)
    monkeypatch.setenv("SN_HUMAN_PASSWORD", "   ")
    make_snapshot(snap)
    notes = now.load()
    assert any(n.startswith("now-data tools: off (configure() needs") for n in notes), notes
