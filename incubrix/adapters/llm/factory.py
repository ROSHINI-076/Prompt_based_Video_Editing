"""LLM provider registry driven by application settings."""

from __future__ import annotations

import httpx

from incubrix.adapters.base import AdapterConfigurationError
from incubrix.adapters.llm.base import LLMAdapter
from incubrix.adapters.llm.mock import MockLLMAdapter
from incubrix.adapters.llm.openai_compatible import OpenAICompatibleLLMAdapter
from incubrix.config import Settings, get_settings


def get_llm_adapter(
    settings: Settings | None = None,
    *,
    client: httpx.Client | None = None,
) -> LLMAdapter:
    """Build mock or OpenAI-compatible parser from configured provider settings."""
    config = settings or get_settings()
    if config.LLM_PROVIDER == "mock":
        return MockLLMAdapter(config.LLM_MODEL)
    if config.LLM_PROVIDER in {"groq", "openai"}:
        key = config.LLM_API_KEY
        if not key:
            key = config.GROQ_API_KEY if config.LLM_PROVIDER == "groq" else config.OPENAI_API_KEY
        if not key:
            raise AdapterConfigurationError(
                f"LLM_API_KEY or the {config.LLM_PROVIDER.upper()}_API_KEY setting is required."
            )
        return OpenAICompatibleLLMAdapter(
            model=config.LLM_MODEL,
            base_url=config.LLM_BASE_URL,
            api_key=key,
            provider=config.LLM_PROVIDER,
            timeout_s=config.LLM_TIMEOUT_S,
            max_retries=config.LLM_MAX_RETRIES,
            client=client,
        )
    raise AdapterConfigurationError(f"Unsupported LLM provider: {config.LLM_PROVIDER}.")
