import csv
import json

from complaint_processor.llm.base import LLMClient, LLMResult
from complaint_processor.llm.mock_client import MockLLMClient
from complaint_processor.schemas import CustomerEmail, ProcessingStatus
from complaint_processor.workflow import ComplaintProcessingWorkflow, assign_doc_ids


def test_end_to_end_batch_with_mock_provider(settings):
    workflow = ComplaintProcessingWorkflow(settings, MockLLMClient(), MockLLMClient())
    batch = workflow.run(clean_output=True)

    by_name = {r.file_name: r for r in batch.results}
    assert batch.count(ProcessingStatus.SUCCESS) == 8
    assert by_name["complaint_009_corrupted.pdf"].status == ProcessingStatus.FAILED
    assert by_name["complaint_010_empty.txt"].status == ProcessingStatus.FAILED
    assert by_name["customer_list.csv"].status == ProcessingStatus.SKIPPED

    out = settings.output_dir
    for sub, ext in (("structured_data", "json"), ("customer_emails", "txt"), ("case_summaries", "md")):
        assert len(list((out / sub).glob(f"*.{ext}"))) == 8

    with open(out / "final_report.csv", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 11  # every input file is accounted for, including failures
    assert json.loads((out / "run_summary.json").read_text())["status_counts"]["SUCCESS"] == 8


def test_duplicate_stems_get_unique_ids(tmp_path):
    ids = assign_doc_ids([tmp_path / "a.pdf", tmp_path / "a.txt", tmp_path / "b.txt"])
    assert sorted(ids.values()) == ["a_pdf", "a_txt", "b"]


class FailingEmailLLM(MockLLMClient):
    """Email generation always fails; the summary must still be produced."""

    def generate_structured(self, system_prompt, user_prompt, schema):
        if schema is CustomerEmail:
            raise TimeoutError("simulated provider timeout")
        return super().generate_structured(system_prompt, user_prompt, schema)


def test_task_failure_is_isolated_as_partial(settings):
    workflow = ComplaintProcessingWorkflow(settings, MockLLMClient(), FailingEmailLLM())
    result = workflow.process_file(settings.data_dir / "complaint_003.txt")
    assert result.status == ProcessingStatus.PARTIAL
    assert result.summary is not None and result.email is None
    assert any("customer_email failed" in e for e in result.errors)


class HallucinatingThenFixedLLM(LLMClient):
    """First email draft invents a refund amount; second draft is clean."""

    provider, model = "stub", "stub"

    def __init__(self):
        self.email_calls = 0
        self._mock = MockLLMClient()

    def generate_structured(self, system_prompt, user_prompt, schema):
        if schema is CustomerEmail:
            self.email_calls += 1
            if self.email_calls == 1:
                assert "grounding check" not in user_prompt
                body = "Dear Meera Joshi,\n\nWe will refund $999.00 and add a $50 voucher.\n\nKind regards,\nTeam"
            else:
                assert "grounding check" in user_prompt  # feedback was passed back to the model
                body = "Dear Meera Joshi,\n\nYour replacement delivery is scheduled.\n\nKind regards,\nTeam"
            return LLMResult(parsed=CustomerEmail(subject="Your order ORD-448120", body=body))
        return self._mock.generate_structured(system_prompt, user_prompt, schema)


def test_grounding_self_correction_loop(settings):
    llm = HallucinatingThenFixedLLM()
    workflow = ComplaintProcessingWorkflow(settings, MockLLMClient(), llm)
    result = workflow.process_file(settings.data_dir / "complaint_003.txt")
    assert llm.email_calls == 2
    assert "$999" not in result.email.body
    assert not any("Customer email grounding" in w for w in result.warnings)
