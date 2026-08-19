"""Model routing — every node can run on a different model.

A model is named "<provider>:<model>", e.g.
    ollama:gpt-oss:20b                    local, free
    openrouter:qwen/qwen3.8-27b           any hosted model, one key
    openrouter:deepseek/deepseek-v4-flash
    openrouter:google/gemini-3.7-flash
    bedrock:qwen.qwen3-32b-v1:0           AWS, cheapest hosted Qwen
    openai:gpt-5-mini
    anthropic:claude-sonnet-5

`openrouter` is the useful one for this project: a single key reaches open
models, OpenAI, Anthropic and Google alike, so comparing them per role is an
env-var change rather than an integration.

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

FALLBACK = "ollama:gpt-oss:20b"

ROLES = ("sql_writer", "verifier", "answerer")


def model_name(role: str) -> str:
    """Resolved per call, not at import: a test or a sweep that sets the env
    var after importing this module should still take effect."""
    return os.environ.get(
        f"DATA_AGENT_MODEL_{role.upper()}",
        os.environ.get("DATA_AGENT_MODEL_DEFAULT", FALLBACK),
    )


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

    if provider == "openrouter":
        # OpenRouter speaks the OpenAI wire format, so the same adapter works —
        # only the base URL and key differ. Needs OPENROUTER_API_KEY.
        from langchain_openai import ChatOpenAI

        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            # The OpenAI SDK's own message names OPENAI_API_KEY here, which
            # sends you looking in the wrong place.
            raise RuntimeError(
                f"{spec} needs OPENROUTER_API_KEY. Get one at "
                "https://openrouter.ai/keys, then: "
                'echo \'export OPENROUTER_API_KEY="sk-or-..."\' >> ~/.zshrc'
            )
        return ChatOpenAI(
            model=name,
            temperature=0.0,
            base_url="https://openrouter.ai/api/v1",
            api_key=key,
        )

    if provider == "bedrock":
        # Bedrock's Converse API normalises across model families, so one
        # class covers Qwen, Llama, Claude and the rest. Region matters:
        # the Qwen3 catalogue is not in every region (us-west-2 has the
        # widest coverage). Auth comes from the normal AWS chain — no key
        # in this file.
        from langchain_aws import ChatBedrockConverse  # pip install langchain-aws

        return ChatBedrockConverse(
            model=name,
            temperature=0.0,
            region_name=os.environ.get("AWS_REGION", "us-west-2"),
        )

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic  # pip install langchain-anthropic

        return ChatAnthropic(model=name, temperature=0.0)

    raise ValueError(
        f"unknown provider {provider!r} in {spec!r} — "
        "expected ollama, openrouter, openai or anthropic"
    )


def active_models() -> dict:
    """What the UI displays, so you always know which model produced a run."""
    return {role: model_name(role) for role in ROLES}
