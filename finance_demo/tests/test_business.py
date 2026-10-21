from decimal import Decimal
from types import SimpleNamespace

import pytest

from finance_demo_api.business import FinanceBusiness, literal
from finance_demo_api.errors import FinanceError


CUSTOMER_FIELDS = {
    "account": "CustomerAccount", "company": "dataAreaId", "name": "OrganizationName",
    "currency": "SalesCurrencyCode", "group": "CustomerGroupId", "payment_terms": "PaymentTerms",
    "party_type": "PartyType",
}
TRANSACTION_FIELDS = {
    "account": "AccountNum", "company": "dataAreaId", "invoice": "Invoice",
    "voucher": "Voucher", "transaction_date": "TransDate", "due_date": "DueDate",
    "amount": "AmountCur", "transaction_type": "TransType",
}


class Schema:
    def __init__(self, entity_set="CustomersV3", fields=None):
        self.entity_set = entity_set
        self.fields = fields or CUSTOMER_FIELDS

    def field(self, name):
        return self.fields[name]

    def key_path(self, company, account):
        return f"{self.entity_set}(dataAreaId={literal(company)},CustomerAccount={literal(account)})"


class Metadata:
    def require_customers(self):
        return Schema()

    def require_transactions(self):
        return Schema("CustTransBiEntities", TRANSACTION_FIELDS)


class Client:
    def __init__(self, reads=(), writes=()):
        self.reads = list(reads)
        self.writes = list(writes)
        self.calls = []

    async def json_request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        result = self.reads.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if self.writes:
            result = self.writes.pop(0)
            if isinstance(result, BaseException):
                raise result
        return None


def settings():
    return SimpleNamespace(company="usmf", demo_customer_prefix="TEST-COPILOT-", currency="INR", customer_group="20", payment_terms="Net30")


def row(account="TEST-COPILOT-001", name="Demo Customer"):
    return {"CustomerAccount": account, "dataAreaId": "usmf", "OrganizationName": name,
            "SalesCurrencyCode": "INR", "CustomerGroupId": "20", "PaymentTerms": "Net30", "PartyType": "Organization"}


def listing(*rows):
    return {"value": list(rows)}


@pytest.mark.asyncio
async def test_customer_lookup_scopes_company_escapes_account_and_uses_equality():
    client = Client([listing(row("O'NEIL"))])
    result = await FinanceBusiness(settings(), client, Metadata()).get_customer("O'NEIL")
    assert result["customer_account"] == "O'NEIL"
    params = client.calls[0][2]["params"]
    assert params["$filter"] == "dataAreaId eq 'usmf' and CustomerAccount eq 'O''NEIL'"
    assert params["cross-company"] == "true"
    assert "contains" not in params["$filter"]


@pytest.mark.asyncio
async def test_lists_are_bounded_and_reject_cross_company_data():
    client = Client([listing(row("AST-001"))])
    data, meta = await FinanceBusiness(settings(), client, Metadata()).list_customers(5)
    assert meta == {"company": "usmf", "limited": True, "limit": 5, "returned": 1}
    assert data[0]["customer_account"] == "AST-001"
    assert client.calls[0][2]["params"]["$top"] == "5"
    wrong = row(); wrong["dataAreaId"] = "demf"
    with pytest.raises(FinanceError) as exc:
        await FinanceBusiness(settings(), Client([listing(wrong)]), Metadata()).get_customer("TEST-COPILOT-001")
    assert exc.value.code == "d365_scope_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize("method,args", [("create_customer", ("AST-001", "Name")), ("update_customer", ("AST-001", "Name")), ("delete_customer", ("AST-001",))])
async def test_ordinary_accounts_cannot_be_mutated(method, args):
    client = Client()
    with pytest.raises(FinanceError) as exc:
        await getattr(FinanceBusiness(settings(), client, Metadata()), method)(*args)
    assert exc.value.status_code == 403
    assert client.calls == []


@pytest.mark.asyncio
async def test_create_sets_configured_defaults_and_reads_record_back():
    client = Client([listing(), listing(row())])
    result = await FinanceBusiness(settings(), client, Metadata()).create_customer("TEST-COPILOT-001", "Demo Customer")
    assert result["organization_name"] == "Demo Customer"
    assert [c[0] for c in client.calls] == ["GET", "POST", "GET"]
    payload = client.calls[1][2]["json"]
    assert payload == row()
    assert client.calls[1][2]["params"] == {"cross-company": "true"}


@pytest.mark.asyncio
async def test_duplicate_create_never_sends_a_write():
    client = Client([listing(row())])
    with pytest.raises(FinanceError) as exc:
        await FinanceBusiness(settings(), client, Metadata()).create_customer("TEST-COPILOT-001", "Demo Customer")
    assert exc.value.code == "customer_exists" and exc.value.status_code == 409
    assert [c[0] for c in client.calls] == ["GET"]


@pytest.mark.asyncio
async def test_update_uses_exact_key_and_requires_confirmed_name():
    client = Client([listing(row()), listing(row(name="New name"))])
    result = await FinanceBusiness(settings(), client, Metadata()).update_customer("TEST-COPILOT-001", "New name")
    assert result["organization_name"] == "New name"
    assert client.calls[1][0] == "PATCH"
    assert client.calls[1][1] == "CustomersV3(dataAreaId='usmf',CustomerAccount='TEST-COPILOT-001')"
    assert client.calls[1][2]["json"] == {"OrganizationName": "New name"}


@pytest.mark.asyncio
async def test_unconfirmed_write_is_not_reported_as_success():
    client = Client([listing(row()), listing(row(name="Old name"))])
    with pytest.raises(FinanceError) as exc:
        await FinanceBusiness(settings(), client, Metadata()).update_customer("TEST-COPILOT-001", "New name")
    assert exc.value.uncertain and not exc.value.retryable
    assert sum(c[0] == "PATCH" for c in client.calls) == 1


@pytest.mark.asyncio
async def test_delete_succeeds_only_after_verified_empty_lookup():
    client = Client([listing(row()), listing()])
    result = await FinanceBusiness(settings(), client, Metadata()).delete_customer("TEST-COPILOT-001")
    assert result == {"customer_account": "TEST-COPILOT-001", "company": "usmf", "deleted": True, "verified": True}
    assert [c[0] for c in client.calls] == ["GET", "DELETE", "GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize("verify", [FinanceError("denied", "Denied", 403), FinanceError("gone", "Entity missing", 404), FinanceError("timeout", "Timeout", 503), listing(row())])
async def test_delete_readback_failure_is_never_proof_of_deletion(verify):
    client = Client([listing(row()), verify])
    with pytest.raises(FinanceError) as exc:
        await FinanceBusiness(settings(), client, Metadata()).delete_customer("TEST-COPILOT-001")
    assert exc.value.uncertain
    assert [c[0] for c in client.calls] == ["GET", "DELETE", "GET"]


@pytest.mark.asyncio
async def test_write_timeout_has_one_attempt_and_requires_manual_verification():
    client = Client([listing()], [FinanceError("timeout", "Timeout", 503, uncertain=True)])
    with pytest.raises(FinanceError) as exc:
        await FinanceBusiness(settings(), client, Metadata()).create_customer("TEST-COPILOT-001", "Demo Customer")
    assert exc.value.code == "write_outcome_uncertain" and exc.value.uncertain
    assert [c[0] for c in client.calls] == ["GET", "POST"]


@pytest.mark.asyncio
async def test_entity_404_is_not_a_missing_customer():
    client = Client([FinanceError("missing", "Entity missing", 404)])
    with pytest.raises(FinanceError) as exc:
        await FinanceBusiness(settings(), client, Metadata()).get_customer("AST-001")
    assert exc.value.status_code == 503
    assert exc.value.code == "customer_entity_unavailable"


@pytest.mark.asyncio
async def test_recent_transactions_use_accountnum_and_exact_decimal_strings_without_balance():
    trans = {"AccountNum": "AST-001", "dataAreaId": "usmf", "Invoice": "INV1", "Voucher": "V1", "TransDate": "2026-01-01", "DueDate": "2026-02-01", "AmountCur": Decimal("1234567890123.456789"), "TransType": "Sales"}
    client = Client([listing(row("AST-001")), listing(trans)])
    data, meta = await FinanceBusiness(settings(), client, Metadata()).transactions("AST-001", 10)
    assert data[0]["amount"] == "1234567890123.456789"
    assert meta["complete_balance"] is False and meta["limited"] is True
    assert "balance" not in data[0]
    assert client.calls[-1][2]["params"]["$filter"] == "dataAreaId eq 'usmf' and AccountNum eq 'AST-001'"
    assert client.calls[-1][2]["params"]["$orderby"] == "TransDate desc"


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["AST/001", "AST(001)", "AST 001", "AST\n001", "a" * 41])
async def test_invalid_identifiers_never_reach_d365(account):
    client = Client()
    with pytest.raises(FinanceError) as exc:
        await FinanceBusiness(settings(), client, Metadata()).get_customer(account)
    assert exc.value.status_code == 422 and not client.calls
