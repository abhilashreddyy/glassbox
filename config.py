"""Model routing — every node can run on a different model.

A model is named "<provider>:<model>", e.g.
    ollama:gpt-oss:20b
    openai:gpt-4o-mini
    anthropic:claude-sonnet-5

Three roles, because they have genuinely different difficulty:
    sql_writer  — the hard one. Needs schema reasoning + correct SQL.
    verifier    — reads question + SQL + result, says if they match. Medium.
    answerer    — turns a result table into a sentence. Easy; a small model is fine.

Override per role without touching code:
    export DATA_AGENT_MODEL_SQL_WRITER=anthropic:claude-sonnet-5
    export DATA_AGENT_MODEL_DEFAULT=openai:gpt-4o-mini

That makes "how much model does each part actually need?" a measurable
question rather than a guess — swap one role, rerun the eval, compare.
"""

import os
from functools import lru_cache

DEFAULT = os.environ.get("DATA_AGENT_MODEL_DEFAULT", "ollama:gpt-oss:20b")

ROLES = ("sql_writer", "verifier", "answerer")


def model_name(role: str) -> str:
    return os.environ.get(f"DATA_AGENT_MODEL_{role.upper()}", DEFAULT)


@lru_cache(maxsize=None)
def get_model(role: str):
    """Build the chat model for a role. Imports are lazy so you only need the
    provider package for the providers you actually use."""
    spec = model_name(role)
    provider, _, name = spec.partition(":")  # partition: model names contain ':'
    if not name:
        raise ValueError(f"model spec must be '<provider>:<model>', got {spec!r}")

    if provider == "ollama":
        from langchain_ollama import ChatOllama

        # num_ctx matters: Ollama's default window silently truncates long
        # prompts, and a truncated schema looks exactly like a stupid model.
        return ChatOllama(model=name, temperature=0.0, num_ctx=16384)

    if provider == "openai":
        from langchain_openai import ChatOpenAI  # pip install langchain-openai

        return ChatOpenAI(model=name, temperature=0.0)

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic  # pip install langchain-anthropic

        return ChatAnthropic(model=name, temperature=0.0)

    raise ValueError(f"unknown provider {provider!r} in {spec!r}")


def active_models() -> dict:
    """What the UI displays, so you always know which model produced a run."""
    return {role: model_name(role) for role in ROLES}
