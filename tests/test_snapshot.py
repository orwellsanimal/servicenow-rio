# tests/test_snapshot.py — making the instance-config snapshot from rio (#22), and refusing one of
# another instance. No instance: now-data's exporter and client are replaced by recorders.

import json

import pytest

from rio import config, snapshot
from rio.tools import TOOLS, now

now_data_client = pytest.importorskip("now_data.client")
now_data_export = pytest.importorskip("now_data.export")
now_config_verbs = pytest.importorskip("now_config.verbs")


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


def exporter(monkeypatch, calls, code):
    monkeypatch.setattr(now_data_export, "main", lambda argv: calls.append(("export", argv)) or code)


# --- python -m rio.snapshot --------------------------------------------------------------

def test_rio_identity_reaches_the_exporter_before_it_runs(identity, calls, monkeypatch, capsys, tmp_path):
    # now-mcp-export alone reads SN_USER/SN_PASSWORD; rio's .env holds an SN_IDENTITY pair.
    (tmp_path / "snap" / "schema").mkdir(parents=True)
    (tmp_path / "snap" / "schema" / "tables.json").write_text("{}")
    exporter(monkeypatch, calls, 0)
    assert snapshot.main(["--out", str(tmp_path / "snap")]) == 0
    assert calls == [("configure", ("dev1", "jo", "pw")), ("export", ["--out", str(tmp_path / "snap")])]
    assert f"INSTANCE_CONFIG_ROOT={tmp_path / 'snap'}" in capsys.readouterr().out


def test_defaults_to_snapshot_and_passes_only(identity, calls, monkeypatch):
    exporter(monkeypatch, calls, 0)
    snapshot.main(["--only", "schema"])
    assert calls[-1] == ("export", ["--out", "snapshot", "--only", "schema"])


def test_no_env_line_for_a_folder_now_config_cannot_load(identity, calls, monkeypatch, capsys, tmp_path):
    exporter(monkeypatch, calls, 0)  # --only security into an empty folder: no schema to load
    assert snapshot.main(["--out", str(tmp_path), "--only", "security"]) == 0
    out = capsys.readouterr().out
    assert "INSTANCE_CONFIG_ROOT=" not in out and "has no schema yet" in out


def test_a_failed_export_is_a_failed_command_with_no_env_line(identity, calls, monkeypatch, capsys):
    exporter(monkeypatch, calls, 1)  # now-mcp#16: 1 also when nothing was written
    assert snapshot.main([]) == 1
    assert "INSTANCE_CONFIG_ROOT=" not in capsys.readouterr().out


def test_without_credentials_nothing_runs(calls, monkeypatch, capsys):
    monkeypatch.setattr(config, "SN_INSTANCE", "")
    exporter(monkeypatch, calls, 0)
    assert snapshot.main([]) == 1
    assert calls == [] and "SN_INSTANCE is not set" in capsys.readouterr().err


# --- the startup check: a snapshot answers only for its own instance ----------------------

@pytest.fixture
def snap(tmp_path, monkeypatch, identity):
    monkeypatch.setattr(config, "INSTANCE_CONFIG_ROOT", str(tmp_path))
    monkeypatch.setattr(now_data_client, "configure", lambda *a: None)
    monkeypatch.setattr(now, "_check_fields", lambda: ("off for this test", ""))  # no socket
    monkeypatch.setattr(now_config_verbs, "use_snapshot", lambda root: None)
    monkeypatch.setattr(now_config_verbs, "now_role_exists", lambda role: {"exists": True})

    def write(url):
        (tmp_path / "instance.json").write_text(json.dumps({"instance_url": url}), encoding="utf-8")
    return write


def config_line(notes):
    return next(n for n in notes if n.startswith("now-config tools"))


@pytest.mark.parametrize("url", ["https://dev1.service-now.com", "https://DEV1.service-now.com/", "dev1"])
def test_a_snapshot_of_this_instance_is_used(snap, url):
    snap(url)
    line = config_line(now.load())
    assert line == "now-config tools: field_exists, choice_values"
    assert {"now_field_exists", "now_choice_values"} <= set(TOOLS)


def test_a_snapshot_of_another_instance_is_refused(snap):
    # A lab snapshot would answer confidently, and wrongly, about the work instance's choices.
    snap("https://dev442835.service-now.com")
    line = config_line(now.load())
    assert line.startswith("now-config tools: off (the snapshot is of https://dev442835.service-now.com, "
                           "but SN_INSTANCE is https://dev1.service-now.com")
    assert "python -m rio.snapshot" in line
    assert not {"now_field_exists", "now_choice_values"} & set(TOOLS)


def test_a_snapshot_without_instance_json_is_used_with_a_warning(snap):
    line = config_line(now.load())
    assert line.startswith("now-config tools: field_exists, choice_values [warning: can't tell which instance")


def test_no_snapshot_says_how_to_make_one(snap, monkeypatch):
    monkeypatch.setattr(config, "INSTANCE_CONFIG_ROOT", "")
    line = config_line(now.load())
    assert "INSTANCE_CONFIG_ROOT not set" in line and "python -m rio.snapshot" in line
