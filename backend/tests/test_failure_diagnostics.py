"""Production error context survives wrappers; logs contain mechanism, never values."""

import asyncio
import json
import logging

import httpx
import pytest

from app.diagnostic_templates import safe_message_template, valid_message_template
from app.errors import IntegrationError
from app.failure_diagnostics import entra_diagnostics
from app.observability import error_fields, log_event, stage


@pytest.mark.parametrize(
    "kind,code",
    [
        (httpx.ConnectTimeout, "network_connect_timeout"),
        (httpx.ReadTimeout, "network_read_timeout"),
        (httpx.WriteTimeout, "network_write_timeout"),
        (httpx.PoolTimeout, "network_pool_timeout"),
        (httpx.ConnectError, "network_connection_failed"),
        (httpx.RemoteProtocolError, "network_protocol_failed"),
    ],
)
def test_network_failures_survive_generic_integration_wrappers(kind, code, caplog):
    caplog.set_level(logging.INFO, logger="app.workflow")
    with pytest.raises(IntegrationError):
        with stage("test.integration"):
            try:
                raise kind("PRIVATE-TOKEN private@example.test https://host.test?sig=PRIVATE-SAS")
            except kind as error:
                raise IntegrationError("Private Applicant provider response") from error
    event = next(
        r.workflow_event
        for r in caplog.records
        if hasattr(r, "workflow_event") and r.workflow_event["outcome"] == "failed"
    )
    assert event["issue_code"] == code and event["root_exception_type"] == kind.__name__
    assert "PRIVATE" not in caplog.text and "private@example.test" not in caplog.text
    assert "Private Applicant" not in caplog.text and "host.test" not in caplog.text


def test_d365_safe_cause_survives_outer_request_and_recovery_wrappers():
    details = {
        "issue_code": "field_length_or_missing",
        "field": "ApplicationId",
        "max_length": 20,
        "activity_id": "cb035259-4dd8-0000-ec66-bed5d84ddd01",
        "upstream_status_code": 500,
        "message_template": "the field length [redacted]",
        "root_exception_type": "ErrorException",
    }
    try:
        try:
            raise IntegrationError("private raw value", log_diagnostics=details)
        except IntegrationError as error:
            raise IntegrationError("another private value") from error
    except IntegrationError as error:
        fields = error_fields(error)
    assert fields["issue_code"] == details["issue_code"]
    assert fields["root_exception_type"] == "ErrorException"
    assert fields["upstream_status_code"] == 500 and fields["field"] == "ApplicationId"
    assert fields["activity_id"] == details["activity_id"]
    assert "private" not in json.dumps(fields)


@pytest.mark.parametrize(
    "number,code",
    [
        ("700016", "entra_application_missing"),
        ("7000215", "entra_secret_invalid"),
        ("7000222", "entra_secret_expired"),
        ("7000218", "entra_secret_missing"),
        ("70011", "entra_scope_invalid"),
        ("65001", "entra_consent_required"),
        ("53003", "entra_policy_blocked"),
        ("700027", "entra_assertion_invalid"),
    ],
)
def test_entra_diagnostics_keep_only_fixed_code_and_hint(number, code):
    result = entra_diagnostics(
        {"error_description": f"AADSTS{number}: private-client-id private@example.test PRIVATE-SECRET"}
    )
    assert result["issue_code"] == code and result["provider_code"] == "AADSTS" + number
    assert "private" not in json.dumps(result) and "PRIVATE-SECRET" not in json.dumps(result)


def test_templates_never_preserve_arbitrary_words_quotes_numbers_urls_or_control_characters(caplog):
    caplog.set_level(logging.INFO, logger="app.workflow")
    message = 'Cannot create record "Private Applicant". Email private@example.test; Token PRIVATE-TOKEN; amount 12345; https://host.test/?sig=secret\nPRIVATE-PHONE'
    template = safe_message_template(message)
    assert valid_message_template(template)
    log_event("test.provider", "failed", message_template=template, provider_message=message)
    assert "cannot create record" in caplog.text
    for private in [
        "Private Applicant",
        "private@example.test",
        "PRIVATE-TOKEN",
        "12345",
        "host.test",
        "PRIVATE-PHONE",
    ]:
        assert private not in caplog.text
    log_event("test.provider", "failed", message_template="Unfiltered Private Applicant")
    assert "Private Applicant" not in caplog.text


def test_cyclic_and_cancelled_exceptions_are_bounded():
    error = RuntimeError("private")
    error.__cause__ = error
    assert error_fields(error)["issue_code"] == "application_failure"
    assert error_fields(asyncio.CancelledError("private"))["root_exception_type"] == "CancelledError"


@pytest.mark.parametrize(
    "message,code",
    [
        ("certificate verify failed PRIVATE-SECRET", "network_tls_verification_failed"),
        ("getaddrinfo failed private.example.test", "network_dns_failed"),
        ("Connection refused private-host", "network_connection_refused"),
    ],
)
def test_connection_error_mechanisms_are_actionable_without_printing_address(message, code):
    result = error_fields(httpx.ConnectError(message))
    assert result["issue_code"] == code
    assert "PRIVATE-SECRET" not in json.dumps(result) and "private.example.test" not in json.dumps(result)
