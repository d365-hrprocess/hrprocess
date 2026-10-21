import asyncio
from dataclasses import replace
import hashlib
import hmac

import httpx
import pytest
from fastapi.testclient import TestClient

from finance_demo_api.api import connector_key, create_app
from finance_demo_api.cli import main as cli_main
from finance_demo_api.errors import FinanceError
from finance_demo_api.settings import Settings

SECRET = "fictional-test-secret-not-a-real-credential"


class Metadata:
    def __init__(self, ready=True):
        self.ready = ready
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    async def close(self):
        self.closed = True

    def require_customers(self):
        if not self.ready:
            raise FinanceError("metadata_warming", "Finance metadata is warming. Try again shortly.", 503, True)
        return object()

    def status(self):
        return {"state": "ready" if self.ready else "warming", "ready": self.ready,
                "capabilities": {"customers": "available", "transactions": "unavailable"}}


class Client:
    pass


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("FINANCE_DEMO_CLIENT_SECRET", SECRET)
    return Settings.load()


def auth(settings):
    return {"X-Finance-Demo-Key": connector_key(settings)}


def test_derived_key_matches_exact_hmac_and_is_stable(settings):
    message = f"finance-demo/copilot/v1|{settings.tenant_id}|{settings.client_id}"
    expected = hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
    assert connector_key(settings) == expected == connector_key(settings)
    assert len(expected) == 64 and expected != SECRET


def test_key_rotates_with_secret(settings, monkeypatch):
    old = connector_key(settings)
    monkeypatch.setenv("FINANCE_DEMO_CLIENT_SECRET", "a-new-fictional-secret")
    assert connector_key(settings) != old


@pytest.mark.asyncio
async def test_public_health_reveals_only_minimal_status_without_secret(monkeypatch):
    monkeypatch.delenv("FINANCE_DEMO_CLIENT_SECRET", raising=False)
    app = create_app(Settings.load(), client=Client(), metadata=Metadata())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://example.test") as c:
        r = await c.get("/health")
        assert r.status_code == 200 and r.json() == {"status": "ok"}
        for path in ["/ready", "/metadata/status", "/customers", "/swagger2.json"]:
            r = await c.get(path)
            assert r.status_code == 503
            assert r.json()["error"]["code"] == "finance_secret_missing"
            assert SECRET not in r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/ready", "/metadata/status", "/customers", "/customers/AST-001", "/customers/AST-001/transactions", "/swagger2.json"])
async def test_all_business_diagnostics_and_schema_require_key(settings, path):
    app = create_app(settings, client=Client(), metadata=Metadata())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://example.test") as c:
        for headers in [{}, {"X-Finance-Demo-Key": "x" * 64}, {"X-Finance-Demo-Key": SECRET}]:
            r = await c.get(path, headers=headers)
            assert r.status_code == 401
            assert r.json()["error"]["code"] == "invalid_connector_key"
            assert SECRET not in r.text


@pytest.mark.asyncio
async def test_ready_is_fast_retryable_503_while_warming(settings):
    app = create_app(settings, client=Client(), metadata=Metadata(ready=False))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://example.test") as c:
        async with asyncio.timeout(1):
            r = await c.get("/ready", headers=auth(settings))
        assert r.status_code == 503 and r.headers["retry-after"] == "5"
        assert r.json()["error"]["retryable"] is True
        status = await c.get("/metadata/status", headers=auth(settings))
        assert status.status_code == 200 and status.json()["data"]["state"] == "warming"


@pytest.mark.asyncio
async def test_customer_ready_does_not_imply_transaction_capability(settings):
    app = create_app(settings, client=Client(), metadata=Metadata())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://example.test") as c:
        r = await c.get("/ready", headers=auth(settings))
        assert r.status_code == 200
        assert r.json()["data"]["capabilities"]["transactions"] == "unavailable"


def test_disabled_finance_never_initializes_or_grants_business_access(settings):
    metadata = Metadata()
    app = create_app(replace(settings, enabled=False), client=Client(), metadata=metadata)
    with TestClient(app) as c:
        assert metadata.started is False
        assert c.get("/health").json() == {"status": "disabled"}
        r = c.get("/customers", headers=auth(settings))
        assert r.status_code == 503 and r.json()["error"]["code"] == "finance_disabled"
    assert metadata.closed


def test_own_process_lifespan_explicitly_starts_background_warmup(settings):
    metadata = Metadata(ready=False)
    app = create_app(settings, client=Client(), metadata=metadata)
    with TestClient(app) as c:
        assert metadata.started is True
        assert c.get("/health").status_code == 200
    assert metadata.closed


@pytest.mark.asyncio
async def test_proxy_prefix_does_not_duplicate_or_redirect(settings):
    app = create_app(settings, client=Client(), metadata=Metadata())
    transport = httpx.ASGITransport(app, root_path="/finance")
    async with httpx.AsyncClient(transport=transport, base_url="https://example.test") as c:
        r = await c.get("/finance/ready", headers=auth(settings))
        assert r.status_code == 200
        assert "location" not in r.headers
        wrong = await c.get("/finance/finance/ready", headers=auth(settings))
        assert wrong.status_code == 404 and wrong.headers["content-type"].startswith("application/json")


@pytest.mark.asyncio
async def test_request_validation_rejects_extra_fields_limits_and_injection(settings):
    app = create_app(settings, client=Client(), metadata=Metadata())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://example.test", headers=auth(settings)) as c:
        for query in ["limit=0", "limit=21", "limit=abc"]:
            r = await c.get("/customers?" + query)
            assert r.status_code == 422
        r = await c.post("/customers", json={"customer_account": "TEST-COPILOT-001", "organization_name": "Name", "company": "demf"})
        assert r.status_code == 422
        r = await c.post("/customers", json={"customer_account": "a) or 1 eq 1", "organization_name": "Name"})
        assert r.status_code == 422
        r = await c.patch("/customers/TEST-COPILOT-001", json={"organization_name": " \n "})
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_unexpected_error_is_sanitized(settings):
    metadata = Metadata()
    def broken_status():
        raise RuntimeError(SECRET + " Bearer do-not-leak")
    metadata.status = broken_status
    app = create_app(settings, client=Client(), metadata=metadata)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://example.test") as c:
        r = await c.get("/metadata/status", headers=auth(settings))
        assert r.status_code == 500 and r.json()["error"]["code"] == "finance_internal_error"
        assert SECRET not in r.text and "Bearer" not in r.text


@pytest.mark.asyncio
async def test_write_budget_timeout_requires_verification_and_cannot_look_successful(settings):
    app = create_app(replace(settings, business_operation_timeout_seconds=.01), client=Client(), metadata=Metadata())
    async def slow(account, name):
        await asyncio.sleep(1)
    app.state.business.create_customer = slow
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://example.test") as c:
        r = await c.post("/customers", headers=auth(settings), json={"customer_account": "TEST-COPILOT-001", "organization_name": "Name"})
        assert r.status_code == 504
        assert r.json()["success"] is False
        assert r.json()["error"]["outcome_uncertain"] is True
        assert r.json()["error"]["verification_required"] is True
        assert r.json()["error"]["retryable"] is False


def test_cli_intentionally_prints_only_derived_key(settings, capsys):
    assert cli_main(["show-connector-key"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == connector_key(settings)
    assert SECRET not in captured.out and not captured.err


def test_cli_missing_secret_does_not_print_a_fake_key(settings, monkeypatch, capsys):
    monkeypatch.delenv("FINANCE_DEMO_CLIENT_SECRET", raising=False)
    assert cli_main(["show-connector-key"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "VALUE" in captured.err


@pytest.mark.asyncio
async def test_supplied_secret_identifier_is_never_accepted_as_value(settings, monkeypatch):
    monkeypatch.setenv("FINANCE_DEMO_CLIENT_SECRET", "fb07b3d3-b14c-4880-b49e-04d818160c0f")
    app = create_app(settings, client=Client(), metadata=Metadata())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://example.test") as c:
        r = await c.get("/ready", headers={"X-Finance-Demo-Key": "x" * 64})
        assert r.status_code == 503 and r.json()["error"]["code"] == "finance_secret_invalid"


@pytest.mark.asyncio
async def test_invalid_upstream_field_types_fail_the_typed_response_contract(settings):
    app = create_app(settings, client=Client(), metadata=Metadata())
    async def bad_customer(account):
        return {"customer_account": account, "company": "usmf", "organization_name": 123}
    app.state.business.get_customer = bad_customer
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://example.test") as c:
        r = await c.get("/customers/AST-001", headers=auth(settings))
        assert r.status_code == 502
        assert r.json()["error"]["code"] == "invalid_d365_response"


@pytest.mark.asyncio
async def test_typed_customer_response_matches_the_connector_contract(settings):
    app = create_app(settings, client=Client(), metadata=Metadata())
    async def customer(account):
        return {"customer_account": account, "company": "usmf", "organization_name": "Demo"}
    app.state.business.get_customer = customer
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://example.test") as c:
        r = await c.get("/customers/AST-001", headers=auth(settings))
        assert r.status_code == 200
        assert r.json()["data"] == {"customer_account": "AST-001", "company": "usmf", "organization_name": "Demo", "currency": None, "customer_group": None, "payment_terms": None}
