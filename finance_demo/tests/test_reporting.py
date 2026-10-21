"""Synthetic accounting and transport checks; these do not claim live ERP reconciliation."""

import asyncio
from dataclasses import replace
from urllib.parse import urlencode

import httpx
import pytest

from finance_demo_api.api import connector_key, create_app
from finance_demo_api.d365 import D365Client
from finance_demo_api.errors import FinanceError
from finance_demo_api.metadata import EntitySchema, parse_metadata
from finance_demo_api.reporting import FinanceReporting, financial_schema
from finance_demo_api.settings import Settings


TX_PROPERTIES = {
    "dataAreaId": "Edm.String",
    "SourceKey": "Edm.Int64",
    "AccountNum": "Edm.String",
    "Invoice": "Edm.String",
    "Voucher": "Edm.String",
    "TransDate": "Edm.DateTimeOffset",
    "DueDate": "Edm.DateTimeOffset",
    "CurrencyCode": "Edm.String",
    "AmountCur": "Edm.Decimal",
    "SettleAmountCur": "Edm.Decimal",
    "TransType": "Microsoft.Dynamics.DataEntities.LedgerTransType",
}
TX = EntitySchema(
    "CustTransBiEntities",
    "Microsoft.Dynamics.DataEntities.CustTransBiEntity",
    ("dataAreaId", "SourceKey"),
    TX_PROPERTIES,
    {"company": "dataAreaId", "account": "AccountNum"},
    {
        "TransType": [
            "None",
            "Sales",
            "Payment",
            "Project",
            "Cust",
            "Interest",
            "Invoice",
            "CustPayment",
        ]
    },
)
CUSTOMER = EntitySchema(
    "CustomersV3",
    "Demo.Customer",
    ("dataAreaId", "CustomerAccount"),
    {
        "dataAreaId": "Edm.String",
        "CustomerAccount": "Edm.String",
        "OrganizationName": "Edm.String",
        "SalesCurrencyCode": "Edm.String",
        "CustomerGroupId": "Edm.String",
        "PaymentTerms": "Edm.String",
        "PartyType": "Edm.String",
    },
    {
        "company": "dataAreaId",
        "account": "CustomerAccount",
        "name": "OrganizationName",
        "currency": "SalesCurrencyCode",
        "group": "CustomerGroupId",
        "payment_terms": "PaymentTerms",
        "party_type": "PartyType",
    },
)


def row(
    key,
    amount="75000",
    settled="25000",
    kind="Sales",
    invoice="FTI-1",
    account="AST-001",
    currency="INR",
    due="2026-10-01",
    trans="2026-09-01",
):
    return {
        "dataAreaId": "usmf",
        "SourceKey": str(key),
        "AccountNum": account,
        "Invoice": invoice,
        "Voucher": "V-" + str(key),
        "TransDate": trans,
        "DueDate": due,
        "CurrencyCode": currency,
        "AmountCur": amount,
        "SettleAmountCur": settled,
        "TransType": kind,
    }


class Metadata:
    def require_customers(self):
        return CUSTOMER

    def require_transactions(self):
        return TX

    def start(self):
        pass

    async def close(self):
        pass

    def status(self):
        return {"ready": True}


class Source:
    def __init__(self, rows, page_size=2, fail_page=None, customers=25):
        self.rows, self.page_size, self.fail_page, self.calls = (
            rows,
            page_size,
            fail_page,
            [],
        )
        self.customer_rows = [
            {
                "dataAreaId": "usmf",
                "CustomerAccount": f"C-{i:03}",
                "OrganizationName": f"Customer {i}",
            }
            for i in range(customers)
        ]
        self.customer_rows.append(
            {
                "dataAreaId": "usmf",
                "CustomerAccount": "AST-001",
                "OrganizationName": "AST customer",
            }
        )

    async def json_request(self, method, entity, params):
        assert method == "GET" and entity == "CustomersV3"
        return {"value": [self.customer_rows[-1]]}

    async def reporting_page(self, entity, params, link):
        assert "$top" not in params
        assert params["cross-company"] == "true"
        assert "dataAreaId eq 'usmf'" in params["$filter"]
        self.calls.append((entity, params, link))
        index = int(link) if link else 0
        if entity == "CustTransBiEntities" and index == self.fail_page:
            raise FinanceError("d365_access_denied", "D365 denied access.", 403)
        values = self.rows if entity == "CustTransBiEntities" else self.customer_rows
        value = {"value": values[index : index + self.page_size]}
        if index + self.page_size < len(values):
            value["@odata.nextLink"] = str(index + self.page_size)
        return value


def reporting(rows, **kwargs):
    settings = replace(Settings.load(), **kwargs)
    source = Source(rows)
    return FinanceReporting(settings, source, Metadata())


async def load(service, account="AST-001", currency=None):
    with pytest.raises(FinanceError) as error:
        await service.snapshot(account, currency)
    assert error.value.code == "report_preparing"
    await service.entries[(account, currency)]["task"]
    return await service.snapshot(account, currency)


@pytest.mark.asyncio
async def test_partial_settlement_unapplied_credit_and_no_double_subtraction():
    service = reporting(
        [
            row(1),
            row(2, "35000", "0", invoice="FTI-2"),
            row(3, "-25000", "-25000", kind="Payment", invoice="FTI-1"),
            row(4, "-10000", "0", kind="Payment", invoice=""),
        ]
    )
    await load(service)
    data, meta = await service.report("balance", account="AST-001")
    value = data["balances"][0]
    assert value["net_balance"] == "75000"
    assert value["open_invoice_total"] == value["debit_open_total"] == "85000"
    assert value["credit_open_total"] == "-10000"
    assert meta["complete"] and meta["transaction_pages"] == 2
    invoices, _ = await service.report("open_invoices", account="AST-001")
    assert len(invoices) == 2
    payments, meta = await service.report("payments", account="AST-001")
    assert len(payments) == 2  # includes fully settled payment
    assert meta["payment_period_totals"] == [
        {"currency": "INR", "received_amount": "35000"}
    ]
    await service.close()


@pytest.mark.asyncio
async def test_mixed_currencies_never_combined_output_page_not_calculation_limit():
    service = reporting([row(1), row(2, "10", "0", currency="USD"), row(3, "5", "0")])
    await load(service)
    data, meta = await service.report("balance", account="AST-001")
    assert {v["currency"]: v["net_balance"] for v in data["balances"]} == {
        "INR": "50005",
        "USD": "10",
    }
    rows, meta = await service.report("open_invoices", account="AST-001", limit=1)
    assert len(rows) == 1 and meta["total_rows"] == 3 and meta["has_more"]
    assert len(meta["totals"]) == 2


@pytest.mark.asyncio
async def test_overdue_due_today_sentinels_future_transactions_and_past_cutoff():
    service = reporting(
        [
            row(1, "100", "0"),
            row(2, "200", "0", due="2026-10-21"),
            row(3, "300", "0", due="1900-01-01"),
            row(4, "400", "0", due="0001-01-01"),
            row(5, "500", "0", due=None),
            row(6, "600", "0", trans="2026-10-22"),
            row(7, "700", "0", trans=None),
        ]
    )
    await load(service)
    rows, meta = await service.report(
        "overdue_invoices", account="AST-001", as_of_date="2026-10-21"
    )
    assert len(rows) == 2 and {r["remaining_amount"] for r in rows} == {"100", "700"}
    assert rows[0]["days_overdue"] == 20
    assert meta["totals"][0]["overdue_total"] == "800"
    assert meta["totals"][0]["unknown_due_date_count"] == 3
    assert meta["totals"][0]["unknown_due_date_amount"] == "1200"
    rows, meta = await service.report(
        "overdue_invoices", account="AST-001", as_of_date="2026-09-30"
    )
    assert rows == [] and "historical" in meta["basis"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("AmountCur", None),
        ("SettleAmountCur", ""),
        ("AmountCur", "NaN"),
        ("AmountCur", 1.5),
        ("SettleAmountCur", "oops"),
    ],
)
async def test_bad_amounts_publish_no_partial_snapshot(field, value):
    bad = row(2)
    bad[field] = value
    service = reporting([row(1), bad])
    with pytest.raises(FinanceError):
        await service.snapshot("AST-001")
    await service.entries[("AST-001", None)]["task"]
    with pytest.raises(FinanceError) as error:
        await service.snapshot("AST-001")
    assert error.value.code == "invalid_source_amount"
    assert service.entries[("AST-001", None)]["snapshot"] is None


@pytest.mark.asyncio
async def test_company_scan_beyond_twenty_customers_and_full_master_name_join():
    service = reporting(
        [row(i + 1, str(i + 1), "0", account=f"C-{i:03}") for i in range(25)]
    )
    await load(service, None)
    rows, meta = await service.report("customer_balances", limit=2, offset=20)
    assert len(rows) == 2 and rows[0]["customer_account"] == "C-020"
    assert rows[0]["organization_name"] == "Customer 20"
    assert meta["total_rows"] == 25 and meta["source_customer_count"] == 26
    assert meta["totals"][0]["net_balance"] == "325"
    assert len([c for c in service.client.calls if c[0] == "CustTransBiEntities"]) == 13


@pytest.mark.asyncio
async def test_failed_later_page_and_duplicate_keys_publish_no_totals():
    service = reporting([row(1), row(2), row(3)])
    service.client.fail_page = 2
    with pytest.raises(FinanceError):
        await service.snapshot("AST-001")
    await service.entries[("AST-001", None)]["task"]
    with pytest.raises(FinanceError) as error:
        await service.snapshot("AST-001")
    assert error.value.code == "d365_access_denied"
    service = reporting([row(1), row(1)])
    with pytest.raises(FinanceError):
        await load(service)
    assert service.entries[("AST-001", None)]["error"].code == "duplicate_reporting_key"


@pytest.mark.asyncio
async def test_unknown_positive_type_is_not_reported_as_complete_invoice_classification():
    service = reporting(
        [
            row(1, "100", "0", kind="Interest"),
            row(2, "25", "0", kind=2),
            row(3, "50", "0", kind="Payment"),
        ]
    )
    await load(service)
    data, meta = await service.report("balance", account="AST-001")
    value = data["balances"][0]
    assert value["net_balance"] == "175"
    assert value["open_invoice_total"] == "0"
    assert value["unclassified_positive_open_amount"] == "125"
    assert meta["invoice_classification_complete"] is False
    rows, _ = await service.report("open_invoices", account="AST-001")
    assert rows == []  # a payment with an invoice reference is still a payment


@pytest.mark.asyncio
async def test_settled_invoice_and_qualified_ambiguity():
    service = reporting([row(1, "50", "50")])
    await load(service)
    data, _ = await service.report("invoice", account="AST-001", invoice_number="FTI-1")
    assert data["status"] == "settled" and data["remaining_amount"] == "0"
    with pytest.raises(FinanceError) as error:
        await service.report("invoice", account="AST-001", invoice_number="DRAFT-1")
    assert error.value.code == "posted_invoice_not_found"
    service = reporting([row(1), row(2)])
    await load(service)
    with pytest.raises(FinanceError) as error:
        await service.report("invoice", account="AST-001", invoice_number="FTI-1")
    assert error.value.code == "invoice_ambiguous"


@pytest.mark.asyncio
async def test_singleflight_short_lived_cache_and_scan_limits():
    service = reporting([row(1)])
    for _ in range(4):
        with pytest.raises(FinanceError):
            await service.snapshot("AST-001")
    await service.entries[("AST-001", None)]["task"]
    await service.snapshot("AST-001")
    assert len(service.client.calls) == 1
    service.entries[("AST-001", None)]["snapshot"].completed -= 61
    with pytest.raises(FinanceError) as error:
        await service.snapshot("AST-001")
    assert error.value.code == "report_preparing"
    await service.close()
    for kwargs, code in [
        ({"reporting_maximum_records": 1}, "report_record_limit"),
        ({"reporting_maximum_pages": 1}, "report_page_limit"),
    ]:
        service = reporting([row(1), row(2), row(3)], **kwargs)
        with pytest.raises(FinanceError):
            await load(service)
        assert service.entries[("AST-001", None)]["error"].code == code


def test_financial_strategy_fails_closed_without_verified_enum_or_settlement_property():
    financial_schema(TX)
    for schema in [
        replace(TX, enum_members={}),
        replace(TX, properties={**TX.properties, "SettleAmountCur": "Edm.String"}),
        replace(TX, properties={**TX.properties, "TransType": "Edm.Int32"}),
    ]:
        with pytest.raises(FinanceError):
            financial_schema(schema)


def test_metadata_persists_namespaced_transaction_enum_members_and_inherited_keys(
    tmp_path,
):
    from test_metadata import csdl

    text = csdl().replace('Type="Edm.String"/><', 'Type="Edm.String"/><')
    text = text.replace(
        '<Property Name="TransType" Type="Edm.String"/>',
        '<Property Name="TransType" Type="D.LedgerTransType"/>',
    )
    text = text.replace(
        "<EntityContainer",
        '<EnumType Name="LedgerTransType"><Member Name="Sales" Value="2"/><Member Name="Payment" Value="3"/></EnumType><EntityContainer',
    )
    p = tmp_path / "test.xml"
    p.write_text(text)
    parsed = parse_metadata(p, Settings.load())
    schema = EntitySchema.from_dict(parsed["transactions"])
    assert schema.enum_members["TransType"] == ["Sales", "Payment"]
    assert schema.properties["TransType"] == "Demo.LedgerTransType"


@pytest.mark.asyncio
async def test_safe_pagination_keeps_scope_ieee_headers_and_rejects_host_filter_entity_changes(
    monkeypatch,
):
    monkeypatch.setenv("FINANCE_DEMO_CLIENT_SECRET", "synthetic-test-secret")
    calls = []

    def handle(req):
        calls.append(req)
        if req.url.host == "login.microsoftonline.com":
            return httpx.Response(
                200, json={"access_token": "fixture-token", "expires_in": 3600}
            )
        return httpx.Response(200, json={"value": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = D365Client(Settings.load(), http)
        params = {
            "cross-company": "true",
            "$filter": "dataAreaId eq 'usmf'",
            "$select": "AccountNum,AmountCur",
        }
        await client.reporting_page("CustTransBiEntities", params, "?$skiptoken=next")
        assert calls[-1].url.params["$filter"] == params["$filter"]
        assert calls[-1].headers["accept"] == "application/json;IEEE754Compatible=true"
        assert calls[-1].headers["authorization"] == "Bearer fixture-token"
        count = len(calls)
        links = [
            "https://evil.example/data/CustTransBiEntities?$skiptoken=x",
            "/data/CustomersV3?$skiptoken=x",
            "?$skiptoken=x&cross-company=false",
            "?$skiptoken=x&" + urlencode({"$filter": "dataAreaId eq 'demf'"}),
            "?$skiptoken=x&$skiptoken=y",
            "?$skiptoken=x&$top=1",
            "?$skiptoken=x#fragment",
        ]
        for link in links:
            with pytest.raises(FinanceError) as error:
                await client.reporting_page("CustTransBiEntities", params, link)
            assert error.value.code == "unsafe_pagination_link"
        assert len(calls) == count


@pytest.mark.asyncio
async def test_new_api_routes_authentication_fast_preparing_and_json_success(
    monkeypatch,
):
    settings = Settings.load()
    monkeypatch.setenv("FINANCE_DEMO_CLIENT_SECRET", "fixture-not-a-real-key")
    app = create_app(settings, client=Source([row(1)]), metadata=Metadata())
    paths = [
        "/customers/AST-001/balance",
        "/customers/AST-001/open-invoices",
        "/customers/AST-001/overdue-invoices",
        "/customers/AST-001/invoices/FTI-1",
        "/customers/AST-001/payments",
        "/reports/customer-balances",
        "/reports/overdue-customers",
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app, root_path="/finance"),
        base_url="https://example.test",
    ) as http:
        for path in paths:
            assert (await http.get("/finance" + path)).status_code == 401
        headers = {"X-Finance-Demo-Key": connector_key(settings)}
        async with asyncio.timeout(1):
            result = await http.get(
                "/finance/customers/AST-001/balance", headers=headers
            )
        assert (
            result.status_code == 503
            and result.json()["error"]["code"] == "report_preparing"
        )
        assert result.json()["error"]["outcome_uncertain"] is False
        await app.state.reporting.entries[("AST-001", None)]["task"]
        result = await http.get("/finance/customers/AST-001/balance", headers=headers)
        assert (
            result.status_code == 200
            and result.json()["data"]["balances"][0]["net_balance"] == "50000"
        )
        for suffix in ["?currency=INR%27%20or%201%20eq%201", "?currency=inr"]:
            assert (
                await http.get(
                    "/finance/customers/AST-001/balance" + suffix, headers=headers
                )
            ).status_code == 422
        assert (
            await http.get(
                "/finance/reports/customer-balances?limit=101", headers=headers
            )
        ).status_code == 422
        assert (
            await http.get(
                "/finance/customers/AST-001/payments?date_from=tomorrow",
                headers=headers,
            )
        ).status_code == 422
    await app.state.reporting.close()


@pytest.mark.asyncio
async def test_exact_precision_and_signed_payment_date_filter():
    service = reporting(
        [
            row(1, "75000.123456789123456789", "25000.000000000000000001"),
            row(2, "-100.50", "0", kind="Payment", trans="2026-10-20"),
            row(3, "25.25", "0", kind="Payment", trans="2026-10-20"),
        ]
    )
    await load(service)
    result, _ = await service.report("balance", account="AST-001")
    assert result["balances"][0]["net_balance"] == "49924.873456789123456788"
    rows, meta = await service.report(
        "payments", account="AST-001", date_from="2026-10-20", date_to="2026-10-20"
    )
    assert (
        len(rows) == 2
        and meta["payment_period_totals"][0]["received_amount"] == "75.25"
    )
