"""Writes per-document outputs and the consolidated report.

output/
├── structured_data/<doc_id>.json   validated extraction + metadata
├── customer_emails/<doc_id>.txt    customer response email
├── case_summaries/<doc_id>.md      internal management summary
├── final_report.csv                one row per input file (incl. failures)
└── run_summary.json                batch statistics
"""
from __future__ import annotations

import csv
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .schemas import DocumentResult, ProcessingStatus

logger = logging.getLogger(__name__)

STRUCTURED_DIR = "structured_data"
EMAILS_DIR = "customer_emails"
SUMMARIES_DIR = "case_summaries"
REPORT_FILE = "final_report.csv"
RUN_SUMMARY_FILE = "run_summary.json"

REPORT_COLUMNS = [
    "doc_id", "file_name", "file_type", "processing_status",
    "customer_name", "email", "phone_number", "reference_number", "product_or_service",
    "complaint_category", "is_complaint", "escalation_required", "supporting_document_available",
    "overall_case_status", "priority", "issue_description", "resolution_provided",
    "email_subject", "recommended_next_action",
    "structured_data_file", "customer_email_file", "case_summary_file",
    "warnings_count", "warnings", "errors",
    "llm_calls", "input_tokens", "output_tokens", "processing_seconds",
]


@dataclass(frozen=True)
class RunMetadata:
    run_id: str
    provider: str
    model: str
    prompt_version: str
    started_at: str


def _atomic_write(path: Path, content: str) -> None:
    """Write via a temp file so a crash never leaves a half-written output."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class OutputWriter:
    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir)
        self.structured_dir = self.output_dir / STRUCTURED_DIR
        self.emails_dir = self.output_dir / EMAILS_DIR
        self.summaries_dir = self.output_dir / SUMMARIES_DIR

    def prepare(self, clean: bool = False) -> None:
        if clean:
            for directory in (self.structured_dir, self.emails_dir, self.summaries_dir):
                shutil.rmtree(directory, ignore_errors=True)
            for name in (REPORT_FILE, RUN_SUMMARY_FILE):
                (self.output_dir / name).unlink(missing_ok=True)
            logger.info("Cleaned previous outputs in %s", self.output_dir)
        for directory in (self.structured_dir, self.emails_dir, self.summaries_dir):
            directory.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    def write_document_outputs(self, result: DocumentResult, meta: RunMetadata) -> dict[str, str]:
        files: dict[str, str] = {}
        processed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        if result.extraction is not None:
            payload = {
                "doc_id": result.doc_id,
                "source_file": result.file_name,
                "processed_at": processed_at,
                "run_id": meta.run_id,
                "llm_provider": meta.provider,
                "llm_model": meta.model,
                "prompt_version": meta.prompt_version,
                "extraction": result.extraction.model_dump(),
                "validation_warnings": [w for w in result.warnings if "grounding" not in w],
            }
            path = self.structured_dir / f"{result.doc_id}.json"
            _atomic_write(path, json.dumps(payload, indent=2, ensure_ascii=False))
            files["structured_data_file"] = self._rel(path)

        if result.email is not None:
            path = self.emails_dir / f"{result.doc_id}.txt"
            _atomic_write(path, f"Subject: {result.email.subject}\n\n{result.email.body}\n")
            files["customer_email_file"] = self._rel(path)

        if result.summary is not None and result.extraction is not None:
            path = self.summaries_dir / f"{result.doc_id}.md"
            _atomic_write(path, self._render_summary(result, processed_at))
            files["case_summary_file"] = self._rel(path)

        return files

    @staticmethod
    def _render_summary(result: DocumentResult, processed_at: str) -> str:
        e, s = result.extraction, result.summary
        assert e is not None and s is not None
        return (
            f"# Internal Case Summary - {result.doc_id}\n\n"
            f"| Field | Value |\n|---|---|\n"
            f"| Source file | {result.file_name} |\n"
            f"| Customer | {e.customer_name} |\n"
            f"| Reference | {e.reference_number} |\n"
            f"| Category | {e.complaint_category} |\n"
            f"| Priority | {e.priority} |\n"
            f"| Status | {e.overall_case_status} |\n"
            f"| Escalation required | {e.escalation_required} |\n"
            f"| Processed (UTC) | {processed_at} |\n\n"
            f"## Case overview\n{s.case_overview}\n\n"
            f"## Key issue\n{s.key_issue}\n\n"
            f"## Action taken\n{s.action_taken}\n\n"
            f"## Current status\n{s.current_status}\n\n"
            f"## Recommended next action *(AI recommendation)*\n{s.recommended_next_action}\n"
        )

    # ------------------------------------------------------------------ #
    def write_final_report(self, results: list[DocumentResult]) -> Path:
        path = self.output_dir / REPORT_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [self._report_row(r) for r in results]

        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".report.", suffix=".tmp")
        try:
            # utf-8-sig so Excel opens it with the right encoding
            with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=REPORT_COLUMNS)
                writer.writeheader()
                writer.writerows(rows)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        logger.info("Final report written: %s (%d rows)", path, len(rows))
        return path

    def write_run_summary(self, results: list[DocumentResult], meta: RunMetadata, elapsed: float) -> Path:
        counts = {status.value: 0 for status in ProcessingStatus}
        for r in results:
            counts[r.status.value] += 1
        extracted = [r.extraction for r in results if r.extraction]
        summary = {
            **meta.__dict__,
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "elapsed_seconds": round(elapsed, 2),
            "files_total": len(results),
            "status_counts": counts,
            "escalations_required": sum(1 for e in extracted if e.escalation_required == "Yes"),
            "complaints": sum(1 for e in extracted if e.is_complaint == "Yes"),
            "by_category": _count_by(extracted, "complaint_category"),
            "by_case_status": _count_by(extracted, "overall_case_status"),
            "by_priority": _count_by(extracted, "priority"),
            "documents_with_warnings": sum(1 for r in results if r.warnings),
            "llm_calls": sum(r.llm_calls for r in results),
            "input_tokens": sum(r.input_tokens for r in results),
            "output_tokens": sum(r.output_tokens for r in results),
        }
        path = self.output_dir / RUN_SUMMARY_FILE
        _atomic_write(path, json.dumps(summary, indent=2))
        return path

    # ------------------------------------------------------------------ #
    def _report_row(self, r: DocumentResult) -> dict[str, object]:
        row: dict[str, object] = {col: "" for col in REPORT_COLUMNS}
        row.update(
            doc_id=r.doc_id,
            file_name=r.file_name,
            file_type=r.file_type,
            processing_status=r.status.value,
            warnings_count=len(r.warnings),
            warnings=" | ".join(r.warnings),
            errors=" | ".join(r.errors),
            llm_calls=r.llm_calls,
            input_tokens=r.input_tokens,
            output_tokens=r.output_tokens,
            processing_seconds=round(r.processing_seconds, 2),
        )
        if r.extraction:
            row.update(r.extraction.model_dump())
        if r.email:
            row["email_subject"] = r.email.subject
        if r.summary:
            row["recommended_next_action"] = r.summary.recommended_next_action
        row.update(r.output_files)
        return row

    def _rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.output_dir).as_posix()
        except ValueError:
            return str(path)


def _count_by(items: list, attribute: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        key = getattr(item, attribute)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
