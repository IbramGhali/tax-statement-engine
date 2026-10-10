"""Claude adapter (optional): `uv sync --extra anthropic` and set ANTHROPIC_API_KEY."""

from __future__ import annotations

from typing import Any

from tax_engine.llm import LLMError, LLMRequest, LLMResponse

FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5", "claude-fable-5-1"}


class AnthropicLLM:
    name = "anthropic"

    def __init__(self, timeout: float = 60.0) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - optional extra
            raise LLMError("install the anthropic extra: uv sync --extra anthropic") from exc
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(timeout=timeout, max_retries=2)

    def complete(self, request: LLMRequest) -> LLMResponse:
        anthropic = self._anthropic
        kwargs: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": request.user}],
            "output_config": {"effort": "low"},  # short, grounded note; no deep reasoning
        }
        try:
            if request.model in FALLBACK_MODELS:
                response = self._client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
                )
            else:
                response = self._client.messages.create(**kwargs)
        except anthropic.RateLimitError as exc:
            raise LLMError(f"rate limited: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("connection error") from exc
        if response.stop_reason in ("refusal", "max_tokens"):
            raise LLMError(f"unusable response: {response.stop_reason}")
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            raise LLMError("empty response")
        return LLMResponse(
            text=text,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
