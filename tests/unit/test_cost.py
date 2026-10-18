"""Tests for cost estimation: mock/local providers report zero cost."""

from __future__ import annotations

import pytest

from incubrix.adapters.base import Usage
from incubrix.config import Settings
from incubrix.pipeline.cost import estimate_llm_cost, estimate_stt_cost


def _mock_settings(**overrides) -> Settings:
    defaults = {
        "DATABASE_URL": "sqlite:///:memory:",
        "DATABASE_URL_ASYNC": "sqlite+aiosqlite:///:memory:",
        "STT_COST_PER_MINUTE": 0.0,
        "LLM_COST_PER_1K_TOKENS": 0.0,
        "LLM_INPUT_COST_PER_1K_TOKENS": 0.0,
        "LLM_OUTPUT_COST_PER_1K_TOKENS": 0.0,
    }
    defaults.update(overrides)
    return Settings(**defaults)


class TestMockLocalZeroCost:
    """Mock and local providers always report zero API cost."""

    def test_stt_mock_zero(self):
        assert estimate_stt_cost(10.0, "mock") == 0.0

    def test_stt_faster_whisper_zero(self):
        assert estimate_stt_cost(60.0, "faster_whisper") == 0.0

    def test_stt_mock_zero_even_with_nonzero_env(self):
        settings = _mock_settings(STT_COST_PER_MINUTE=0.006)
        assert estimate_stt_cost(10.0, "mock", settings) == 0.0

    def test_llm_mock_zero(self):
        usage = Usage(provider="mock", model="mock", input_tokens=500, output_tokens=200)
        settings = _mock_settings()
        assert estimate_llm_cost(usage, settings) == 0.0


class TestHostedProviderCost:
    """Hosted providers use prices from env."""

    def test_stt_hosted_uses_env_price(self):
        settings = _mock_settings(STT_COST_PER_MINUTE=0.006)
        cost = estimate_stt_cost(10.0, "deepgram", settings)
        assert cost == pytest.approx(0.06)

    def test_llm_hosted_input_output_cost(self):
        settings = _mock_settings(
            LLM_INPUT_COST_PER_1K_TOKENS=0.01,
            LLM_OUTPUT_COST_PER_1K_TOKENS=0.03,
        )
        usage = Usage(provider="groq", model="llama", input_tokens=1000, output_tokens=500)
        cost = estimate_llm_cost(usage, settings)
        # 1000 * 0.01/1000 + 500 * 0.03/1000 = 0.01 + 0.015 = 0.025
        assert cost == pytest.approx(0.025)

    def test_llm_legacy_combined_cost(self):
        settings = _mock_settings(LLM_COST_PER_1K_TOKENS=0.005)
        usage = Usage(provider="openai", model="gpt-4", input_tokens=800, output_tokens=200)
        cost = estimate_llm_cost(usage, settings)
        # 1000 * 0.005/1000 = 0.005
        assert cost == pytest.approx(0.005)
