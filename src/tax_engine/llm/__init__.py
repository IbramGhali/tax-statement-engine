"""LLM adapters for the commentary step. `fake` is the default and needs no network."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class LLMRequest:
    model: str
    system: str
    user: str
    payload: dict[str, Any] = field(default_factory=dict)
    max_tokens: int = 1024


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    input_tokens: int
    output_tokens: int


class LLMError(RuntimeError):
    """Any provider failure. The commentary step fails safe."""


class LLMClient(Protocol):
    name: str

    def complete(self, request: LLMRequest) -> LLMResponse: ...


def make_client(provider: str, drift: float = 0.05) -> LLMClient:
    if provider == "fake":
        from tax_engine.llm.fake import FakeLLM

        return FakeLLM(drift=drift)
    if provider == "anthropic":
        from tax_engine.llm.anthropic_adapter import AnthropicLLM

        return AnthropicLLM()
    raise ValueError(f"unknown TSE_LLM_PROVIDER '{provider}' (fake|anthropic)")
