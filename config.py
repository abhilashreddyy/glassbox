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

        # num_ctx matters twice over. Too small and Ollama silently truncates
        # the prompt, which looks exactly like a stupid model. Too large and
        # the KV cache costs real memory: measured here, 16384 holds 15 GB
        # resident against 14 GB at 8192, on a 24 GB machine where that
        # gigabyte decides whether the run swaps.
        # Largest real prompt is ~2,100 tokens (sql_writer with the schema,
        # profile and glossary), so 8192 is ~3.5x headroom.
        return ChatOllama(model=name, temperature=0.0, num_ctx=8192)

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
