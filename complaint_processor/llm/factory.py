"""Builds LLM clients for the configured provider."""
from __future__ import annotations

import logging

from ..config import ConfigError, Settings
from .base import LLMClient

logger = logging.getLogger(__name__)


def _build_client(settings: Settings, temperature: float | None) -> LLMClient:
    provider = settings.llm_provider

    if provider == "mock":
        from .mock_client import MockLLMClient
        return MockLLMClient()

    from .langchain_client import LangChainLLMClient

    common: dict = {"timeout": settings.llm_timeout_seconds, "max_retries": 2}
    if temperature is not None:
        common["temperature"] = temperature

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        kwargs = {"model": settings.resolved_model, "api_key": settings.openai_api_key, **common}
        if settings.openai_base_url:
            kwargs["base_url"] = settings.openai_base_url
        chat_model = ChatOpenAI(**kwargs)

    elif provider == "azure_openai":
        from langchain_openai import AzureChatOpenAI

        chat_model = AzureChatOpenAI(
            azure_endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key,
            azure_deployment=settings.azure_openai_deployment,
            api_version=settings.azure_openai_api_version,
            **common,
        )

    elif provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        chat_model = ChatGoogleGenerativeAI(
            model=settings.resolved_model,
            google_api_key=settings.google_api_key,
            **common,
        )
    else:
        raise ConfigError(f"Unsupported provider: {provider}")

    return LangChainLLMClient(
        chat_model=chat_model,
        provider=provider,
        model=settings.resolved_model,
        max_retries=settings.llm_max_retries,
    )


def create_llm_clients(settings: Settings) -> tuple[LLMClient, LLMClient]:
    """Return (extraction_client, generation_client).

    Extraction uses a low temperature for determinism; generation (email and
    summary) uses a slightly higher temperature for more natural writing.
    """
    extraction = _build_client(settings, settings.extraction_temperature)
    generation = _build_client(settings, settings.generation_temperature)
    logger.info(
        "LLM clients ready: provider=%s model=%s (extraction T=%s, generation T=%s)",
        extraction.provider, extraction.model,
        settings.extraction_temperature, settings.generation_temperature,
    )
    return extraction, generation
