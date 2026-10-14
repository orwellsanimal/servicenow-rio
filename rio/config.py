# rio/config.py — every knob rio reads, from the environment, with no coded credential defaults.
#
# Carried from whitcortex config.py (the SN_IDENTITY model, #2599) and trimmed. servicenow-coder's
# `SN_USER` / `SN_PASSWORD` pair is read as the `legacy` identity so one `.env` can serve both
# repos on the same machine.

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

OLLAMA_URL: str = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
MODEL: str = os.getenv("MODEL", "qwen3:14b")
NUM_CTX: int = int(os.getenv("NUM_CTX", "8192"))          # ADR-0002: a CPU laptop, not the lab's 16k
LLM_TIMEOUT: int = int(os.getenv("LLM_TIMEOUT", "300"))   # CPU decode is slow; the first call also loads
# A ceiling on tokens generated per call. A model that loops on a tool call would otherwise generate
# until LLM_TIMEOUT, and Ollama keeps the runner busy on an abandoned request after rio gives up.
NUM_PREDICT: int = int(os.getenv("NUM_PREDICT", "1024"))

# On an instance rio must not change (the corporate one), no write tool is offered or dispatched,
# approved or not (ADR-0003).
READ_ONLY: bool = os.getenv("RIO_READ_ONLY", "").strip().lower() in ("1", "true", "yes")

# The instance-config snapshot now-config reads (servicenow-coder's layout); it must be a snapshot
# of THIS instance. Unset: the now-config tools are not offered.
INSTANCE_CONFIG_ROOT: str = os.getenv("INSTANCE_CONFIG_ROOT", "").strip()

# ── ServiceNow identity ──────────────────────────────────────────────────────
SN_INSTANCE: str = os.getenv("SN_INSTANCE", "").strip()
SN_IDENTITY: str = os.getenv("SN_IDENTITY", "").strip().lower() or "human"
SN_IDENTITY_VARS: dict[str, tuple[str, str]] = {
    "human": ("SN_HUMAN_USER", "SN_HUMAN_PASSWORD"),
    "machine": ("SN_MACHINE_USER", "SN_MACHINE_PASSWORD"),
    "agent": ("SN_AGENT_USER", "SN_AGENT_PASSWORD"),
    "legacy": ("SN_USER", "SN_PASSWORD"),      # servicenow-coder's names
}


def sn_instance_url() -> str:
    """The instance as an https URL, from a bare name or a URL; "" when unset."""
    if not SN_INSTANCE:
        return ""
    if SN_INSTANCE.startswith("http"):
        return SN_INSTANCE.rstrip("/")
    return f"https://{SN_INSTANCE}.service-now.com"


def sn_credentials() -> tuple[str, str]:
    """(user, password) for SN_IDENTITY, or ("", "") when that pair is not set."""
    user_var, pw_var = SN_IDENTITY_VARS.get(SN_IDENTITY, SN_IDENTITY_VARS["human"])
    return os.getenv(user_var, ""), os.getenv(pw_var, "")


def sn_config_problem() -> str:
    """Why ServiceNow is not usable, or "" when it is."""
    if not SN_INSTANCE:
        return "SN_INSTANCE is not set (bare name, e.g. dev442835)."
    if SN_IDENTITY not in SN_IDENTITY_VARS:
        return f"SN_IDENTITY={SN_IDENTITY!r} is not one of {', '.join(SN_IDENTITY_VARS)}."
    user, pw = sn_credentials()
    if not user or not pw:
        u, p = SN_IDENTITY_VARS[SN_IDENTITY]
        return f"{u} / {p} are not both set for SN_IDENTITY={SN_IDENTITY}."
    return ""
