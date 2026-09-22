"""The three AI tasks of the workflow.

Task 1  Document -> ComplaintExtraction           (extraction LLM + verify_extraction)
Task 2  Document + Extraction -> CustomerEmail    (generation LLM + grounding check/self-correction)
Task 3  Document + Extraction -> CaseSummary      (generation LLM + grounding check/self-correction)

Each task is a small, independently testable function. The orchestration
(ordering, parallelism, error isolation) lives in workflow.py.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Generic, TypeVar

from pydantic import BaseModel

from .config import Settings
from .guardrails import check_grounding, verify_extraction
from .ingestion import SourceDocument
from .llm.base import LLMClient
from .prompts import build_email_prompt, build_extraction_prompt, build_summary_prompt
from .schemas import CaseSummary, ComplaintExtraction, CustomerEmail

logger = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)


@dataclass
class TaskOutput(Generic[M]):
    result: M
    warnings: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    llm_calls: int = 0


# ---------------------------------------------------------------------- #
def run_extraction(llm: LLMClient, document: SourceDocument) -> TaskOutput[ComplaintExtraction]:
    system, user = build_extraction_prompt(document)
    response = llm.generate_structured(system, user, ComplaintExtraction)
    extraction, warnings = verify_extraction(response.parsed, document.text)
    for w in warnings:
        logger.warning("%s: %s", document.file_name, w)
    logger.info(
        "%s: extracted category=%s status=%s escalation=%s",
        document.file_name, extraction.complaint_category,
        extraction.overall_case_status, extraction.escalation_required,
    )
    return TaskOutput(
        result=extraction,
        warnings=warnings,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        llm_calls=response.attempts,
    )


def _generate_grounded(
    task_name: str,
    llm: LLMClient,
    document: SourceDocument,
    schema: type[M],
    build_prompt: Callable[[list[str] | None], tuple[str, str]],
    to_text: Callable[[M], str],
    allowed_text: str,
    max_corrections: int,
) -> TaskOutput[M]:
    """Generate -> grounding check -> (optional) regenerate with feedback."""
    output: TaskOutput[M] | None = None
    feedback: list[str] | None = None
    issues: list[str] = []

    for attempt in range(max_corrections + 1):
        system, user = build_prompt(feedback)
        response = llm.generate_structured(system, user, schema)
        if output is None:
            output = TaskOutput(result=response.parsed)
        output.result = response.parsed
        output.input_tokens += response.input_tokens
        output.output_tokens += response.output_tokens
        output.llm_calls += response.attempts

        issues = check_grounding(to_text(response.parsed), document.text, allowed_text)
        if not issues:
            if attempt > 0:
                logger.info("%s: %s passed grounding check after self-correction", document.file_name, task_name)
            break
        logger.warning(
            "%s: %s grounding issues (attempt %d/%d): %s",
            document.file_name, task_name, attempt + 1, max_corrections + 1, "; ".join(issues),
        )
        feedback = issues

    assert output is not None
    output.warnings = [f"{task_name} grounding: {issue}" for issue in issues]
    return output


def run_email_generation(
    llm: LLMClient, document: SourceDocument, extraction: ComplaintExtraction, settings: Settings
) -> TaskOutput[CustomerEmail]:
    return _generate_grounded(
        task_name="Customer email",
        llm=llm,
        document=document,
        schema=CustomerEmail,
        build_prompt=lambda fb: build_email_prompt(
            document, extraction, settings.company_name, settings.email_signature, fb
        ),
        to_text=lambda email: f"{email.subject}\n{email.body}",
        allowed_text=f"{settings.company_name}\n{settings.email_signature}",
        max_corrections=settings.grounding_max_corrections,
    )


def run_summary_generation(
    llm: LLMClient, document: SourceDocument, extraction: ComplaintExtraction, settings: Settings
) -> TaskOutput[CaseSummary]:
    return _generate_grounded(
        task_name="Case summary",
        llm=llm,
        document=document,
        schema=CaseSummary,
        build_prompt=lambda fb: build_summary_prompt(document, extraction, fb),
        to_text=lambda s: " ".join(s.model_dump().values()),
        allowed_text=settings.company_name,
        max_corrections=settings.grounding_max_corrections,
    )
