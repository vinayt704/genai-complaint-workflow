import pytest
from pydantic import ValidationError

from complaint_processor.schemas import NOT_PROVIDED, CaseSummary, ComplaintExtraction, CustomerEmail

BASE = dict(
    customer_name="Jane Doe", email="Jane.Doe@Example.com", phone_number="613-555-0100",
    reference_number="CMP-100", product_or_service="Card", complaint_category="Billing & Payments",
    issue_description="Charged twice.", resolution_provided="Refunded.", is_complaint="Yes",
    escalation_required="No", supporting_document_available="No", overall_case_status="Resolved",
    priority="High",
)


def make(**overrides):
    return ComplaintExtraction(**{**BASE, **overrides})


def test_valid_extraction_and_email_lowercased():
    assert make().email == "jane.doe@example.com"


@pytest.mark.parametrize("raw, expected", [(True, "Yes"), ("yes", "Yes"), ("N", "No"), (False, "No")])
def test_yes_no_normalisation(raw, expected):
    assert make(escalation_required=raw).escalation_required == expected


@pytest.mark.parametrize("raw", ["", "N/A", None, "unknown", "  not provided "])
def test_missing_values_become_not_provided(raw):
    assert make(phone_number=raw).phone_number == NOT_PROVIDED


def test_case_insensitive_literals_and_category_fallback():
    extraction = make(overall_case_status="in progress", priority="critical", complaint_category="Weird stuff")
    assert extraction.overall_case_status == "In Progress"
    assert extraction.priority == "Critical"
    assert extraction.complaint_category == "Other"


def test_invalid_status_is_rejected():
    with pytest.raises(ValidationError):
        make(overall_case_status="Maybe")


def test_invalid_yes_no_is_rejected():
    with pytest.raises(ValidationError):
        make(is_complaint="perhaps")


def test_email_body_must_not_be_trivial():
    with pytest.raises(ValidationError):
        CustomerEmail(subject="Hi", body="Too short")


def test_summary_rejects_empty_fields():
    with pytest.raises(ValidationError):
        CaseSummary(case_overview="", key_issue="x", action_taken="x", current_status="x", recommended_next_action="x")
