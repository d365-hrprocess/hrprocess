import logging
import uuid

import httpx
import pytest

from finance_demo_api.api import create_app, connector_key
from finance_demo_api.errors import FinanceError
from finance_demo_api.observability import SafeProviderLogs, log_event
from finance_demo_api.settings import Settings
from test_reporting import Metadata, Source


def events(caplog):
    return [r.finance_event for r in caplog.records if hasattr(r, "finance_event")]


@pytest.mark.asyncio
async def test_finance_http_logs_templates_and_failure_cause_without_account_key_or_body(
    monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger="finance.workflow")
    secret = "PRIVATE-FINANCE-SECRET"
    monkeypatch.setenv("FINANCE_DEMO_CLIENT_SECRET", secret)
    settings = Settings.load()
    app = create_app(settings, client=Source([]), metadata=Metadata())

    async def failed(account):
        raise FinanceError(
            "write_outcome_uncertain",
            "Safe failure.",
            uncertain=True,
            cause_code="d365_read_timeout",
            stage="d365_write",
        )

    app.state.business.delete_customer = failed
    key = connector_key(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://example.test"
    ) as client:
        result = await client.delete(
            "/customers/TEST-COPILOT-PRIVATE?key=PRIVATE-QUERY",
            headers={"X-Finance-Demo-Key": key},
        )
    assert result.status_code == 503
    failure = next(e for e in events(caplog) if e["stage"] == "finance.operation")
    assert failure["cause_code"] == "d365_read_timeout" and failure["outcome_uncertain"]
    assert str(uuid.UUID(failure["request_id"])) == failure["request_id"]
    http = next(e for e in events(caplog) if e["stage"] == "finance.http")
    assert http["route"] == "/customers/{account}" and http["elapsed_ms"] >= 0
    for private in [secret, key, "TEST-COPILOT-PRIVATE", "PRIVATE-QUERY"]:
        assert private not in caplog.text


def test_finance_filters_drop_sdk_payloads_access_queries_and_large_numbers(caplog):
    caplog.set_level(logging.INFO, logger="finance.workflow")
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        "%s %s %s %s %s",
        ("127.0.0.1", "GET", "/customers/PRIVATE?key=PRIVATE-KEY", "1.1", 200),
        None,
    )
    assert SafeProviderLogs().filter(record) and "PRIVATE" not in record.getMessage()
    log_event(
        "finance.test",
        "completed",
        count=10**1000,
        payload="PRIVATE-SECRET",
        status_code=200,
    )
    assert events(caplog)[-1] == {
        "stage": "finance.test",
        "outcome": "completed",
        "status_code": 200,
    }


def test_entra_error_code_logs_no_token_response_text(caplog):
    from finance_demo_api.observability import log_entra_error

    caplog.set_level(logging.INFO, logger="finance.workflow")
    log_entra_error(
        httpx.Response(
            400,
            json={
                "error_description": "AADSTS7000222: PRIVATE-SECRET private@example.test"
            },
        )
    )
    assert events(caplog)[-1]["issue_code"] == "entra_secret_expired"
    assert events(caplog)[-1]["provider_code"] == "AADSTS7000222"
    assert (
        "PRIVATE-SECRET" not in caplog.text
        and "private@example.test" not in caplog.text
    )
