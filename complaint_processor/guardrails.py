"""Deterministic (non-LLM) guardrails.

1. verify_extraction  - contact details and reference numbers returned by the LLM
                        must literally appear in the source document; anything
                        else is removed and replaced with "Not Provided".
                        Also runs business-consistency checks.
2. check_grounding    - scans generated text (email / summary) for emails, phone
                        numbers, money amounts and reference numbers that do not
                        exist in the source, plus leftover template placeholders.

These checks are cheap, explainable and make hallucinations visible in the
final report instead of silently shipping them.
"""
from __future__ import annotations

import re

from .schemas import NOT_PROVIDED, ComplaintExtraction

EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
EMAIL_FULL = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
PHONE_PATTERN = re.compile(r"(?<![\w-])(?:\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?![\w-])")
MONEY_PATTERN = re.compile(
    r"(?:[$€£₹]\s?|\b(?:USD|CAD|INR|EUR|GBP|Rs\.?)\s?)\d[\d,]*(?:\.\d{1,2})?", re.IGNORECASE
)
REFERENCE_PATTERN = re.compile(r"\b[A-Z]{2,6}-\d{3,}(?:-\d+)*\b")
PLACEHOLDER_PATTERN = re.compile(
    r"\[(?:[A-Z][A-Za-z ]{1,30}|insert[^\]]*|your [^\]]*)\]|\{\{?\s*[a-z_ ]+\s*\}?\}|<[A-Z][A-Za-z ]{2,30}>"
)


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _digits_in_source(digits: str, source: str) -> bool:
    """True if the digit sequence appears in the source, allowing formatting chars between digits."""
    if not digits or len(digits) > 20:
        return False
    pattern = r"\D{0,3}".join(re.escape(d) for d in digits)
    return re.search(pattern, source) is not None


def _normalize_money(text: str) -> str:
    number = re.sub(r"[^\d.]", "", text).strip(".")
    try:
        return f"{float(number):.2f}"
    except ValueError:
        return number


# ---------------------------------------------------------------------- #
def verify_extraction(extraction: ComplaintExtraction, source_text: str) -> tuple[ComplaintExtraction, list[str]]:
    """Validate the LLM extraction against the source text.

    Returns a (possibly corrected) copy of the extraction and a list of warnings.
    """
    warnings: list[str] = []
    updates: dict[str, str] = {}
    source_lower = source_text.lower()

    # Email: valid format AND present in source
    if extraction.email != NOT_PROVIDED:
        if not EMAIL_FULL.match(extraction.email):
            warnings.append(f"Extracted email '{extraction.email}' has an invalid format - set to Not Provided")
            updates["email"] = NOT_PROVIDED
        elif extraction.email.lower() not in source_lower:
            warnings.append(f"Extracted email '{extraction.email}' not found in source - removed (possible hallucination)")
            updates["email"] = NOT_PROVIDED

    # Phone: digits must appear in source
    if extraction.phone_number != NOT_PROVIDED:
        digits = _digits(extraction.phone_number)
        if len(digits) < 7 or not _digits_in_source(digits, source_text):
            warnings.append(
                f"Extracted phone '{extraction.phone_number}' not found in source - removed (possible hallucination)"
            )
            updates["phone_number"] = NOT_PROVIDED

    # Reference number must appear in source (ignoring spaces / case)
    if extraction.reference_number != NOT_PROVIDED:
        compact_ref = re.sub(r"\s+", "", extraction.reference_number.lower())
        if compact_ref not in re.sub(r"\s+", "", source_lower):
            warnings.append(
                f"Extracted reference '{extraction.reference_number}' not found in source - removed (possible hallucination)"
            )
            updates["reference_number"] = NOT_PROVIDED

    # Name: warn only (names can legitimately be re-ordered, e.g. "Last, First")
    if extraction.customer_name != NOT_PROVIDED:
        tokens = [t for t in re.split(r"[\s,]+", extraction.customer_name.lower()) if len(t) > 1]
        if tokens and not any(t in source_lower for t in tokens):
            warnings.append(f"Customer name '{extraction.customer_name}' not found in source - please verify")

    corrected = extraction.model_copy(update=updates) if updates else extraction

    # Business consistency checks (warnings only - flagged for human review)
    if corrected.escalation_required == "Yes" and corrected.overall_case_status in ("Resolved", "Closed"):
        warnings.append("Consistency: escalation required but case status is Resolved/Closed - review")
    if corrected.overall_case_status == "Resolved" and corrected.resolution_provided == NOT_PROVIDED:
        warnings.append("Consistency: status Resolved but no resolution recorded - review")
    if corrected.is_complaint == "No" and corrected.escalation_required == "Yes":
        warnings.append("Consistency: non-complaint document flagged for escalation - review")
    if corrected.email == NOT_PROVIDED and corrected.phone_number == NOT_PROVIDED:
        warnings.append("No customer contact details found - response email cannot be delivered automatically")

    return corrected, warnings


def check_grounding(generated_text: str, source_text: str, allowed_text: str = "") -> list[str]:
    """Return a list of ungrounded items found in generated text.

    ``allowed_text`` holds legitimate values that are not in the source
    (e.g. the company name / signature).
    """
    reference = f"{source_text}\n{allowed_text}"
    reference_lower = reference.lower()
    issues: list[str] = []

    for email in set(EMAIL_PATTERN.findall(generated_text)):
        if email.lower() not in reference_lower:
            issues.append(f"mentions email address '{email}' that is not in the source document")

    for phone in set(PHONE_PATTERN.findall(generated_text)):
        if not _digits_in_source(_digits(phone), reference):
            issues.append(f"mentions phone number '{phone.strip()}' that is not in the source document")

    source_amounts = {_normalize_money(m) for m in MONEY_PATTERN.findall(reference)}
    for amount in set(MONEY_PATTERN.findall(generated_text)):
        if _normalize_money(amount) not in source_amounts:
            issues.append(f"mentions amount '{amount.strip()}' that is not in the source document")

    for ref in set(REFERENCE_PATTERN.findall(generated_text)):
        if ref.lower() not in reference_lower:
            issues.append(f"mentions reference '{ref}' that is not in the source document")

    for placeholder in set(PLACEHOLDER_PATTERN.findall(generated_text)):
        issues.append(f"contains an unfilled template placeholder '{placeholder}'")

    return sorted(issues)
