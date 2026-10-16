"""Shared LLM response object."""

from dataclasses import dataclass


@dataclass(frozen=True)
class LLMResult:
    content: str
    model: str
