"""Provider-agnostic interface used by the workflow.

The workflow depends only on ``LLMClient.generate_structured`` which returns a
validated Pydantic object. Swapping OpenAI / Azure OpenAI / Gemini / mock is a
configuration change, not a code change.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class StructuredOutputError(RuntimeError):
    """The model response could not be parsed/validated against the schema."""


@dataclass
class LLMResult(Generic[T]):
    parsed: T
    input_tokens: int = 0
    output_tokens: int = 0
    latency_seconds: float = 0.0
    attempts: int = 1


class LLMClient(ABC):
    provider: str = "unknown"
    model: str = "unknown"

    @abstractmethod
    def generate_structured(self, system_prompt: str, user_prompt: str, schema: type[T]) -> LLMResult[T]:
        """Call the model and return an instance of ``schema``."""
