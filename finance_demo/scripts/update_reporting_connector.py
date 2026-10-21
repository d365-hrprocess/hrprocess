"""Deterministically extend the baseline Swagger 2.0; no secret or new infrastructure."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
path = ROOT / "connector/swagger2.json"
spec = json.loads(path.read_text())
spec["info"]["version"] = "1.1.0"
spec["info"]["description"] = (
    "USMF customer demo: existing customer CRUD and limited recent history, plus separate complete-data posted receivables, invoice remaining amounts, overdue reports . Writes only to TEST-COPILOT- accounts with confirmation. No posting or settlement."
)


def ref(name):
    return {"$ref": "#/definitions/" + name}


def text(nullable=False):
    return {"type": "string", **({"x-nullable": True} if nullable else {})}


def arr(name):
    return {"type": "array", "items": ref(name)}


def obj(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        **({"required": list(required)} if required else {}),
    }


def string_array():
    return {"type": "array", "items": {"type": "string"}}


d = spec["definitions"]
amounts = (
    "net_balance",
    "debit_open_total",
    "credit_open_total",
    "open_invoice_total",
    "overdue_total",
    "unknown_due_date_amount",
    "unclassified_positive_open_amount",
    "unknown_transaction_date_amount",
)
counts = (
    "open_item_count",
    "overdue_invoice_count",
    "unknown_due_date_count",
    "unclassified_positive_open_count",
    "unknown_transaction_date_count",
    "customer_count",
)
d["FinancialSummary"] = obj(
    {
        **{k: text() for k in ("company", "customer_account", "currency", *amounts)},
        "organization_name": text(True),
        "oldest_due_date": text(True),
        "invoice_classification_complete": {"type": "boolean"},
        **{k: {"type": "integer", "format": "int32"} for k in counts},
    },
    ("currency", "net_balance"),
)
d["PostedFinancialRecord"] = obj(
    {
        **{
            k: text(
                k
                in (
                    "transaction_date",
                    "due_date",
                    "organization_name",
                    "transaction_type",
                    "payment_date",
                )
            )
            for k in (
                "company",
                "customer_account",
                "organization_name",
                "currency",
                "invoice",
                "voucher",
                "transaction_date",
                "due_date",
                "original_amount",
                "settled_amount",
                "remaining_amount",
                "transaction_type",
                "classification",
                "status",
                "payment_date",
                "received_amount",
            )
        },
        "record_key": {"type": "object", "additionalProperties": {"type": "string"}},
        "classification_verified": {"type": "boolean"},
        "is_overdue": {"type": "boolean"},
        "days_overdue": {"type": "integer", "format": "int32"},
    },
    (
        "company",
        "customer_account",
        "currency",
        "original_amount",
        "settled_amount",
        "remaining_amount",
        "record_key",
    ),
)
d["PaymentPeriodTotal"] = obj(
    {
        "currency": text(),
        "received_amount": text(),
        "count": {"type": "integer", "format": "int32"},
    }
)
d["FinancialMetadata"] = obj(
    {
        **{
            k: text(
                k in ("customer_account", "currency_filter", "date_from", "date_to")
            )
            for k in (
                "company",
                "customer_account",
                "currency_filter",
                "source_entity",
                "fetch_started_at",
                "fetched_at",
                "basis",
                "consistency_note",
                "as_of_date",
                "business_timezone",
                "pagination_basis",
                "display_filter",
                "classification_note",
                "date_from",
                "date_to",
                "payment_classification_note",
            )
        },
        **{
            k: {"type": "integer", "format": "int32"}
            for k in (
                "source_record_count",
                "source_customer_count",
                "transaction_pages",
                "customer_pages",
                "cache_ttl_seconds",
                "limit",
                "offset",
                "total_rows",
                "returned",
            )
        },
        "next_offset": {"type": "integer", "format": "int32", "x-nullable": True},
        "cache_age_seconds": {"type": "number"},
        **{
            k: {"type": "boolean"}
            for k in (
                "complete",
                "snapshot_isolation",
                "invoice_classification_complete",
                "has_more",
            )
        },
        "source_key_fields": string_array(),
        "totals": arr("FinancialSummary"),
        "displayed_filter_totals": arr("FinancialSummary"),
        "payment_period_totals": arr("PaymentPeriodTotal"),
        "payments_with_unknown_date": arr("PaymentPeriodTotal"),
    }
)


d["UnclassifiedTransaction"] = obj(
    {
        "currency": text(),
        "count": {"type": "integer", "format": "int32"},
        "original_amount": text(),
    }
)
d["FinancialMetadata"]["properties"].update(
    {
        "overdue_classification_complete": {"type": "boolean"},
        "unclassified_transactions": arr("UnclassifiedTransaction"),
        "missing_customer_name_count": {"type": "integer", "format": "int32"},
    }
)


def envelope(name, data):
    d[name] = obj(
        {
            "success": {"type": "boolean"},
            "data": data,
            "meta": ref("FinancialMetadata"),
        },
        ("success", "data", "meta"),
    )


envelope(
    "CustomerBalanceResponse",
    obj(
        {"balances": arr("FinancialSummary"), "totals": arr("FinancialSummary")},
        ("balances", "totals"),
    ),
)
envelope("FinancialReportResponse", arr("FinancialSummary"))
envelope("PostedFinancialListResponse", arr("PostedFinancialRecord"))
envelope("PostedInvoiceResponse", ref("PostedFinancialRecord"))


def param(name, place="query", **kw):
    return {
        "name": name,
        "in": place,
        "required": place == "path",
        "type": "string",
        **kw,
    }


account = param(
    "account", "path", description="Exact USMF customer account, for example AST-001."
)
currency = param(
    "currency",
    pattern="^[A-Z]{3}$",
    description="Optional transaction currency. Omit to keep all currencies as separate totals.",
)
cutoff = param(
    "as_of_date",
    format="date",
    description="YYYY-MM-DD; defaults to business today in Asia/Kolkata. Past cutoff does not reconstruct historical settlements.",
)
page = [
    param(
        "limit",
        type="integer",
        format="int32",
        minimum=1,
        maximum=100,
        default=10,
        description="Displayed rows only; complete source calculations are not limited.",
    ),
    param(
        "offset", type="integer", format="int32", minimum=0, maximum=100000, default=0
    ),
]
base_errors = {
    k: v
    for k, v in spec["paths"]["/customers"]["get"]["responses"].items()
    if k != "200"
}
base_errors["502"] = {
    "description": "Invalid or unsafe upstream reporting data",
    "schema": ref("ErrorResponse"),
}


def operation(route, operation_id, summary, params, response, method="get"):
    spec["paths"].setdefault(route, {})[method] = {
        "operationId": operation_id,
        "summary": summary,
        "x-ms-summary": summary,
        "x-ms-visibility": "important",
        "description": "Read-only D365 operation. Complete scans prepare in the background: report_preparing is retryable; repeat the same request after five seconds. Never infer final totals from partial scans or recent-history records. Inspect completeness, currency, unknown classification counts and fetch window. "
        + summary,
        "parameters": params,
        "responses": {
            "200": {
                "description": "Verified result, with explicit scope and completeness",
                "schema": ref(response),
            },
            **base_errors,
        },
    }


operation(
    "/customers/{account}/balance",
    "GetCustomerBalance",
    "Read complete current customer balance",
    [account, currency],
    "CustomerBalanceResponse",
)
operation(
    "/customers/{account}/open-invoices",
    "GetCustomerOpenInvoices",
    "Read open posted invoice remaining amounts",
    [account, currency, *page],
    "PostedFinancialListResponse",
)
operation(
    "/customers/{account}/overdue-invoices",
    "GetCustomerOverdueInvoices",
    "Read overdue customer invoices",
    [account, currency, cutoff, *page],
    "PostedFinancialListResponse",
)
operation(
    "/customers/{account}/invoices/{invoice_number}",
    "GetInvoiceRemainingAmount",
    "Read exact posted invoice remaining amount",
    [account, param("invoice_number", "path", maxLength=100), currency],
    "PostedInvoiceResponse",
)
operation(
    "/customers/{account}/payments",
    "GetCustomerPaymentHistory",
    "Read complete-source posted payment history",
    [
        account,
        currency,
        param("date_from", format="date"),
        param("date_to", format="date"),
        *page,
    ],
    "PostedFinancialListResponse",
)
operation(
    "/reports/customer-balances",
    "ListCustomerBalances",
    "List USMF customer outstanding balances",
    [currency, *page],
    "FinancialReportResponse",
)
operation(
    "/reports/overdue-customers",
    "ListOverdueCustomers",
    "List overdue customers across USMF",
    [currency, cutoff, *page],
    "FinancialReportResponse",
)
path.write_text(json.dumps(spec, indent=2) + "\n")
print(f"Wrote {path.name}: {path.stat().st_size} bytes, 13 business operations")
