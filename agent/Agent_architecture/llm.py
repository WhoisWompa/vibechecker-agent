"""LLM factory. All config lives in agent/.env — edit that file, not this one.

Shared by both teams. Every model built here carries two callback handlers:
``run_log.handler`` (the analysis team's event log, a no-op until
``run_log.start_run`` is called) and whatever sits in ``default_callbacks``
(the navigation team's token tracker puts itself there for the after-close
pass, so the analysis team's spend counts toward the run budget and shows up
in summary.json).
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_AGENT_DIR = str(Path(__file__).resolve().parent)
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

import run_log  # noqa: E402

try:
    from langchain.chat_models import init_chat_model
except ImportError:
    from langchain_classic.chat_models import init_chat_model

_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(_ENV_PATH)

# Handlers attached to every model built from now on, on top of run_log.handler.
# The navigation runner appends its Tracker here before the analysis pass.
default_callbacks: list = []


def resolve_model(agent: str | None = None) -> str | None:
    """agent: "hypothesis" | "supervisor" | "researcher" | "daily" | "intraday" | "memory" | None.
    Pure name-resolution logic, kept separate from get_llm so it can be tested
    without constructing a real model (no API calls, no key required)."""
    return (os.getenv(f"{agent.upper()}_MODEL") if agent else None) or os.getenv("DEFAULT_MODEL")


def get_llm(agent: str | None = None, max_tokens: int = 4096, callbacks: list | None = None):
    """agent: "hypothesis" | "supervisor" | "researcher" | "daily" | "intraday" | "memory" | None.
    callbacks: optional LangChain callback handlers (e.g. the navigation team's
    token tracker); the agent name travels in the run metadata so every call
    is attributed. run_log.handler and ``default_callbacks`` are always attached."""
    model = resolve_model(agent)
    handlers = [run_log.handler] + list(default_callbacks) + list(callbacks or [])
    kwargs = {"max_tokens": max_tokens, "callbacks": handlers}
    api_key = os.getenv("API_KEY")
    if api_key:
        kwargs["api_key"] = api_key
    provider = (os.getenv("PROVIDER") or "").lower()
    if provider.startswith("bedrock"):
        # the hackathon stack: Claude on AWS Bedrock through the SSO profile from the training
        kwargs["region_name"] = os.getenv("AWS_DEFAULT_REGION") or "ap-southeast-1"
        if os.getenv("AWS_PROFILE"):
            kwargs["credentials_profile_name"] = os.getenv("AWS_PROFILE")
        kwargs.pop("api_key", None)
    if agent:
        kwargs["metadata"] = {"agent": agent}
    return init_chat_model(model, model_provider=os.getenv("PROVIDER"), **kwargs)
