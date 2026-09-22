"""Offline, deterministic mock client.

Purpose: run and test the full pipeline (ingestion -> orchestration -> guardrails
-> outputs -> report) without an API key or cost. It uses simple regex/keyword
heuristics and is NOT an LLM - output quality is intentionally basic. Use a real
provider (openai / azure_openai / gemini) for the actual submission outputs.
"""
from __future__ import annotations

import json
import re

from ..schemas import NOT_PROVIDED, CaseSummary, ComplaintExtraction, CustomerEmail
from .base import LLMClient, LLMResult, StructuredOutputError, T

_EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?:\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
_REFERENCE = re.compile(r"\b[A-Z]{2,6}-\d{3,}(?:-\d+)*\b")
_NAME_LABEL = re.compile(
    r"(?i:customer name|my name is|name|from|policyholder|account holder)\s*[:|\n]?\s*([A-Z][A-Za-z'.-]+(?:[ \t]+[A-Z][A-Za-z'.-]+){0,3})"
)
_SIGN_OFF = re.compile(
    r"(?i:regards|sincerely|thanks|thank you|cheers)[,!]?\s*\n+\s*([A-Z][A-Za-z'.-]+(?:[ \t]+[A-Z][A-Za-z'.-]+){0,3})"
)

_CATEGORY_KEYWORDS = [
    ("Fraud & Security", ("fraud", "unauthori", "stolen", "phishing", "scam")),
    ("Insurance Claim", ("claim", "policy number", "adjuster")),
    ("Service Outage / Technical Issue", ("outage", "no internet", "not working", "down", "disconnect")),
    ("Delivery & Shipping", ("deliver", "shipment", "courier", "shipping")),
    ("Billing & Payments", ("charged twice", "double charge", "billing", "invoice", "overcharg", "charge")),
    ("Product Quality / Defect", ("defect", "damaged", "broken", "faulty")),
    ("Refund & Cancellation", ("refund", "cancel")),
    ("Customer Service Experience", ("rude", "agent", "staff", "branch", "representative")),
    ("Account Management", ("account", "loyalty", "points")),
]


def _between(text: str, start: str, end: str) -> str:
    match = re.search(re.escape(start) + r"(.*?)" + re.escape(end), text, re.DOTALL)
    return match.group(1).strip() if match else ""


def _has(text: str, *words: str) -> bool:
    lower = text.lower()
    return any(w in lower for w in words)


def _first_sentences(text: str, n: int = 2, max_chars: int = 350) -> str:
    body = re.sub(r"^.*?(?:description|details|complaint|message)\s*[:|]\s*", "", text, count=1,
                  flags=re.IGNORECASE | re.DOTALL)
    sentences = re.split(r"(?<=[.!?])\s+", " ".join(body.split()))
    return " ".join(sentences[:n])[:max_chars] or NOT_PROVIDED


class MockLLMClient(LLMClient):
    provider = "mock"
    model = "mock-heuristic-v1"

    def generate_structured(self, system_prompt: str, user_prompt: str, schema: type[T]) -> LLMResult[T]:
        document = _between(user_prompt, "<document>", "</document>")
        if schema is ComplaintExtraction:
            parsed = self._extract(document)
        else:
            data = _between(user_prompt, "<extracted_data>", "</extracted_data>")
            if not data:
                raise StructuredOutputError("mock: missing <extracted_data> block")
            extraction = ComplaintExtraction.model_validate(json.loads(data))
            if schema is CustomerEmail:
                parsed = self._email(extraction, system_prompt)
            elif schema is CaseSummary:
                parsed = self._summary(extraction)
            else:
                raise StructuredOutputError(f"mock: unsupported schema {schema.__name__}")
        return LLMResult(
            parsed=parsed,  # type: ignore[arg-type]
            input_tokens=(len(system_prompt) + len(user_prompt)) // 4,
            output_tokens=len(parsed.model_dump_json()) // 4,
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract(doc: str) -> ComplaintExtraction:
        email = _EMAIL.search(doc)
        phone = _PHONE.search(doc)
        ref = _REFERENCE.search(doc)
        name = _NAME_LABEL.search(doc) or _SIGN_OFF.search(doc)

        category = next((c for c, kws in _CATEGORY_KEYWORDS if _has(doc, *kws)), "Other")
        is_complaint = not _has(doc, "compliment", "just wanted to say", "general inquiry", "enquiry only")
        escalated = bool(re.search(r"escalated|escalating|regulator|ombudsman|legal action|crtc|obsi", doc, re.I))
        resolved = _has(doc, "has been resolved", "refund has been", "was refunded", "issue resolved", "case was closed",
                        "case closed", "has been reversed")
        waiting = _has(doc, "please provide", "awaiting your", "we need the following", "pending documents")
        in_progress = _has(doc, "scheduled", "investigating", "under review", "in progress")

        if escalated:
            status = "Escalated"
        elif resolved:
            status = "Resolved"
        elif waiting:
            status = "Pending Customer Response"
        elif in_progress:
            status = "In Progress"
        else:
            status = "Open"

        if not is_complaint:
            priority = "Low"
        elif category == "Fraud & Security" or _has(doc, "regulator", "crtc", "safety"):
            priority = "Critical"
        elif escalated or _has(doc, "$", "charged"):
            priority = "High"
        else:
            priority = "Medium"

        resolution_match = re.search(r"(?:resolution provided|action taken|branch response)[^:\n]*[:|\n]\s*(.+)", doc, re.IGNORECASE)
        return ComplaintExtraction(
            customer_name=name.group(1).strip() if name else NOT_PROVIDED,
            email=email.group(0) if email else NOT_PROVIDED,
            phone_number=phone.group(0).strip() if phone else NOT_PROVIDED,
            reference_number=ref.group(0) if ref else NOT_PROVIDED,
            product_or_service=NOT_PROVIDED,
            complaint_category=category,
            issue_description=_first_sentences(doc),
            resolution_provided=resolution_match.group(1).strip() if resolution_match else NOT_PROVIDED,
            is_complaint="Yes" if is_complaint else "No",
            escalation_required="Yes" if escalated else "No",
            supporting_document_available="Yes" if _has(doc, "attached", "attachment", "enclosed", "photos", "screenshot") else "No",
            overall_case_status=status,
            priority=priority,
        )

    @staticmethod
    def _email(e: ComplaintExtraction, system_prompt: str) -> CustomerEmail:
        signature = _between(system_prompt + "\n9.", "Kind regards,\n", "\n9.") or "Customer Care Team"
        greeting = f"Dear {e.customer_name}," if e.customer_name != NOT_PROVIDED else "Dear Customer,"
        ref = f" (reference {e.reference_number})" if e.reference_number != NOT_PROVIDED else ""
        status_line = {
            "Resolved": "We are pleased to confirm that your case has been resolved.",
            "Closed": "Your case has now been closed.",
            "Escalated": "Your case has been escalated to a specialist team, who will contact you with an update.",
            "In Progress": "Our team is actively working on your case and will keep you updated.",
            "Pending Customer Response": "To proceed, we need some additional information from you as described in our previous correspondence.",
            "Open": "Our team is reviewing your case and will contact you with an update.",
        }[e.overall_case_status]
        body = (
            f"{greeting}\n\n"
            f"Thank you for contacting us{ref}. We understand your concern regarding the following: "
            f"{e.issue_description}\n\n"
            f"{status_line}"
            + (f" Resolution provided: {e.resolution_provided}" if e.resolution_provided != NOT_PROVIDED else "")
            + "\n\nWe appreciate your patience and apologise for any inconvenience caused.\n\n"
            f"Kind regards,\n{signature}"
        )
        return CustomerEmail(subject=f"Update on your case{ref}", body=body)

    @staticmethod
    def _summary(e: ComplaintExtraction) -> CaseSummary:
        next_action = {
            "Escalated": "Specialist team to own the case and confirm an update to the customer.",
            "Pending Customer Response": "Follow up with the customer for the outstanding information.",
            "Resolved": "Confirm customer satisfaction and close the case.",
            "Closed": "No further action; monitor for repeat contact.",
        }.get(e.overall_case_status, "Assign an owner and contact the customer with a plan.")
        return CaseSummary(
            case_overview=f"{e.customer_name} raised a {e.complaint_category} case (priority {e.priority}).",
            key_issue=e.issue_description,
            action_taken=e.resolution_provided if e.resolution_provided != NOT_PROVIDED else "No action recorded",
            current_status=e.overall_case_status,
            recommended_next_action=next_action,
        )
