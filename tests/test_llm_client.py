"""LLM client tests.

The integration test starts a local fake OpenAI-compatible server and runs the
REAL LangChain ChatOpenAI client against it, so the structured-output request
and parsing path is exercised without an API key.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from complaint_processor.config import Settings
from complaint_processor.llm.base import StructuredOutputError
from complaint_processor.llm.factory import create_llm_clients
from complaint_processor.llm.langchain_client import LangChainLLMClient, _is_retryable
from complaint_processor.schemas import CaseSummary, ComplaintExtraction, CustomerEmail
from complaint_processor.workflow import ComplaintProcessingWorkflow

# ---------------------------------------------------------------------- #
#  Unit tests with a stub chat model
# ---------------------------------------------------------------------- #
class _StubRunnable:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0

    def invoke(self, _messages):
        self.calls += 1
        item = self.outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _StubChatModel:
    def __init__(self, outputs):
        self.runnable = _StubRunnable(outputs)

    def with_structured_output(self, schema, include_raw=False):
        assert include_raw is True
        return self.runnable


class AuthenticationError(Exception):
    pass


GOOD_SUMMARY = CaseSummary(case_overview="a", key_issue="b", action_taken="c",
                           current_status="d", recommended_next_action="e")


def test_retries_transient_errors_then_succeeds(monkeypatch):
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda _s: None)
    model = _StubChatModel([
        ConnectionError("network blip"),
        {"raw": None, "parsed": None, "parsing_error": ValueError("bad json")},
        {"raw": None, "parsed": GOOD_SUMMARY, "parsing_error": None},
    ])
    client = LangChainLLMClient(model, "stub", "stub", max_retries=3)
    result = client.generate_structured("s", "u", CaseSummary)
    assert result.parsed == GOOD_SUMMARY and result.attempts == 3


def test_parsing_error_exhausts_retries(monkeypatch):
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda _s: None)
    bad = {"raw": None, "parsed": None, "parsing_error": ValueError("bad")}
    client = LangChainLLMClient(_StubChatModel([bad, bad]), "stub", "stub", max_retries=2)
    with pytest.raises(StructuredOutputError):
        client.generate_structured("s", "u", CaseSummary)


def test_authentication_errors_are_not_retried():
    model = _StubChatModel([AuthenticationError("invalid key")])
    client = LangChainLLMClient(model, "stub", "stub", max_retries=3)
    with pytest.raises(AuthenticationError):
        client.generate_structured("s", "u", CaseSummary)
    assert model.runnable.calls == 1
    assert _is_retryable(ConnectionError()) and not _is_retryable(AuthenticationError())


# ---------------------------------------------------------------------- #
#  Integration test: real ChatOpenAI against a fake OpenAI server
# ---------------------------------------------------------------------- #
CANNED = {
    "ComplaintExtraction": {
        "customer_name": "Meera Joshi", "email": "meera.joshi@example.org", "phone_number": "416-555-0173",
        "reference_number": "ORD-448120", "product_or_service": "Contoso AquaPro 8kg washing machine",
        "complaint_category": "Delivery & Shipping",
        "issue_description": "Washing machine delivered with cracked door glass and dented top panel.",
        "resolution_provided": "Replacement approved; delivery scheduled for 18 September 2026.",
        "is_complaint": "Yes", "escalation_required": "No", "supporting_document_available": "Yes",
        "overall_case_status": "In Progress", "priority": "Medium",
    },
    "CustomerEmail": {
        "subject": "Your replacement washing machine - Order ORD-448120",
        "body": "Dear Meera Joshi,\n\nThank you for letting us know about the damaged washing machine. "
                "We have approved a replacement, which is scheduled for delivery on 18 September 2026.\n\n"
                "Kind regards,\nCustomer Care Team, Contoso",
    },
    "CaseSummary": {
        "case_overview": "Meera Joshi received a damaged washing machine (order ORD-448120).",
        "key_issue": "Unit delivered with cracked door glass and dented panel.",
        "action_taken": "Replacement approved and scheduled for 18 September 2026.",
        "current_status": "In Progress - awaiting replacement delivery.",
        "recommended_next_action": "Logistics to confirm delivery and collection; follow up after delivery.",
    },
}


class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    requests: list = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(body)
        schema_name = body["response_format"]["json_schema"]["name"]
        payload = {
            "id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "stop", "logprobs": None,
                         "message": {"role": "assistant", "content": json.dumps(CANNED[schema_name]), "refusal": None}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160},
        }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        pass


@pytest.fixture
def fake_openai_url():
    _FakeOpenAIHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()


def test_real_chatopenai_structured_output_via_fake_server(fake_openai_url, settings):
    openai_settings = Settings(
        **{**settings.__dict__, "llm_provider": "openai", "openai_api_key": "test-key",
           "openai_base_url": fake_openai_url, "llm_max_retries": 1}
    )
    extraction_llm, generation_llm = create_llm_clients(openai_settings)
    workflow = ComplaintProcessingWorkflow(openai_settings, extraction_llm, generation_llm)
    result = workflow.process_file(settings.data_dir / "complaint_003.txt")

    assert result.status.value == "SUCCESS", result.errors
    assert isinstance(result.extraction, ComplaintExtraction)
    assert result.extraction.overall_case_status == "In Progress"
    assert isinstance(result.email, CustomerEmail) and "Meera Joshi" in result.email.body
    assert result.input_tokens == 360 and result.output_tokens == 120  # 3 calls x usage
    assert result.warnings == []

    sent = _FakeOpenAIHandler.requests
    assert {r["response_format"]["json_schema"]["name"] for r in sent} == set(CANNED)
    assert all(r["response_format"]["type"] == "json_schema" for r in sent)
    extraction_request = next(r for r in sent if r["response_format"]["json_schema"]["name"] == "ComplaintExtraction")
    assert extraction_request["temperature"] == 0.0
    assert "<document>" in extraction_request["messages"][1]["content"]


def test_real_azure_chatopenai_via_fake_server(fake_openai_url, settings):
    azure_settings = Settings(
        **{**settings.__dict__, "llm_provider": "azure_openai",
           "azure_openai_endpoint": fake_openai_url.removesuffix("/v1"),
           "azure_openai_api_key": "test-key", "azure_openai_deployment": "gpt-4o-mini-deploy",
           "llm_max_retries": 1}
    )
    azure_settings.validate()
    extraction_llm, generation_llm = create_llm_clients(azure_settings)
    assert extraction_llm.model == "gpt-4o-mini-deploy"
    result = ComplaintProcessingWorkflow(azure_settings, extraction_llm, generation_llm).process_file(
        settings.data_dir / "complaint_003.txt"
    )
    assert result.status.value == "SUCCESS", result.errors
    assert result.extraction.reference_number == "ORD-448120"
