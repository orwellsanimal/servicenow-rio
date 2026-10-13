# tests/test_config.py — credentials come from the environment, with no coded defaults, and
# servicenow-coder's SN_USER / SN_PASSWORD pair is accepted as the `legacy` identity.

import pytest

from rio import config


@pytest.fixture
def env(monkeypatch):
    for v in ("SN_INSTANCE", "SN_IDENTITY", "SN_HUMAN_USER", "SN_HUMAN_PASSWORD", "SN_USER", "SN_PASSWORD"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(config, "SN_INSTANCE", "")
    monkeypatch.setattr(config, "SN_IDENTITY", "human")
    return monkeypatch


def test_unset_means_not_configured(env):
    assert config.sn_config_problem().startswith("SN_INSTANCE is not set")
    assert config.sn_instance_url() == ""


def test_a_bare_name_becomes_the_instance_url(env):
    env.setattr(config, "SN_INSTANCE", "dev442835")
    assert config.sn_instance_url() == "https://dev442835.service-now.com"
    env.setattr(config, "SN_INSTANCE", "https://dev442835.service-now.com/")
    assert config.sn_instance_url() == "https://dev442835.service-now.com"


def test_the_identity_pair_must_both_be_set(env):
    env.setattr(config, "SN_INSTANCE", "dev442835")
    env.setenv("SN_HUMAN_USER", "jody")
    assert "SN_HUMAN_USER / SN_HUMAN_PASSWORD are not both set" in config.sn_config_problem()
    env.setenv("SN_HUMAN_PASSWORD", "x")
    assert config.sn_config_problem() == "" and config.sn_credentials() == ("jody", "x")


def test_servicenow_coders_names_are_the_legacy_identity(env):
    env.setattr(config, "SN_INSTANCE", "dev442835")
    env.setattr(config, "SN_IDENTITY", "legacy")
    env.setenv("SN_USER", "coder")
    env.setenv("SN_PASSWORD", "y")
    assert config.sn_config_problem() == "" and config.sn_credentials() == ("coder", "y")


def test_an_unknown_identity_is_named(env):
    env.setattr(config, "SN_INSTANCE", "dev442835")
    env.setattr(config, "SN_IDENTITY", "robot")
    assert "SN_IDENTITY='robot' is not one of" in config.sn_config_problem()
