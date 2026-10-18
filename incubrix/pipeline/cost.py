"""Small provider-usage cost estimator."""

from __future__ import annotations

from incubrix.adapters.base import Usage
from incubrix.config import Settings, get_settings


def estimate_llm_cost(usage: Usage, settings: Settings | None = None) -> float:
    """Estimate price from separate token prices or the legacy combined token price."""
    config = settings or get_settings()
    if (
        config.LLM_INPUT_COST_PER_1K_TOKENS == 0
        and config.LLM_OUTPUT_COST_PER_1K_TOKENS == 0
        and config.LLM_COST_PER_1K_TOKENS != 0
    ):
        return round(usage.tokens * config.LLM_COST_PER_1K_TOKENS / 1000, 8)
    return round(
        usage.input_tokens * config.LLM_INPUT_COST_PER_1K_TOKENS / 1000
        + usage.output_tokens * config.LLM_OUTPUT_COST_PER_1K_TOKENS / 1000,
        8,
    )
