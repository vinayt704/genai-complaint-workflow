"""LangChain-backed client: structured output + retries with exponential backoff."""
from __future__ import annotations

import logging
import time

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .base import LLMClient, LLMResult, StructuredOutputError, T

logger = logging.getLogger(__name__)

# Errors where retrying cannot help (bad key, missing deployment, invalid request).
_NON_RETRYABLE = {
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "BadRequestError",
    "UnprocessableEntityError",
    "Unauthenticated",
    "PermissionDenied",
    "InvalidArgument",
    "ConfigError",
}


def _is_retryable(exc: BaseException) -> bool:
    names = {cls.__name__ for cls in type(exc).__mro__}
    return not (names & _NON_RETRYABLE)


class LangChainLLMClient(LLMClient):
    def __init__(self, chat_model: BaseChatModel, provider: str, model: str, max_retries: int = 3):
        self._chat_model = chat_model
        self.provider = provider
        self.model = model
        self._max_retries = max(1, max_retries)

    def generate_structured(self, system_prompt: str, user_prompt: str, schema: type[T]) -> LLMResult[T]:
        # include_raw=True -> we get token usage and explicit parsing errors
        runnable = self._chat_model.with_structured_output(schema, include_raw=True)
        messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]

        retrying = Retrying(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=20),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
            before_sleep=lambda rs: logger.warning(
                "%s call failed (attempt %d/%d): %s - retrying",
                schema.__name__, rs.attempt_number, self._max_retries,
                rs.outcome.exception() if rs.outcome else "unknown",
            ),
        )
        for attempt in retrying:
            with attempt:
                started = time.perf_counter()
                output = runnable.invoke(messages)
                latency = time.perf_counter() - started

                parsed = output.get("parsed")
                parsing_error = output.get("parsing_error")
                if parsing_error is not None or parsed is None:
                    raise StructuredOutputError(
                        f"Model output did not match {schema.__name__}: {parsing_error or 'empty response'}"
                    )
                if not isinstance(parsed, schema):
                    parsed = schema.model_validate(parsed)

                usage = getattr(output.get("raw"), "usage_metadata", None) or {}
                return LLMResult(
                    parsed=parsed,
                    input_tokens=int(usage.get("input_tokens", 0) or 0),
                    output_tokens=int(usage.get("output_tokens", 0) or 0),
                    latency_seconds=latency,
                    attempts=attempt.retry_state.attempt_number,
                )
        raise StructuredOutputError("unreachable")  # pragma: no cover
