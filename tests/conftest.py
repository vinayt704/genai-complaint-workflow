from __future__ import annotations

from pathlib import Path

import pytest

from complaint_processor.config import Settings
from scripts.generate_sample_data import generate


@pytest.fixture(scope="session")
def sample_data_dir(tmp_path_factory) -> Path:
    directory = tmp_path_factory.mktemp("data")
    generate(directory)
    return directory


@pytest.fixture
def settings(tmp_path, sample_data_dir) -> Settings:
    return Settings(
        llm_provider="mock",
        data_dir=sample_data_dir,
        output_dir=tmp_path / "output",
        log_dir=tmp_path / "logs",
        max_workers=3,
        llm_max_retries=3,
    )
