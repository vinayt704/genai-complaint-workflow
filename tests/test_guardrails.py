from complaint_processor.guardrails import check_grounding, verify_extraction
from complaint_processor.schemas import NOT_PROVIDED, ComplaintExtraction

SOURCE = """Customer: Priya Raman, priya.raman@example.com, +1 (613) 555-0142.
Case CMP-2026-00417. Duplicate charge of $249.99 reversed."""


def extraction(**overrides):
    data = dict(
        customer_name="Priya Raman", email="priya.raman@example.com", phone_number="613-555-0142",
        reference_number="CMP-2026-00417", product_or_service="Visa", complaint_category="Billing & Payments",
        issue_description="Charged twice.", resolution_provided="Reversed.", is_complaint="Yes",
        escalation_required="No", supporting_document_available="Yes", overall_case_status="Resolved",
        priority="High",
    )
    data.update(overrides)
    return ComplaintExtraction(**data)


def test_grounded_extraction_passes_unchanged():
    corrected, warnings = verify_extraction(extraction(), SOURCE)
    assert warnings == []
    assert corrected.phone_number == "613-555-0142"  # different formatting still matches


def test_hallucinated_contact_details_are_removed():
    corrected, warnings = verify_extraction(
        extraction(email="someone.else@example.com", phone_number="999-555-1234", reference_number="CMP-1"),
        SOURCE,
    )
    assert corrected.email == corrected.phone_number == corrected.reference_number == NOT_PROVIDED
    assert len([w for w in warnings if "hallucination" in w]) == 3


def test_consistency_warning_for_escalated_but_resolved():
    _, warnings = verify_extraction(extraction(escalation_required="Yes"), SOURCE)
    assert any("Consistency" in w for w in warnings)


def test_grounding_accepts_source_values():
    text = "Dear Priya Raman, the charge of $249.99 on case CMP-2026-00417 was reversed. Call +1 613 555 0142."
    assert check_grounding(text, SOURCE) == []


def test_grounding_flags_invented_values_and_placeholders():
    text = ("We will refund $500.00 within 5 days. Ref CMP-9999. Email help@contoso.example "
            "or call 800-555-0000. Regards, [Agent Name]")
    issues = check_grounding(text, SOURCE)
    joined = " ".join(issues)
    for fragment in ("$500.00", "CMP-9999", "help@contoso.example", "800-555-0000", "[Agent Name]"):
        assert fragment in joined


def test_allowed_text_is_not_flagged():
    text = "Contact support@contoso.example"
    assert check_grounding(text, SOURCE, allowed_text="support@contoso.example") == []
