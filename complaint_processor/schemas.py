"""Pydantic schemas.

1. LLM output schemas (passed to the model as a JSON schema for structured output):
   - ComplaintExtraction
   - CustomerEmail
   - CaseSummary
2. Workflow result models (DocumentResult, ProcessingStatus).

Field descriptions are part of the JSON schema the LLM receives, so they double
as field-level prompt instructions. Validators normalise the few ways a model
can still drift (e.g. "yes" vs "Yes", "N/A" vs "Not Provided").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator

NOT_PROVIDED = "Not Provided"

YesNo = Literal["Yes", "No"]

ComplaintCategory = Literal[
    "Billing & Payments",
    "Product Quality / Defect",
    "Delivery & Shipping",
    "Service Outage / Technical Issue",
    "Customer Service Experience",
    "Fraud & Security",
    "Account Management",
    "Refund & Cancellation",
    "Insurance Claim",
    "Other",
]

CaseStatus = Literal[
    "Open",
    "In Progress",
    "Pending Customer Response",
    "Escalated",
    "Resolved",
    "Closed",
]

Priority = Literal["Low", "Medium", "High", "Critical"]

_MISSING_TOKENS = {
    "", "n/a", "na", "none", "null", "nil", "unknown", "-", "--",
    "not provided", "not available", "not mentioned", "not specified", "not stated",
}
_YES = {"yes", "y", "true", "1"}
_NO = {"no", "n", "false", "0"}


def _clean_text(value: object) -> str:
    if value is None:
        return NOT_PROVIDED
    text = " ".join(str(value).split())
    return NOT_PROVIDED if text.lower() in _MISSING_TOKENS else text


def _to_yes_no(value: object) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    text = str(value).strip().lower()
    if text in _YES:
        return "Yes"
    if text in _NO:
        return "No"
    raise ValueError(f"Expected Yes/No, got '{value}'")


def _match_literal(value: object, literal_type) -> str:
    options = get_args(literal_type)
    text = str(value).strip().lower()
    for option in options:
        if option.lower() == text:
            return option
    raise ValueError(f"'{value}' is not one of {options}")


# ====================================================================== #
#  LLM output schemas
# ====================================================================== #
class ComplaintExtraction(BaseModel):
    """Structured information extracted from one customer document."""

    model_config = ConfigDict(extra="ignore")

    customer_name: str = Field(
        description="Full name of the customer who raised the case, exactly as written. 'Not Provided' if absent."
    )
    email: str = Field(
        description="Customer email address copied exactly from the document. 'Not Provided' if absent."
    )
    phone_number: str = Field(
        description="Customer phone number copied exactly from the document. 'Not Provided' if absent."
    )
    reference_number: str = Field(
        description="Case, ticket, order, claim or account reference number mentioned in the document. 'Not Provided' if absent."
    )
    product_or_service: str = Field(
        description="Product or service the case is about (e.g. 'Visa Platinum credit card'). 'Not Provided' if absent."
    )
    complaint_category: ComplaintCategory = Field(
        description="Best-fitting category for the case."
    )
    issue_description: str = Field(
        description="2-3 sentence factual description of the customer's issue, based only on the document."
    )
    resolution_provided: str = Field(
        description="Resolution or action already taken by the company, as stated in the document. 'Not Provided' if none is mentioned."
    )
    is_complaint: YesNo = Field(
        description="'Yes' if the customer reports a problem or dissatisfaction; 'No' for pure inquiries, compliments or feedback."
    )
    escalation_required: YesNo = Field(
        description="'Yes' if the document says the case was or must be escalated, or it contains escalation triggers "
                    "(regulator/legal threat, fraud or financial loss, safety risk, repeated unresolved contacts, "
                    "request for a manager). Otherwise 'No'."
    )
    supporting_document_available: YesNo = Field(
        description="'Yes' only if the document states that supporting material (receipt, invoice, photo, screenshot, "
                    "statement, report) is attached or has been provided. Otherwise 'No'."
    )
    overall_case_status: CaseStatus = Field(
        description="Current status of the case according to the document."
    )
    priority: Priority = Field(
        description="Business priority: Critical = fraud/safety/regulatory risk; High = financial loss or service "
                    "unavailable; Medium = service degraded or delayed; Low = minor issue or non-complaint."
    )

    @field_validator(
        "customer_name", "email", "phone_number", "reference_number",
        "product_or_service", "issue_description", "resolution_provided",
        mode="before",
    )
    @classmethod
    def _clean_strings(cls, value):
        return _clean_text(value)

    @field_validator("email", mode="after")
    @classmethod
    def _lowercase_email(cls, value: str) -> str:
        return value if value == NOT_PROVIDED else value.lower()

    @field_validator("is_complaint", "escalation_required", "supporting_document_available", mode="before")
    @classmethod
    def _normalize_yes_no(cls, value):
        return _to_yes_no(value)

    @field_validator("complaint_category", mode="before")
    @classmethod
    def _normalize_category(cls, value):
        try:
            return _match_literal(value, ComplaintCategory)
        except ValueError:
            return "Other"

    @field_validator("overall_case_status", mode="before")
    @classmethod
    def _normalize_status(cls, value):
        return _match_literal(value, CaseStatus)

    @field_validator("priority", mode="before")
    @classmethod
    def _normalize_priority(cls, value):
        return _match_literal(value, Priority)


class CustomerEmail(BaseModel):
    """Customer-facing response email."""

    model_config = ConfigDict(extra="ignore")

    subject: str = Field(description="Concise subject line. Include the reference number if one is available.")
    body: str = Field(
        description="Complete email body: greeting, issue summary, resolution/status, next steps and sign-off. "
                    "Plain text, paragraphs separated by blank lines."
    )

    @field_validator("subject", mode="before")
    @classmethod
    def _clean_subject(cls, value):
        text = " ".join(str(value).split())
        if not text:
            raise ValueError("subject must not be empty")
        return text

    @field_validator("body", mode="before")
    @classmethod
    def _clean_body(cls, value):
        text = str(value).strip()
        if len(text) < 40:
            raise ValueError("email body is too short")
        return text


class CaseSummary(BaseModel):
    """Internal management case summary."""

    model_config = ConfigDict(extra="ignore")

    case_overview: str = Field(description="1-2 sentences: who, what product/service, what happened.")
    key_issue: str = Field(description="The core problem in one sentence.")
    action_taken: str = Field(description="Actions already taken according to the document, or 'No action recorded'.")
    current_status: str = Field(description="Current case status and what it is waiting on.")
    recommended_next_action: str = Field(
        description="Concrete internal next step(s). This is a recommendation, not a fact from the document."
    )

    @field_validator("*", mode="before")
    @classmethod
    def _clean(cls, value):
        text = " ".join(str(value).split())
        if not text:
            raise ValueError("field must not be empty")
        return text


# ====================================================================== #
#  Workflow result models
# ====================================================================== #
class ProcessingStatus(str, Enum):
    SUCCESS = "SUCCESS"   # all three AI tasks completed
    PARTIAL = "PARTIAL"   # extraction OK, email and/or summary failed
    FAILED = "FAILED"     # ingestion or extraction failed
    SKIPPED = "SKIPPED"   # unsupported file type


@dataclass
class DocumentResult:
    doc_id: str
    file_name: str
    file_type: str
    status: ProcessingStatus = ProcessingStatus.FAILED
    extraction: ComplaintExtraction | None = None
    email: CustomerEmail | None = None
    summary: CaseSummary | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stage_seconds: dict[str, float] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    llm_calls: int = 0
    output_files: dict[str, str] = field(default_factory=dict)
    processing_seconds: float = 0.0
