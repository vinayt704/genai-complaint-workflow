"""Document ingestion: discover files in the data folder and extract their text.

Supported formats: .txt, .pdf, .docx
Every file-level problem (empty, corrupt, encrypted, too large, image-only PDF)
is converted into a ``DocumentLoadError`` with a human-readable message so the
batch can continue with the remaining files.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

MIN_TEXT_CHARS = 20  # below this we consider the document unreadable


class DocumentLoadError(Exception):
    """A single document could not be read. The batch continues."""


@dataclass(frozen=True)
class SourceDocument:
    doc_id: str
    file_name: str
    file_path: Path
    file_type: str
    text: str
    page_count: int | None = None
    truncated: bool = False

    @property
    def char_count(self) -> int:
        return len(self.text)


def _normalize_text(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class DocumentLoader:
    """Reads supported business documents from disk."""

    SUPPORTED_EXTENSIONS = (".txt", ".pdf", ".docx")
    _IGNORED_PREFIXES = (".", "~$")  # hidden files, Office lock files

    def __init__(self, max_file_size_mb: float = 10.0, max_chars: int = 20000):
        self.max_file_size_bytes = int(max_file_size_mb * 1024 * 1024)
        self.max_chars = max_chars
        self._readers: dict[str, Callable[[Path], tuple[str, int | None]]] = {
            ".txt": self._read_txt,
            ".pdf": self._read_pdf,
            ".docx": self._read_docx,
        }

    # ------------------------------------------------------------------ #
    def discover(self, data_dir: Path) -> tuple[list[Path], list[Path]]:
        """Return (supported_files, unsupported_files) sorted by name."""
        data_dir = Path(data_dir)
        if not data_dir.exists():
            raise FileNotFoundError(f"Data folder not found: {data_dir.resolve()}")
        if not data_dir.is_dir():
            raise NotADirectoryError(f"Data path is not a folder: {data_dir.resolve()}")

        files = sorted(
            p for p in data_dir.iterdir()
            if p.is_file() and not p.name.startswith(self._IGNORED_PREFIXES)
        )
        supported = [p for p in files if p.suffix.lower() in self.SUPPORTED_EXTENSIONS]
        unsupported = [p for p in files if p.suffix.lower() not in self.SUPPORTED_EXTENSIONS]
        logger.info(
            "Discovered %d file(s) in %s: %d supported, %d unsupported",
            len(files), data_dir, len(supported), len(unsupported),
        )
        return supported, unsupported

    def load(self, path: Path, doc_id: str | None = None) -> SourceDocument:
        """Read one document and return its cleaned text."""
        path = Path(path)
        ext = path.suffix.lower()
        if ext not in self._readers:
            raise DocumentLoadError(
                f"Unsupported file type '{ext}' (supported: {', '.join(self.SUPPORTED_EXTENSIONS)})"
            )
        if not path.exists():
            raise DocumentLoadError(f"File not found: {path}")

        size = path.stat().st_size
        if size == 0:
            raise DocumentLoadError("File is empty (0 bytes)")
        if size > self.max_file_size_bytes:
            raise DocumentLoadError(
                f"File is {size / 1_048_576:.1f} MB, above the "
                f"{self.max_file_size_bytes / 1_048_576:.0f} MB limit"
            )

        try:
            raw_text, page_count = self._readers[ext](path)
        except DocumentLoadError:
            raise
        except Exception as exc:  # corrupt / malformed files
            raise DocumentLoadError(
                f"Could not parse {ext} file ({type(exc).__name__}: {exc})"
            ) from exc

        text = _normalize_text(raw_text)
        if len(text) < MIN_TEXT_CHARS:
            hint = " (it may be a scanned, image-only PDF)" if ext == ".pdf" else ""
            raise DocumentLoadError(f"No extractable text found{hint}")

        truncated = False
        if len(text) > self.max_chars:
            logger.warning("%s: text truncated from %d to %d chars", path.name, len(text), self.max_chars)
            text = text[: self.max_chars]
            truncated = True

        logger.debug("%s: extracted %d chars (pages=%s)", path.name, len(text), page_count)
        return SourceDocument(
            doc_id=doc_id or path.stem,
            file_name=path.name,
            file_path=path,
            file_type=ext.lstrip("."),
            text=text,
            page_count=page_count,
            truncated=truncated,
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _read_txt(path: Path) -> tuple[str, None]:
        for encoding in ("utf-8-sig", "utf-8", "cp1252"):
            try:
                return path.read_text(encoding=encoding), None
            except UnicodeDecodeError:
                continue
        logger.warning("%s: unknown encoding, falling back to latin-1", path.name)
        return path.read_text(encoding="latin-1"), None

    @staticmethod
    def _read_pdf(path: Path) -> tuple[str, int]:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                if not reader.decrypt(""):
                    raise DocumentLoadError("PDF is password-protected")
            except DocumentLoadError:
                raise
            except Exception as exc:
                raise DocumentLoadError("PDF is password-protected") from exc
        pages = [(page.extract_text() or "") for page in reader.pages]
        return "\n\n".join(pages), len(pages)

    @staticmethod
    def _read_docx(path: Path) -> tuple[str, None]:
        import docx  # python-docx
        from docx.table import Table as DocxTable

        document = docx.Document(str(path))
        parts: list[str] = []
        # iter_inner_content keeps paragraphs and tables in document order
        # (forms are often laid out as tables).
        for block in document.iter_inner_content():
            if isinstance(block, DocxTable):
                for row in block.rows:
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        parts.append(" | ".join(dict.fromkeys(cells)))  # dedupe merged cells
            elif block.text.strip():
                parts.append(block.text)
        return "\n".join(parts), None
