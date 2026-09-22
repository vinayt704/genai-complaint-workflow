"""Application configuration loaded from environment variables / .env.

All tunable values live here so that nothing is hard-coded in the workflow.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields, replace
from pathlib import Path

from dotenv import load_dotenv

SUPPORTED_PROVIDERS = ("openai", "azure_openai", "gemini", "mock")

DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.5-flash",
    "mock": "mock-heuristic-v1",
    "azure_openai": "",  # Azure uses the deployment name
}


class ConfigError(ValueError):
    """Raised when the configuration is invalid or incomplete."""


def _env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got '{raw}'") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got '{raw}'") from exc


def _env_optional_float(name: str, default: float | None) -> float | None:
    """Float that can be disabled with 'none' (for models that reject temperature)."""
    raw = _env_str(name)
    if not raw:
        return default
    if raw.lower() in {"none", "null", "off"}:
        return None
    return _env_float(name, default or 0.0)


@dataclass(frozen=True)
class Settings:
    # LLM
    llm_provider: str = "openai"
    model_name: str = ""
    extraction_temperature: float | None = 0.0
    generation_temperature: float | None = 0.3
    llm_max_retries: int = 3
    llm_timeout_seconds: int = 60

    # Provider credentials
    openai_api_key: str = ""
    openai_base_url: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_deployment: str = ""
    azure_openai_api_version: str = "2024-10-21"
    google_api_key: str = ""

    # Workflow
    data_dir: Path = Path("data")
    output_dir: Path = Path("output")
    log_dir: Path = Path("logs")
    log_level: str = "INFO"
    max_workers: int = 3
    max_file_size_mb: float = 10.0
    max_document_chars: int = 20000
    grounding_max_corrections: int = 1

    # Branding
    company_name: str = "Contoso Customer Care"
    email_signature: str = "Customer Care Team, Contoso"

    # ------------------------------------------------------------------ #
    @classmethod
    def from_env(cls, env_file: str | Path | None = ".env", **overrides) -> "Settings":
        """Build settings from environment variables (and an optional .env file).

        Keyword overrides (e.g. from CLI flags) win over environment values;
        overrides with value ``None`` are ignored.
        """
        if env_file and Path(env_file).exists():
            load_dotenv(env_file, override=False)

        settings = cls(
            llm_provider=_env_str("LLM_PROVIDER", "openai").lower(),
            model_name=_env_str("MODEL_NAME"),
            extraction_temperature=_env_optional_float("EXTRACTION_TEMPERATURE", 0.0),
            generation_temperature=_env_optional_float("GENERATION_TEMPERATURE", 0.3),
            llm_max_retries=_env_int("LLM_MAX_RETRIES", 3),
            llm_timeout_seconds=_env_int("LLM_TIMEOUT_SECONDS", 60),
            openai_api_key=_env_str("OPENAI_API_KEY"),
            openai_base_url=_env_str("OPENAI_BASE_URL"),
            azure_openai_endpoint=_env_str("AZURE_OPENAI_ENDPOINT"),
            azure_openai_api_key=_env_str("AZURE_OPENAI_API_KEY"),
            azure_openai_deployment=_env_str("AZURE_OPENAI_DEPLOYMENT"),
            azure_openai_api_version=_env_str("AZURE_OPENAI_API_VERSION", "2024-10-21"),
            google_api_key=_env_str("GOOGLE_API_KEY") or _env_str("GEMINI_API_KEY"),
            data_dir=Path(_env_str("DATA_DIR", "data")),
            output_dir=Path(_env_str("OUTPUT_DIR", "output")),
            log_dir=Path(_env_str("LOG_DIR", "logs")),
            log_level=_env_str("LOG_LEVEL", "INFO").upper(),
            max_workers=_env_int("MAX_WORKERS", 3),
            max_file_size_mb=_env_float("MAX_FILE_SIZE_MB", 10.0),
            max_document_chars=_env_int("MAX_DOCUMENT_CHARS", 20000),
            grounding_max_corrections=_env_int("GROUNDING_MAX_CORRECTIONS", 1),
            company_name=_env_str("COMPANY_NAME", "Contoso Customer Care"),
            email_signature=_env_str("EMAIL_SIGNATURE", "Customer Care Team, Contoso"),
        )
        valid_names = {f.name for f in fields(cls)}
        clean = {k: v for k, v in overrides.items() if v is not None and k in valid_names}
        for path_field in ("data_dir", "output_dir", "log_dir"):
            if path_field in clean:
                clean[path_field] = Path(clean[path_field])
        if "llm_provider" in clean:
            clean["llm_provider"] = clean["llm_provider"].lower()
        return replace(settings, **clean)

    # ------------------------------------------------------------------ #
    @property
    def resolved_model(self) -> str:
        if self.llm_provider == "azure_openai":
            return self.azure_openai_deployment
        return self.model_name or DEFAULT_MODELS.get(self.llm_provider, "")

    def validate(self) -> None:
        """Fail fast with a clear message before any document is processed."""
        if self.llm_provider not in SUPPORTED_PROVIDERS:
            raise ConfigError(
                f"Unsupported LLM_PROVIDER '{self.llm_provider}'. "
                f"Choose one of: {', '.join(SUPPORTED_PROVIDERS)}"
            )
        required: dict[str, str] = {}
        if self.llm_provider == "openai":
            required = {"OPENAI_API_KEY": self.openai_api_key}
        elif self.llm_provider == "azure_openai":
            required = {
                "AZURE_OPENAI_ENDPOINT": self.azure_openai_endpoint,
                "AZURE_OPENAI_API_KEY": self.azure_openai_api_key,
                "AZURE_OPENAI_DEPLOYMENT": self.azure_openai_deployment,
            }
        elif self.llm_provider == "gemini":
            required = {"GOOGLE_API_KEY": self.google_api_key}
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ConfigError(
                f"Provider '{self.llm_provider}' requires: {', '.join(missing)}. "
                "Set them in your .env file (see .env.example)."
            )
        if self.max_workers < 1:
            raise ConfigError("MAX_WORKERS must be >= 1")
        if self.llm_max_retries < 1:
            raise ConfigError("LLM_MAX_RETRIES must be >= 1")
        if self.max_document_chars < 500:
            raise ConfigError("MAX_DOCUMENT_CHARS must be >= 500")
        if self.grounding_max_corrections < 0:
            raise ConfigError("GROUNDING_MAX_CORRECTIONS must be >= 0")

    def describe(self) -> str:
        """Non-secret summary for logs."""
        return (
            f"provider={self.llm_provider} model={self.resolved_model} "
            f"workers={self.max_workers} retries={self.llm_max_retries} "
            f"data_dir={self.data_dir} output_dir={self.output_dir}"
        )
