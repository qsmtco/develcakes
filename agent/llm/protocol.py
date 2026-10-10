"""LLM provider protocol and response dataclass."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol, runtime_checkable


@dataclass
class LLMResponse:
    """Normalized response from any LLM provider."""
    text: str = ""
    tool_calls: list[tuple[str, str, dict]] = field(default_factory=list)
    usage: tuple[int, int] = (0, 0)
    raw: dict = field(default_factory=dict)


@runtime_checkable
class LLMProvider(Protocol):
    """One class per provider wire protocol."""

    @property
    def provider_id(self) -> str: ...

    @property
    def response_format(self) -> str: ...

    def call(
        self,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict],
        tools: list[dict] | None,
        timeout: float,
        x_title: str = "",
        reasoning_effort: str = "off",
    ) -> dict: ...

    def stream(
        self,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict],
        tools: list[dict] | None,
        timeout: float,
        x_title: str = "",
        reasoning_effort: str = "off",
    ) -> Iterator: ...