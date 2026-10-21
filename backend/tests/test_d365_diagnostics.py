import json
import logging
from unittest.mock import Mock

import httpx
import pytest

from app.config import Settings
from app.d365_diagnostics import MAX_ERROR_BYTES, d365_error_diagnostics
from app.errors import ConfigurationError, IntegrationError
from app.services.d365 import D365Service


ACTIVITY_ID = "a920e4fe-5775-4ee5-b3c2-eafcb51a38bb"
COMPANY_MESSAGE = "Company must match the integration user's configured current company."


def diagnostic(body, *, headers=None, status=500):
    return d365_error_diagnostics(httpx.Response(status, json=body, headers=headers))


def test_known_company_exception_is_actionable_and_provider_activity_is_preserved():
    result = diagnostic({"Message": "An exception occured when invoking the operation - " + COMPANY_MESSAGE,
                         "ExceptionType": "Microsoft.Dynamics.Ax.Xpp.ErrorException", "ActivityId": ACTIVITY_ID})
    assert result["issue_code"] == "company_mismatch"
    assert result["provider_message"] == COMPANY_MESSAGE
    assert result["activity_id"] == ACTIVITY_ID
    assert result["exception_type"] == "Microsoft.Dynamics.Ax.Xpp.ErrorException"
    assert "D365_COMPANY" in result["hint"]


@pytest.mark.parametrize("table,field,size", [
    ("UBJobApplicationTable", "ApplicationId", 20),
    ("UBCandidateTable", "CandidateId", 30),
    ("UBCandidateTable", "IdentityKey", 40),
])
def test_field_length_errors_only_emit_known_metadata(table, field, size):
    message = f"{table}.{field} is missing or exceeds its existing field length ({size})."
    result = diagnostic({"error": {"innererror": {"message": message}}})
    assert result["issue_code"] == "field_length_or_missing"
    assert result["table"] == table
    assert result["field"] == field
    assert result["max_length"] == size
    assert result["provider_message"] == message


@pytest.mark.parametrize("message,code", [
    ("The candidate record could not be validated. Check the existing table's business defaults.", "candidate_validation_failed"),
    ("The application record could not be validated. Check the existing table's business defaults.", "application_validation_failed"),
    ("The evaluation record could not be validated. Check the existing table's business defaults.", "evaluation_validation_failed"),
    ("The requirement is not accepting applications.", "requirement_closed"),
    ("ApplicationId must contain the stable APP- identifier supplied by the backend.", "external_id_invalid"),
    ("UBJobApplicationTable.AIStatus must be an enum or string field for the status mapping.", "status_field_type_invalid"),
    ("Email must be a contact email address.", "contact_email_invalid"),
    ("MatchedSkillsJson must contain a valid JSON array; use [] for an empty collection.", "evaluation_json_invalid"),
])
def test_known_validation_messages(message, code):
    result = diagnostic({"Message": message})
    assert result["issue_code"] == code
    assert result["provider_message"] == message


def test_enum_error_suppresses_arbitrary_custom_enum_name():
    result = diagnostic({"Message": "Enum PrivateIdentitySentinel has no symbol 'Pending'. Set the service configuration method to your actual existing enum symbol."})
    assert result["issue_code"] == "enum_symbol_missing"
    assert result["enum_symbol"] == "Pending"
    assert "PrivateIdentitySentinel" not in json.dumps(result)
    assert "Pending" in result["provider_message"]


def test_echoed_request_values_and_unknown_text_are_never_logged_even_when_repeated():
    private_values = ["candidate@example.test", "PRIVATE-TOKEN-SENTINEL", "+91-PRIVATE-PHONE",
                      "Person Private Name", "https://blob.example.test/resume?sig=PRIVATE-SAS"]
    echoes = " ".join(private_values) * 4
    result = diagnostic({"Message": echoes + COMPANY_MESSAGE + echoes,
                         "ExceptionType": echoes, "ActivityId": echoes,
                         "StackTrace": echoes, "Request": {"Email": private_values[0]}})
    serialized = json.dumps(result)
    assert result["issue_code"] == "company_mismatch"
    assert result["provider_message"] == COMPANY_MESSAGE
    assert not any(value in serialized for value in private_values)
    assert "exception_type" not in result
    assert "activity_id" not in result
    assert all(isinstance(value, (str, int)) for value in result.values())


@pytest.mark.parametrize("body", [
    {"Message": "A provider diagnostic includes candidate@example.test and PRIVATE-TOKEN"},
    {"Message": {"Email": "candidate@example.test"}, "ExceptionType": "PRIVATE-TOKEN"},
    {"error": "candidate@example.test"},
    {"Email": COMPANY_MESSAGE},
    {"Message": "UnknownTable.SecretField is missing or exceeds its existing field length (20)."},
    {"Message": "UBCandidateTable.PrivateSecret is missing or exceeds its existing field length (20)."},
    {"Message": "Enum PRIVATE-TOKEN has no symbol 'PRIVATE-SUB'. Set the service configuration method to your actual existing enum symbol."},
    ["candidate@example.test"],
    None,
])
def test_unknown_and_hostile_json_is_bounded_generic_metadata(body):
    result = diagnostic(body)
    assert result["issue_code"] == "d365_unclassified"
    assert "provider_message" not in result
    assert "candidate@example.test" not in json.dumps(result)
    assert "PRIVATE-TOKEN" not in json.dumps(result)


def test_header_activity_id_accepts_only_uuid_and_never_echoes_arbitrary_headers():
    good = diagnostic({}, headers={"x-ms-activity-id": ACTIVITY_ID})
    assert good["activity_id"] == ACTIVITY_ID
    bad = diagnostic({}, headers={"x-ms-activity-id": "PRIVATE-TOKEN", "Authorization": "Bearer PRIVATE-TOKEN"})
    assert "activity_id" not in bad
    assert "PRIVATE-TOKEN" not in json.dumps(bad)


@pytest.mark.parametrize("content", [
    b"<html><body>PRIVATE-TOKEN and Company must match the integration user's configured current company.</body></html>",
    b"PRIVATE-TOKEN not json",
    b"{bad-json",
    b"\xff\xfeinvalid",
])
def test_non_json_and_html_bodies_are_suppressed(content):
    result = d365_error_diagnostics(httpx.Response(500, content=content))
    assert result["response_kind"] == "non_json"
    assert result["issue_code"] == "d365_unclassified"
    assert "provider_message" not in result
    assert "PRIVATE-TOKEN" not in json.dumps(result)


def test_oversized_response_and_message_are_not_parsed_or_emitted():
    result = diagnostic({"Message": COMPANY_MESSAGE + "PRIVATE-TOKEN" * MAX_ERROR_BYTES})
    assert result["response_kind"] == "oversized"
    assert result["issue_code"] == "d365_unclassified"
    result = diagnostic({"Message": COMPANY_MESSAGE + "PRIVATE-TOKEN" * 400})
    assert result["response_kind"] == "json"
    assert result["issue_code"] == "d365_unclassified"
    assert "PRIVATE-TOKEN" not in json.dumps(result)


def test_deep_json_and_double_serialized_service_response_are_safe():
    body = {"Message": COMPANY_MESSAGE}
    for _ in range(20):
        body = {"innererror": body}
    assert diagnostic(body)["issue_code"] == "d365_unclassified"
    result = diagnostic(json.dumps({"Message": COMPANY_MESSAGE}))
    assert result["issue_code"] == "company_mismatch"


@pytest.fixture
def settings():
    return Settings(_env_file=None, d365_enabled=True, d365_tenant_id="tenant-id",
                    d365_client_id="client-id", d365_client_secret="PRIVATE-CLIENT-SECRET")


@pytest.fixture
def msal_app():
    return Mock(acquire_token_for_client=Mock(return_value={"access_token": "PRIVATE-ACCESS-TOKEN", "expires_in": 3600}))


async def test_service_500_logs_reason_and_stage_without_exposing_body_or_changing_error(settings, msal_app, caplog):
    caplog.set_level(logging.INFO)
    body = {"Message": COMPANY_MESSAGE + " echoed candidate@example.test PRIVATE-CLIENT-SECRET",
            "ExceptionType": "ErrorException", "ActivityId": ACTIVITY_ID}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500, json=body))) as client:
        with pytest.raises(IntegrationError) as error:
            await D365Service(settings, client=client, msal_app=msal_app).submit_application({
                "requirement_id": "REQ0013", "candidate_name": "Private Candidate",
                "email": "candidate@example.test", "phone": "PRIVATE-PHONE",
            })
    assert str(error.value).startswith("D365 submit_application failed (HTTP 500). Reference: ")
    assert error.value.status_code == 502
    assert error.value.retryable is True
    failure = next(record.workflow_event for record in caplog.records
                   if hasattr(record, "workflow_event") and record.workflow_event["stage"] == "d365.http"
                   and record.workflow_event["outcome"] == "failed")
    assert failure["status_code"] == 500
    assert failure["correlation_id"] in str(error.value)
    assert failure["exception_type"] == "ErrorException"
    assert error.value.diagnostics["issue_code"] == "company_mismatch"
    assert error.value.diagnostics["activity_id"] == ACTIVITY_ID
    assert error.value.diagnostics["upstream_status_code"] == 500
    assert error.value.diagnostics["correlation_id"] == failure["correlation_id"]
    assert "PRIVATE-CLIENT-SECRET" not in json.dumps(error.value.diagnostics)
    assert "candidate@example.test" not in json.dumps(error.value.diagnostics)
    output = caplog.text
    assert "d365.auth.acquire" in output
    assert "d365.request.prepare" in output
    assert "d365.http" in output
    assert "company_mismatch" in output
    assert COMPANY_MESSAGE in output
    assert ACTIVITY_ID in output
    for private in ("PRIVATE-CLIENT-SECRET", "PRIVATE-ACCESS-TOKEN", "Private Candidate", "candidate@example.test", "PRIVATE-PHONE"):
        assert private not in output
        assert private not in str(error.value)


async def test_config_failure_logs_before_any_token_request(settings, msal_app, caplog):
    caplog.set_level(logging.INFO)
    settings.d365_enabled = False
    with pytest.raises(ConfigurationError):
        await D365Service(settings, msal_app=msal_app).get_open_requirements()
    assert "d365.config" in caplog.text
    assert "d365_disabled" in caplog.text
    msal_app.acquire_token_for_client.assert_not_called()


async def test_operation_path_failure_logs_before_any_token_request(settings, msal_app, caplog):
    caplog.set_level(logging.INFO)
    settings.d365_submit_application_path = ""
    with pytest.raises(ConfigurationError):
        await D365Service(settings, msal_app=msal_app).submit_application({})
    assert "d365.operation.config" in caplog.text
    assert "d365_operation_path_missing" in caplog.text
    msal_app.acquire_token_for_client.assert_not_called()


async def test_auth_failure_classifies_safe_error_without_description(settings, msal_app, caplog):
    caplog.set_level(logging.INFO)
    msal_app.acquire_token_for_client.return_value = {"error": "invalid_client", "error_description": "PRIVATE-CLIENT-SECRET"}
    with pytest.raises(IntegrationError):
        await D365Service(settings, msal_app=msal_app).get_open_requirements()
    assert "entra_invalid_client" in caplog.text
    assert "PRIVATE-CLIENT-SECRET" not in caplog.text


async def test_401_retry_and_token_cache_are_logged_without_changing_retry_count(settings, msal_app, caplog):
    caplog.set_level(logging.INFO)
    statuses = iter([401, 200, 200])
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(next(statuses), json={"Requirements": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = D365Service(settings, client=client, msal_app=msal_app)
        assert await service.get_open_requirements() == []
        assert await service.get_open_requirements() == []
    assert len(requests) == 3
    assert msal_app.acquire_token_for_client.call_count == 2
    assert "d365.auth.refresh" in caplog.text
    assert "d365.auth.cache" in caplog.text
    assert "d365.response.normalize" in caplog.text
    assert "PRIVATE-ACCESS-TOKEN" not in caplog.text
