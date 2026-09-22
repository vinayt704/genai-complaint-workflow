from pathlib import Path

import pytest

from complaint_processor.ingestion import DocumentLoader, DocumentLoadError


@pytest.fixture
def loader():
    return DocumentLoader(max_file_size_mb=1, max_chars=5000)


def test_discover_splits_supported_and_unsupported(loader, sample_data_dir):
    supported, unsupported = loader.discover(sample_data_dir)
    assert {p.suffix for p in supported} == {".pdf", ".docx", ".txt"}
    assert [p.name for p in unsupported] == ["customer_list.csv"]


@pytest.mark.parametrize("name, expected", [
    ("complaint_001.pdf", "Priya Raman"),
    ("complaint_002.docx", "Daniel Okafor"),
    ("complaint_003.txt", "Meera Joshi"),
])
def test_loads_all_three_formats(loader, sample_data_dir, name, expected):
    document = loader.load(sample_data_dir / name)
    assert expected in document.text
    assert document.file_type == name.rsplit(".", 1)[1]


def test_docx_tables_are_extracted_in_order(loader, sample_data_dir):
    text = loader.load(sample_data_dir / "complaint_005.docx").text
    assert text.index("Aisha Mohammed") < text.index("Details of complaint")


def test_corrupted_pdf_raises_friendly_error(loader, sample_data_dir):
    with pytest.raises(DocumentLoadError, match="Could not parse .pdf"):
        loader.load(sample_data_dir / "complaint_009_corrupted.pdf")


def test_empty_file_raises(loader, sample_data_dir):
    with pytest.raises(DocumentLoadError, match="empty"):
        loader.load(sample_data_dir / "complaint_010_empty.txt")


def test_unsupported_extension_raises(loader, sample_data_dir):
    with pytest.raises(DocumentLoadError, match="Unsupported"):
        loader.load(sample_data_dir / "customer_list.csv")


def test_long_document_is_truncated(tmp_path):
    path = tmp_path / "long.txt"
    path.write_text("word " * 2000, encoding="utf-8")
    document = DocumentLoader(max_chars=1000).load(path)
    assert document.truncated and document.char_count == 1000


def test_cp1252_text_is_decoded(tmp_path):
    path = tmp_path / "legacy.txt"
    path.write_bytes("Customer complaint about the caf\xe9 \x93service\x94 quality.".encode("latin-1"))
    assert "caf" in DocumentLoader().load(path).text


def test_missing_folder_raises(loader):
    with pytest.raises(FileNotFoundError):
        loader.discover(Path("does/not/exist"))


def test_config_validation_reports_missing_keys():
    from complaint_processor.config import ConfigError, Settings
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        Settings(llm_provider="openai").validate()
    with pytest.raises(ConfigError, match="Unsupported"):
        Settings(llm_provider="foo").validate()
