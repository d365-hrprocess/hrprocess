"""Safe, end-to-end diagnostics for unconfirmed D365 writes; no real ERP mutations."""

from dataclasses import replace

import httpx
import pytest

from finance_demo_api.api import connector_key, create_app
from finance_demo_api.d365 import D365Client
from finance_demo_api.settings import Settings
from test_reporting import Metadata


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,expected_cause,upstream_status",
    [
        ("read_timeout", "d365_read_timeout", None),
        ("connect_timeout", "d365_connect_timeout", None),
        ("send_timeout", "d365_send_timeout", None),
        ("pool_timeout", "d365_pool_timeout", None),
        (403, "d365_access_denied", 403),
        (500, "d365_unavailable", 500),
        (429, "d365_throttled", 429),
    ],
)
async def test_create_retains_sanitized_cause_and_never_retries(
    monkeypatch, failure, expected_cause, upstream_status
):
    secret = "fictional-secret-never-returned"
    monkeypatch.setenv("FINANCE_DEMO_CLIENT_SECRET", secret)
    calls = []

    def upstream(request):
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(
                200,
                json={
                    "access_token": "fixture-token-never-returned",
                    "expires_in": 3600,
                },
            )
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"value": []})
        timeouts = {
            "read_timeout": httpx.ReadTimeout,
            "connect_timeout": httpx.ConnectTimeout,
            "send_timeout": httpx.WriteTimeout,
            "pool_timeout": httpx.PoolTimeout,
        }
        if failure in timeouts:
            raise timeouts[failure]("sensitive upstream secret/body should not leak")
        return httpx.Response(
            failure,
            json={"error": {"message": "sensitive ERP response should not leak"}},
        )

    settings = Settings.load()
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as d365_http:
        app = create_app(
            settings, client=D365Client(settings, d365_http), metadata=Metadata()
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="https://example.test",
            headers={"X-Finance-Demo-Key": connector_key(settings)},
        ) as api:
            result = await api.post(
                "/customers",
                json={
                    "customer_account": "TEST-COPILOT-102102",
                    "organization_name": "Copilot Demo Customer",
                },
            )
            assert result.status_code == 503
            error = result.json()["error"]
            assert error["code"] == "write_outcome_uncertain"
            assert error["cause_code"] == expected_cause
            assert error.get("upstream_status_code") == upstream_status
            assert error["stage"] == "d365_write"
            assert (
                error["outcome_uncertain"]
                and error["verification_required"]
                and not error["retryable"]
            )
            assert calls == ["GET", "POST"]
            for value in [
                secret,
                "fixture-token-never-returned",
                "sensitive ERP response",
                "sensitive upstream secret",
            ]:
                assert value not in result.text
            # A successful later absence lookup is separate from the earlier uncertain write.
            readback = await api.get("/customers/TEST-COPILOT-102102")
            assert (
                readback.status_code == 404
                and readback.json()["error"]["code"] == "customer_not_found"
            )
            assert calls.count("POST") == 1


@pytest.mark.asyncio
async def test_readback_failure_has_distinct_phase_and_cause(monkeypatch):
    monkeypatch.setenv("FINANCE_DEMO_CLIENT_SECRET", "fixture-secret")
    reads = 0

    def upstream(request):
        nonlocal reads
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(
                200, json={"access_token": "fixture-token", "expires_in": 3600}
            )
        if request.method == "POST":
            return httpx.Response(201, json={})
        reads += 1
        return (
            httpx.Response(200, json={"value": []})
            if reads == 1
            else httpx.Response(403)
        )

    settings = replace(Settings.load(), read_retry_attempts=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as d365_http:
        app = create_app(
            settings, client=D365Client(settings, d365_http), metadata=Metadata()
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://example.test"
        ) as api:
            result = await api.post(
                "/customers",
                headers={"X-Finance-Demo-Key": connector_key(settings)},
                json={
                    "customer_account": "TEST-COPILOT-102102",
                    "organization_name": "Copilot Demo Customer",
                },
            )
            error = result.json()["error"]
            assert error["code"] == "write_verification_failed"
            assert (
                error["cause_code"] == "d365_access_denied"
                and error["upstream_status_code"] == 403
            )
            assert error["stage"] == "write_readback" and not error["retryable"]
