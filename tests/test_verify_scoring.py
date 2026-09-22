"""Tests for the acceptance-scoring logic used by scripts/verify_submission.py."""
from scripts.verify_submission import evaluate_extractions

SPEC = {
    "doc": {
        "hard": {"customer_name": "Grace Kim", "email": "Not Provided", "phone_number": "6475550188",
                 "overall_case_status": ["Open", "In Progress"]},
        "soft": {"priority": ["Medium", "High"]},
        "forbidden": {"overall_case_status": ["Resolved"]},
    }
}
GOOD = {"customer_name": "Grace Kim", "email": "Not Provided", "phone_number": "647-555-0188",
        "overall_case_status": "Open", "priority": "Medium"}


def test_correct_extraction_scores_100():
    checks, accuracy = evaluate_extractions(SPEC, {"doc": GOOD})
    assert checks[0].status == "PASS"
    assert accuracy["overall_accuracy_pct"] == 100.0


def test_hallucinated_email_is_a_hard_failure():
    checks, _ = evaluate_extractions(SPEC, {"doc": {**GOOD, "email": "grace.kim@example.com"}})
    assert checks[0].status == "FAIL" and "email" in checks[0].detail


def test_forbidden_value_fails_and_soft_mismatch_warns():
    checks, _ = evaluate_extractions(SPEC, {"doc": {**GOOD, "overall_case_status": "Resolved"}})
    assert checks[0].status == "FAIL" and "forbidden" in checks[0].detail
    checks, accuracy = evaluate_extractions(SPEC, {"doc": {**GOOD, "priority": "Low"}})
    assert checks[0].status == "WARN" and accuracy["judgement_accuracy_pct"] == 0.0


def test_missing_document_fails():
    checks, _ = evaluate_extractions(SPEC, {})
    assert checks[0].status == "FAIL"
