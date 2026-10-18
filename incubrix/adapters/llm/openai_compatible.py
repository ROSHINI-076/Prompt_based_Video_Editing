"""OpenAI-compatible chat-completions adapter with bounded retries and repair."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from incubrix.adapters.base import (
    AdapterError,
    AdapterTransientError,
    InstructionNotUnderstoodError,
    Usage,
)
from incubrix.adapters.llm.base import ParseResult
from incubrix.adapters.llm.prompt import system_prompt, user_prompt
from incubrix.pipeline.intents import IntentOps


class OpenAICompatibleLLMAdapter:
    """Call Groq or another compatible endpoint without exposing credential errors."""

    provider = "openai_compatible"

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        provider: str = "openai_compatible",
        timeout_s: float = 30.0,
        max_retries: int = 2,
        client: httpx.Client | None = None,
        backoff_s: float = 0.05,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("LLM timeout must be greater than zero.")
        if max_retries < 0:
            raise ValueError("LLM max retries cannot be negative.")
        self.model = model
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self._client = client or httpx.Client(timeout=timeout_s)
        self._owns_client = client is None

    def close(self) -> None:
        """Close only the HTTP client created and owned by this adapter."""
        if self._owns_client:
            self._client.close()

    def parse(self, instruction: str, context: dict[str, Any]) -> ParseResult:
        """Request JSON output, retry invalid output once with a repair prompt."""
        started = time.perf_counter()
        messages = [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": user_prompt(instruction, context)},
        ]
        total_input = 0
        total_output = 0
        for repair_attempt in range(2):
            response = self._request(messages)
            usage = response.get("usage", {})
            total_input += int(usage.get("prompt_tokens", 0))
            total_output += int(usage.get("completion_tokens", 0))
            try:
                content = response["choices"][0]["message"]["content"]
                decoded = json.loads(content)
                parsed = IntentOps.model_validate(decoded)
            except (KeyError, IndexError, TypeError, ValueError):
                if repair_attempt:
                    raise InstructionNotUnderstoodError(
                        "The language model did not return valid instruction operations."
                    ) from None
                choices = response.get("choices")
                previous_content = ""
                if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                    message = choices[0].get("message")
                    if isinstance(message, dict):
                        previous_content = str(message.get("content", ""))
                messages.extend(
                    [
                        {
                            "role": "assistant",
                            "content": previous_content,
                        },
                        {
                            "role": "user",
                            "content": (
                                "Repair the previous response to match the provided JSON schema. "
                                "Output ONLY valid JSON."
                            ),
                        },
                    ]
                )
                continue
            elapsed = (time.perf_counter() - started) * 1000
            return ParseResult(
                ops=[
                    operation
                    for operation in parsed.model_dump(mode="json", exclude_none=True)["ops"]
                ],
                usage=Usage(
                    provider=self.provider,
                    model=self.model,
                    input_tokens=total_input,
                    output_tokens=total_output,
                    latency_ms=elapsed,
                ),
            )

    def _request(self, messages: list[dict[str, str]]) -> dict:
        """Retry only timeout, rate-limit and server errors."""
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.model,
            "temperature": 0,
            "seed": 0,
            "response_format": {"type": "json_object"},
            "messages": messages,
        }
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.post(
                    url, headers=headers, json=payload, timeout=self.timeout_s
                )
            except httpx.TimeoutException:
                if attempt < self.max_retries:
                    time.sleep(self.backoff_s * (attempt + 1))
                    continue
                raise AdapterTransientError("The language model request timed out.") from None
            except httpx.TransportError:
                if attempt < self.max_retries:
                    time.sleep(self.backoff_s * (attempt + 1))
                    continue
                raise AdapterTransientError("The language model service is unavailable.") from None
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self.max_retries:
                    time.sleep(self.backoff_s * (attempt + 1))
                    continue
                raise AdapterTransientError(
                    f"The language model service returned HTTP {response.status_code}."
                )
            if response.is_error:
                raise AdapterError(
                    f"The language model rejected the request (HTTP {response.status_code})."
                )
            try:
                result = response.json()
            except ValueError:
                raise InstructionNotUnderstoodError(
                    "The language model returned an invalid response envelope."
                ) from None
            if not isinstance(result, dict):
                raise InstructionNotUnderstoodError(
                    "The language model returned an invalid response envelope."
                )
            return result


GroqLLMAdapter = OpenAICompatibleLLMAdapter

__all__ = ["GroqLLMAdapter", "OpenAICompatibleLLMAdapter"]
