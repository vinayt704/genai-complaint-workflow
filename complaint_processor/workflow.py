"""Workflow orchestration.

Per document (process_file):

    Stage 0  Ingestion            (file -> text)
    Stage 1  Structured extraction (sequential: stages 2a/2b depend on it)
    Stage 2  Fan-out in parallel:
               2a  Customer email
               2b  Internal case summary
    Stage 3  Persist outputs

Across documents (run): a thread pool processes up to MAX_WORKERS documents
concurrently. LLM calls are network-bound, so threads give real speed-up.

Failure isolation: a failure in one document never stops the batch, and a
failure in 2a does not discard 2b (status PARTIAL).
"""
from __future__ import annotations

import logging
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .ingestion import DocumentLoader, DocumentLoadError
from .llm.base import LLMClient
from .prompts import PROMPT_VERSION
from .schemas import DocumentResult, ProcessingStatus
from .tasks import TaskOutput, run_email_generation, run_extraction, run_summary_generation
from .writers import OutputWriter, RunMetadata

logger = logging.getLogger(__name__)


@dataclass
class BatchResult:
    results: list[DocumentResult]
    report_path: Path
    run_summary_path: Path
    elapsed_seconds: float

    def count(self, status: ProcessingStatus) -> int:
        return sum(1 for r in self.results if r.status == status)


def assign_doc_ids(paths: list[Path]) -> dict[Path, str]:
    """Use the file stem as id; disambiguate duplicates (a.pdf + a.txt -> a_pdf, a_txt)."""
    stems = Counter(p.stem for p in paths)
    return {p: (f"{p.stem}_{p.suffix.lstrip('.').lower()}" if stems[p.stem] > 1 else p.stem) for p in paths}


class ComplaintProcessingWorkflow:
    def __init__(
        self,
        settings: Settings,
        extraction_llm: LLMClient,
        generation_llm: LLMClient,
        loader: DocumentLoader | None = None,
        writer: OutputWriter | None = None,
    ):
        self.settings = settings
        self.extraction_llm = extraction_llm
        self.generation_llm = generation_llm
        self.loader = loader or DocumentLoader(settings.max_file_size_mb, settings.max_document_chars)
        self.writer = writer or OutputWriter(settings.output_dir)
        self.meta = RunMetadata(
            run_id=uuid.uuid4().hex[:12],
            provider=extraction_llm.provider,
            model=extraction_llm.model,
            prompt_version=PROMPT_VERSION,
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    # ------------------------------------------------------------------ #
    #  Batch level
    # ------------------------------------------------------------------ #
    def run(self, clean_output: bool = False) -> BatchResult:
        started = time.perf_counter()
        logger.info("Run %s started (%s)", self.meta.run_id, self.settings.describe())
        self.writer.prepare(clean=clean_output)

        supported, unsupported = self.loader.discover(self.settings.data_dir)
        results: list[DocumentResult] = [self._skipped(p) for p in unsupported]
        if not supported:
            logger.warning("No supported documents (%s) found in %s",
                           ", ".join(DocumentLoader.SUPPORTED_EXTENSIONS), self.settings.data_dir)

        doc_ids = assign_doc_ids(supported)
        total = len(supported)
        with ThreadPoolExecutor(max_workers=self.settings.max_workers, thread_name_prefix="doc") as pool:
            futures = {pool.submit(self.process_file, path, doc_ids[path]): path for path in supported}
            for index, future in enumerate(as_completed(futures), start=1):
                path = futures[future]
                try:
                    result = future.result()
                except Exception as exc:  # defensive: process_file already catches
                    logger.exception("Unexpected failure for %s", path.name)
                    result = DocumentResult(doc_ids[path], path.name, path.suffix.lstrip("."),
                                            errors=[f"Unexpected error: {exc}"])
                results.append(result)
                logger.info("[%d/%d] %-28s -> %-7s (%.1fs, %d warning(s))",
                            index, total, path.name, result.status.value,
                            result.processing_seconds, len(result.warnings))

        results.sort(key=lambda r: r.file_name.lower())
        elapsed = time.perf_counter() - started
        report_path = self.writer.write_final_report(results)
        summary_path = self.writer.write_run_summary(results, self.meta, elapsed)
        logger.info("Run %s finished in %.1fs", self.meta.run_id, elapsed)
        return BatchResult(results, report_path, summary_path, elapsed)

    # ------------------------------------------------------------------ #
    #  Document level
    # ------------------------------------------------------------------ #
    def process_file(self, path: Path, doc_id: str | None = None) -> DocumentResult:
        path = Path(path)
        result = DocumentResult(doc_id or path.stem, path.name, path.suffix.lstrip(".").lower())
        started = time.perf_counter()
        try:
            self._process(path, result)
        except Exception as exc:  # never let one document break the batch
            logger.exception("%s: unexpected error", path.name)
            result.errors.append(f"Unexpected error: {type(exc).__name__}: {exc}")
            result.status = ProcessingStatus.FAILED
        finally:
            result.processing_seconds = time.perf_counter() - started
        return result

    def _process(self, path: Path, result: DocumentResult) -> None:
        # ---- Stage 0: ingestion ------------------------------------------
        t0 = time.perf_counter()
        try:
            document = self.loader.load(path, doc_id=result.doc_id)
        except DocumentLoadError as exc:
            logger.error("%s: ingestion failed - %s", path.name, exc)
            result.errors.append(f"Ingestion failed: {exc}")
            result.status = ProcessingStatus.FAILED
            return
        result.stage_seconds["ingestion"] = time.perf_counter() - t0
        if document.truncated:
            result.warnings.append(f"Document truncated to {self.settings.max_document_chars} characters")

        # ---- Stage 1: structured extraction (must succeed) ---------------
        t1 = time.perf_counter()
        try:
            extraction_out = run_extraction(self.extraction_llm, document)
        except Exception as exc:
            logger.error("%s: extraction failed - %s: %s", path.name, type(exc).__name__, exc)
            result.errors.append(f"Extraction failed: {type(exc).__name__}: {exc}")
            result.status = ProcessingStatus.FAILED
            return
        result.stage_seconds["extraction"] = time.perf_counter() - t1
        self._absorb(result, extraction_out)
        result.extraction = extraction_out.result

        # ---- Stage 2: email + summary in parallel -----------------------
        tasks = {
            "customer_email": lambda: run_email_generation(
                self.generation_llm, document, result.extraction, self.settings),
            "case_summary": lambda: run_summary_generation(
                self.generation_llm, document, result.extraction, self.settings),
        }
        with ThreadPoolExecutor(max_workers=len(tasks), thread_name_prefix=f"{result.doc_id[:8]}-gen") as pool:
            futures = {name: pool.submit(self._timed, fn) for name, fn in tasks.items()}
            for name, future in futures.items():
                try:
                    output, seconds = future.result()
                except Exception as exc:
                    logger.error("%s: %s failed - %s: %s", path.name, name, type(exc).__name__, exc)
                    result.errors.append(f"{name} failed: {type(exc).__name__}: {exc}")
                    continue
                result.stage_seconds[name] = seconds
                self._absorb(result, output)
                if name == "customer_email":
                    result.email = output.result
                else:
                    result.summary = output.result

        result.status = (
            ProcessingStatus.SUCCESS if result.email and result.summary else ProcessingStatus.PARTIAL
        )

        # ---- Stage 3: persist ------------------------------------------
        try:
            result.output_files = self.writer.write_document_outputs(result, self.meta)
        except OSError as exc:
            logger.error("%s: could not write outputs - %s", path.name, exc)
            result.errors.append(f"Output write failed: {exc}")
            result.status = ProcessingStatus.PARTIAL

    # ------------------------------------------------------------------ #
    @staticmethod
    def _timed(fn):
        start = time.perf_counter()
        output = fn()
        return output, time.perf_counter() - start

    @staticmethod
    def _absorb(result: DocumentResult, output: TaskOutput) -> None:
        result.warnings.extend(output.warnings)
        result.input_tokens += output.input_tokens
        result.output_tokens += output.output_tokens
        result.llm_calls += output.llm_calls

    @staticmethod
    def _skipped(path: Path) -> DocumentResult:
        logger.warning("Skipping unsupported file: %s", path.name)
        return DocumentResult(
            doc_id=path.stem,
            file_name=path.name,
            file_type=path.suffix.lstrip(".").lower() or "unknown",
            status=ProcessingStatus.SKIPPED,
            errors=[f"Unsupported file type (supported: {', '.join(DocumentLoader.SUPPORTED_EXTENSIONS)})"],
        )
