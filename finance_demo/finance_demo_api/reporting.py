"""Complete, bounded posted-receivables scans; all accounting arithmetic stays here."""

from __future__ import annotations

import asyncio
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
import re
import time
from zoneinfo import ZoneInfo

from .business import FinanceBusiness, validate_account
from .errors import FinanceError
from .metadata import utcnow

INVOICE_TYPES = frozenset(
    {
        "Invoice",
        "CustInvoice",
        "CustomerInvoice",
        "FreeTextInvoice",
        "Sales",
        "Cust",
        "Project",
    }
)
PAYMENT_TYPES = frozenset({"Payment", "CustPayment", "CustomerPayment"})
BASIS = "Current posted ERP AmountCur minus SettleAmountCur, in transaction currency. A past cutoff changes date eligibility only; historical settlement balances are not reconstructed."


def currency_code(value):
    if value is not None and (
        not isinstance(value, str) or not re.fullmatch(r"[A-Z]{3}", value)
    ):
        raise FinanceError(
            "invalid_currency", "Use a three-letter uppercase currency code.", 422
        )
    return value


def iso_date(value):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise FinanceError("invalid_date", "Dates must use YYYY-MM-DD.", 422)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise FinanceError(
            "invalid_date", "The supplied date is invalid.", 422
        ) from None


def source_date(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise FinanceError(
            "invalid_source_date",
            "D365 returned an invalid transaction or due date.",
            502,
        )
    try:
        if len(value) == 10:
            parsed = date.fromisoformat(value)
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        return None if parsed.year <= 1900 else parsed
    except ValueError:
        raise FinanceError(
            "invalid_source_date",
            "D365 returned an invalid transaction or due date.",
            502,
        ) from None


def money(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (str, int, Decimal))
        or value == ""
    ):
        raise FinanceError(
            "invalid_source_amount",
            "An original or settled amount is missing or invalid; no financial total is available.",
            502,
        )
    try:
        result = Decimal(value)
        if (
            not result.is_finite()
            or len(result.as_tuple().digits) > 38
            or abs(result.as_tuple().exponent) > 28
            or result.adjusted() > 37
        ):
            raise InvalidOperation
        return result
    except (InvalidOperation, ValueError):
        raise FinanceError(
            "invalid_source_amount",
            "An original or settled amount is missing or invalid; no financial total is available.",
            502,
        ) from None


def exact(value):
    return format(value, "f")


def financial_schema(schema):
    """This strategy is specific to the verified CustTrans BI monetary contract.

    Names alone do not establish access: require_transactions already requires a
    live entity probe. ERP reconciliation remains a separate operational check.
    """
    required = {
        "company": "dataAreaId",
        "account": "AccountNum",
        "invoice": "Invoice",
        "voucher": "Voucher",
        "transaction_date": "TransDate",
        "due_date": "DueDate",
        "currency": "CurrencyCode",
        "amount": "AmountCur",
        "settled": "SettleAmountCur",
        "transaction_type": "TransType",
    }
    for label, name in required.items():
        kind = schema.properties.get(name)
        expected = (
            {"Edm.Decimal"}
            if label in ("amount", "settled")
            else (
                {"Edm.Date", "Edm.DateTimeOffset"}
                if label in ("transaction_date", "due_date")
                else {"Edm.String"}
            )
        )
        if label == "transaction_type":
            if (
                kind != "Microsoft.Dynamics.DataEntities.LedgerTransType"
                or not schema.enum_members.get(name)
            ):
                raise FinanceError(
                    "financial_enum_unverified",
                    "Posted financial reporting requires the metadata-defined LedgerTransType enum. Recent history remains available.",
                    503,
                )
        elif kind not in expected:
            raise FinanceError(
                "financial_fields_unverified",
                f"Posted financial reporting requires the verified {name} property and type. Existing customer/history tools remain available.",
                503,
            )
    for key in schema.keys:
        if schema.properties[key] not in {
            "Edm.String",
            "Edm.Int64",
            "Edm.Int32",
            "Edm.Guid",
        }:
            raise FinanceError(
                "financial_keys_unverified",
                "The posted transaction record keys are not supported.",
                503,
            )
    members = frozenset(schema.enum_members["TransType"])
    if not members & PAYMENT_TYPES or not members & INVOICE_TYPES:
        raise FinanceError(
            "financial_enum_unverified",
            "Invoice and payment enum members could not both be verified.",
            503,
        )
    return required, members


@dataclass
class Snapshot:
    records: list[dict]
    names: dict[str, str | None]
    evidence: dict
    completed: float


class FinanceReporting:
    def __init__(self, settings, client, metadata):
        self.settings, self.client, self.metadata = settings, client, metadata
        self.business = FinanceBusiness(settings, client, metadata)
        self.entries = OrderedDict()
        self.semaphore = asyncio.Semaphore(1)

    async def close(self):
        tasks = [
            e["task"]
            for e in self.entries.values()
            if e.get("task") and not e["task"].done()
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.entries.clear()

    def status(self):
        try:
            schema = self.metadata.require_transactions()
            fields, members = financial_schema(schema)
            capability = {
                "state": "schema_verified",
                "fields": fields,
                "record_keys": list(schema.keys),
                "invoice_types": sorted(members & INVOICE_TYPES),
                "payment_types": sorted(members & PAYMENT_TYPES),
                "strategy": "AmountCur - SettleAmountCur",
                "erp_reconciliation": "requires_live_comparison",
            }
        except FinanceError as error:
            capability = {"state": "unavailable", "error": error.as_dict()}
        capability["scans"] = [
            {
                "account": key[0],
                "currency": key[1],
                "state": entry["state"],
                "error": entry.get("error").as_dict() if entry.get("error") else None,
                "cache_age_seconds": round(
                    time.monotonic() - entry["snapshot"].completed, 1
                )
                if entry.get("snapshot")
                else None,
            }
            for key, entry in self.entries.items()
        ]
        return capability

    async def snapshot(self, account=None, currency=None):
        if account is not None:
            validate_account(account)
        currency_code(currency)
        financial_schema(self.metadata.require_transactions())
        key, now = (account, currency), time.monotonic()
        entry = self.entries.get(key)
        if entry:
            self.entries.move_to_end(key)
            if (
                entry["state"] == "ready"
                and now - entry["snapshot"].completed
                < self.settings.reporting_cache_ttl_seconds
            ):
                return entry["snapshot"]
            if entry["state"] == "preparing":
                raise FinanceError(
                    "report_preparing",
                    "The complete D365 scan is preparing. Retry the SAME operation in five seconds; no final totals are available yet.",
                    503,
                    True,
                )
            if (
                entry["state"] == "error"
                and now - entry["finished"]
                < self.settings.reporting_retry_after_seconds
            ):
                raise entry["error"]
        else:
            if len(self.entries) >= self.settings.reporting_maximum_cached_scopes:
                idle = next(
                    (k for k, e in self.entries.items() if e["state"] != "preparing"),
                    None,
                )
                if idle is None:
                    raise FinanceError(
                        "report_busy",
                        "Other complete reporting scans are preparing. Retry shortly.",
                        503,
                        True,
                    )
                del self.entries[idle]
        entry = {"state": "preparing", "snapshot": None, "error": None}
        self.entries[key] = entry
        entry["task"] = asyncio.create_task(
            self._load(entry, account, currency), name="finance-complete-scan"
        )
        raise FinanceError(
            "report_preparing",
            "A complete D365 scan has started. Retry the SAME operation in five seconds; partial records are never published as totals.",
            503,
            True,
        )

    async def _load(self, entry, account, currency):
        try:
            # Includes queue wait; a normal connector request never awaits this scan.
            async with asyncio.timeout(self.settings.reporting_scan_timeout_seconds):
                async with self.semaphore:
                    value = await self._scan(account, currency)
            entry.update(state="ready", snapshot=value, finished=time.monotonic())
        except asyncio.CancelledError:
            raise
        except FinanceError as error:
            entry.update(
                state="error", error=error, snapshot=None, finished=time.monotonic()
            )
        except TimeoutError:
            entry.update(
                state="error",
                snapshot=None,
                finished=time.monotonic(),
                error=FinanceError(
                    "report_scan_timeout",
                    "The complete scan exceeded its budget. No financial totals were published.",
                    503,
                    True,
                ),
            )
        except Exception:
            entry.update(
                state="error",
                snapshot=None,
                finished=time.monotonic(),
                error=FinanceError(
                    "report_scan_failed",
                    "The complete reporting scan failed. No partial totals were published.",
                    503,
                    True,
                ),
            )

    async def _pages(self, schema, params, *, account=None, currency=None):
        seen_links, seen_keys, result = set(), set(), []
        link = None
        for _ in range(self.settings.reporting_maximum_pages):
            payload = await self.client.reporting_page(schema.entity_set, params, link)
            rows = self.business._rows(payload)
            for row in rows:
                self.business._check_scope(schema, row, account)
                if currency and row.get("CurrencyCode") != currency:
                    raise FinanceError(
                        "d365_scope_mismatch",
                        "D365 returned a record outside the requested currency.",
                        502,
                    )
                key = []
                for name in schema.keys:
                    value = row.get(name)
                    kind = schema.properties[name]
                    if (
                        value is None
                        or isinstance(value, bool)
                        or not isinstance(value, (str, int))
                        or str(value) == ""
                    ):
                        raise FinanceError(
                            "invalid_reporting_key",
                            "D365 returned a missing or invalid exact record key.",
                            502,
                        )
                    if kind in {"Edm.Int64", "Edm.Int32"}:
                        if not re.fullmatch(r"-?\d+", str(value)):
                            raise FinanceError(
                                "invalid_reporting_key",
                                "D365 returned an invalid numeric record key.",
                                502,
                            )
                        value = str(int(value))
                    key.append(str(value))
                key = tuple(key)
                if key in seen_keys:
                    raise FinanceError(
                        "duplicate_reporting_key",
                        "The reporting scan repeated an exact record key; no totals were published.",
                        502,
                    )
                seen_keys.add(key)
                result.append(row)
                if len(result) > self.settings.reporting_maximum_records:
                    raise FinanceError(
                        "report_record_limit",
                        "The full source exceeds the configured record budget; no total is available.",
                        503,
                    )
            link = payload.get("@odata.nextLink")
            if link is None:
                return result, len(seen_links) + 1
            if not isinstance(link, str) or not link or link in seen_links:
                raise FinanceError(
                    "invalid_pagination",
                    "D365 pagination was malformed or repeated; no totals were published.",
                    502,
                )
            seen_links.add(link)
        raise FinanceError(
            "report_page_limit",
            "The full source exceeds the configured page budget; no total is available.",
            503,
        )

    async def _scan(self, account, currency):
        started = utcnow()
        schema = self.metadata.require_transactions()
        fields, members = financial_schema(schema)
        customers = self.metadata.require_customers()
        params = {
            "cross-company": "true",
            "$filter": self.business._filter(schema, account),
            "$select": ",".join(dict.fromkeys([*fields.values(), *schema.keys])),
            "$orderby": ",".join(f"{k} asc" for k in schema.keys),
        }
        if currency:
            params["$filter"] += f" and CurrencyCode eq '{currency}'"
        # One full transaction scan, never an N+1 query per customer.
        if account is not None:
            customer = await self.business.get_customer(account)
            names, customer_pages = {account: customer["organization_name"]}, 1
        else:
            customer_rows, customer_pages = await self._pages(
                customers,
                {
                    "cross-company": "true",
                    "$filter": self.business._filter(customers),
                    "$select": ",".join(
                        dict.fromkeys(
                            [
                                *customers.keys,
                                customers.field("account"),
                                customers.field("company"),
                                customers.field("name"),
                            ]
                        )
                    ),
                    "$orderby": ",".join(f"{k} asc" for k in customers.keys),
                },
            )
            names = {
                row[customers.field("account")]: row.get(customers.field("name"))
                for row in customer_rows
            }
        raw, pages = await self._pages(
            schema, params, account=account, currency=currency
        )
        result = []
        with localcontext() as ctx:
            ctx.prec = 64
            for row in raw:
                account_value = row["AccountNum"]
                validate_account(account_value)
                code = row.get("CurrencyCode")
                currency_code(code)
                if code is None:
                    raise FinanceError(
                        "invalid_source_currency",
                        "A reporting transaction has no currency; no financial total is available.",
                        502,
                    )
                original, settled = (
                    money(row.get("AmountCur")),
                    money(row.get("SettleAmountCur")),
                )
                kind = row.get("TransType")
                # Do not interpret numeric enum values or unverified named members.
                if not isinstance(kind, str):
                    kind = None
                verified = kind in members
                classification = (
                    "payment"
                    if verified and kind in PAYMENT_TYPES
                    else (
                        "invoice"
                        if verified
                        and kind in INVOICE_TYPES
                        and isinstance(row.get("Invoice"), str)
                        and row["Invoice"].strip()
                        else "unclassified"
                    )
                )
                invoice, voucher = row.get("Invoice"), row.get("Voucher")
                if not isinstance(invoice, str) or not isinstance(voucher, str):
                    raise FinanceError(
                        "invalid_source_reference",
                        "D365 returned an invalid invoice or voucher reference.",
                        502,
                    )
                result.append(
                    {
                        "company": self.settings.company,
                        "customer_account": account_value,
                        "organization_name": names.get(account_value),
                        "currency": code,
                        "invoice": invoice,
                        "voucher": voucher,
                        "record_key": {k: str(row[k]) for k in schema.keys},
                        "transaction_date": source_date(row.get("TransDate")),
                        "due_date": source_date(row.get("DueDate")),
                        "original_amount": original,
                        "settled_amount": settled,
                        "remaining_amount": original - settled,
                        "transaction_type": kind,
                        "classification": classification,
                        "classification_verified": classification != "unclassified",
                    }
                )
        finished = utcnow()
        evidence = {
            "company": self.settings.company,
            "customer_account": account,
            "currency_filter": currency,
            "complete": True,
            "source_entity": schema.entity_set,
            "source_key_fields": list(schema.keys),
            "source_record_count": len(result),
            "source_customer_count": len(names),
            "transaction_pages": pages,
            "customer_pages": customer_pages,
            "fetch_started_at": started,
            "fetched_at": finished,
            "basis": BASIS,
            "snapshot_isolation": False,
            "consistency_note": "All pages read within the fetch window; OData does not guarantee an ERP-wide point-in-time snapshot.",
            "cache_ttl_seconds": self.settings.reporting_cache_ttl_seconds,
        }
        return Snapshot(result, names, evidence, time.monotonic())

    def cutoff(self, value):
        return (
            iso_date(value)
            or datetime.now(ZoneInfo(self.settings.business_timezone)).date()
        )

    @staticmethod
    def serialize(row):
        return {
            k: exact(v)
            if isinstance(v, Decimal)
            else v.isoformat()
            if isinstance(v, date)
            else v
            for k, v in row.items()
        }

    def paginate(self, rows, limit, offset):
        if (
            not 1 <= limit <= self.settings.reporting_maximum_output_limit
            or not 0 <= offset <= self.settings.reporting_maximum_offset
        ):
            raise FinanceError(
                "invalid_report_pagination",
                "The output limit or offset is outside the supported bounds.",
                422,
            )
        return rows[offset : offset + limit], {
            "limit": limit,
            "offset": offset,
            "total_rows": len(rows),
            "returned": len(rows[offset : offset + limit]),
            "has_more": offset + limit < len(rows),
            "next_offset": offset + limit if offset + limit < len(rows) else None,
            "pagination_basis": "Output rows only; underlying calculations use every source page.",
        }

    def summaries(self, snapshot, cutoff):
        grouped = {}
        with localcontext() as ctx:
            ctx.prec = 64
            for row in snapshot.records:
                key = row["customer_account"], row["currency"]
                if key not in grouped:
                    grouped[key] = {
                        "company": self.settings.company,
                        "customer_account": key[0],
                        "organization_name": snapshot.names.get(key[0]),
                        "currency": key[1],
                        **{
                            k: Decimal(0)
                            for k in (
                                "net_balance",
                                "debit_open_total",
                                "credit_open_total",
                                "open_invoice_total",
                                "overdue_total",
                                "unknown_due_date_amount",
                                "unclassified_positive_open_amount",
                                "unknown_transaction_date_amount",
                            )
                        },
                        "open_item_count": 0,
                        "overdue_invoice_count": 0,
                        "unknown_due_date_count": 0,
                        "unclassified_positive_open_count": 0,
                        "unknown_transaction_date_count": 0,
                        "oldest_due_date": None,
                        "invoice_classification_complete": True,
                    }
                item = grouped[key]
                remain = row["remaining_amount"]
                if not remain:
                    continue
                item["open_item_count"] += 1
                item["net_balance"] += remain
                item["debit_open_total" if remain > 0 else "credit_open_total"] += (
                    remain
                )
                if remain > 0 and row["classification"] == "unclassified":
                    item["unclassified_positive_open_count"] += 1
                    item["unclassified_positive_open_amount"] += remain
                    item["invoice_classification_complete"] = False
                if remain > 0 and row["classification"] == "invoice":
                    item["open_invoice_total"] += remain
                    if row["due_date"] is None:
                        item["unknown_due_date_count"] += 1
                        item["unknown_due_date_amount"] += remain
                    if row["transaction_date"] is None:
                        item["unknown_transaction_date_count"] += 1
                        item["unknown_transaction_date_amount"] += remain
                    if self.is_overdue(row, cutoff):
                        item["overdue_total"] += remain
                        item["overdue_invoice_count"] += 1
                        oldest = item["oldest_due_date"]
                        item["oldest_due_date"] = (
                            min(oldest, row["due_date"]) if oldest else row["due_date"]
                        )
        return [self.serialize(row) for _, row in sorted(grouped.items())]

    @staticmethod
    def is_overdue(row, cutoff):
        return (
            row["classification"] == "invoice"
            and row["remaining_amount"] > 0
            and row["due_date"] is not None
            and row["due_date"] < cutoff
            and (row["transaction_date"] is None or row["transaction_date"] <= cutoff)
        )

    async def report(
        self,
        metric,
        *,
        account=None,
        currency=None,
        as_of_date=None,
        date_from=None,
        date_to=None,
        invoice_number=None,
        limit=10,
        offset=0,
        _full_output=False,
    ):
        cutoff = self.cutoff(as_of_date)
        start, end = iso_date(date_from), iso_date(date_to)
        if start and end and start > end:
            raise FinanceError(
                "invalid_date_range", "date_from must be on or before date_to.", 422
            )
        if invoice_number is not None and (
            not isinstance(invoice_number, str)
            or not 1 <= len(invoice_number) <= 100
            or not invoice_number.isprintable()
        ):
            raise FinanceError(
                "invalid_invoice_number",
                "Enter a printable exact posted invoice number.",
                422,
            )
        snapshot = await self.snapshot(account, currency)
        summaries = self.summaries(snapshot, cutoff)
        totals = self.currency_totals(summaries)
        meta = {
            **snapshot.evidence,
            "cache_age_seconds": round(time.monotonic() - snapshot.completed, 1),
            "as_of_date": cutoff.isoformat(),
            "business_timezone": self.settings.business_timezone,
            "invoice_classification_complete": all(
                r["invoice_classification_complete"] for r in summaries
            ),
            "totals": totals,
        }
        meta["overdue_classification_complete"] = meta[
            "invoice_classification_complete"
        ] and all(r["unknown_due_date_count"] == 0 for r in summaries)
        unknown = defaultdict(lambda: {"count": 0, "original_amount": Decimal(0)})
        with localcontext() as ctx:
            ctx.prec = 64
            for row in snapshot.records:
                if row["classification"] == "unclassified":
                    unknown[row["currency"]]["count"] += 1
                    unknown[row["currency"]]["original_amount"] += row[
                        "original_amount"
                    ]
        meta["unclassified_transactions"] = [
            {
                "currency": key,
                "count": value["count"],
                "original_amount": exact(value["original_amount"]),
            }
            for key, value in sorted(unknown.items())
        ]
        meta["missing_customer_name_count"] = len(
            {
                r["customer_account"]
                for r in snapshot.records
                if r["organization_name"] is None
            }
        )
        if metric == "balance":
            return {"balances": summaries, "totals": totals}, meta
        if metric in ("customer_balances", "overdue_customers"):
            rows = [
                r
                for r in summaries
                if Decimal(
                    r["net_balance"]
                    if metric == "customer_balances"
                    else r["overdue_total"]
                )
                > 0
            ]
            meta["display_filter"] = (
                "positive_net_balances_only"
                if metric == "customer_balances"
                else "verified_overdue_invoices_only"
            )
            meta["displayed_filter_totals"] = self.currency_totals(rows)
            meta["classification_note"] = (
                "Invoice/overdue figures cover verified invoice types only; inspect unknown counts and amounts."
            )
        elif metric == "payments":
            rows = []
            unknown_dates = defaultdict(
                lambda: {"count": 0, "received_amount": Decimal(0)}
            )
            with localcontext() as ctx:
                ctx.prec = 64
                for row in snapshot.records:
                    if row["classification"] != "payment":
                        continue
                    payment_date = row["transaction_date"]
                    if payment_date is None:
                        unknown_dates[row["currency"]]["count"] += 1
                        unknown_dates[row["currency"]]["received_amount"] += -row[
                            "original_amount"
                        ]
                        if start or end:
                            continue
                    if payment_date and (
                        (start and payment_date < start) or (end and payment_date > end)
                    ):
                        continue
                    rows.append(
                        self.serialize(
                            {
                                **row,
                                "payment_date": payment_date,
                                "received_amount": -row["original_amount"],
                            }
                        )
                    )
            rows.sort(
                key=lambda r: (
                    r["payment_date"] or "",
                    r["currency"],
                    r["voucher"],
                    str(r["record_key"]),
                )
            )
            with localcontext() as ctx:
                ctx.prec = 64
                amounts = defaultdict(Decimal)
                for row in rows:
                    amounts[row["currency"]] += Decimal(row["received_amount"])
            meta.update(
                date_from=date_from,
                date_to=date_to,
                payment_period_totals=[
                    {"currency": k, "received_amount": exact(v)}
                    for k, v in sorted(amounts.items())
                ],
                payments_with_unknown_date=[
                    {
                        "currency": k,
                        "count": v["count"],
                        "received_amount": exact(v["received_amount"]),
                    }
                    for k, v in sorted(unknown_dates.items())
                ],
                payment_classification_note="Only named metadata-verified payment types; unknown types are not guessed from signs.",
            )
        else:
            rows = []
            for row in snapshot.records:
                if row["classification"] != "invoice":
                    continue
                if metric == "open_invoices" and row["remaining_amount"] <= 0:
                    continue
                if metric == "overdue_invoices" and not self.is_overdue(row, cutoff):
                    continue
                if metric == "invoice" and row["invoice"] != invoice_number:
                    continue
                value = self.serialize(row)
                value.update(
                    status="settled" if row["remaining_amount"] == 0 else "open",
                    is_overdue=self.is_overdue(row, cutoff),
                    days_overdue=(cutoff - row["due_date"]).days
                    if self.is_overdue(row, cutoff)
                    else 0,
                )
                rows.append(value)
            rows.sort(
                key=lambda r: (
                    r["currency"],
                    r["invoice"],
                    r["voucher"],
                    str(r["record_key"]),
                )
            )
            if metric == "invoice":
                if len(rows) > 1:
                    raise FinanceError(
                        "invoice_ambiguous",
                        "Multiple posted invoice records match. Use the open-invoices tool to inspect currency, voucher and exact record keys; do not select the first record.",
                        409,
                    )
                if not rows:
                    raise FinanceError(
                        "posted_invoice_not_found",
                        "No verified posted invoice matches this account, invoice and currency. This does not establish a draft invoice balance.",
                        404,
                    )
                return rows[0], meta
        if _full_output:
            return rows, {**meta, "total_rows": len(rows)}
        page, pagination = self.paginate(rows, limit, offset)
        return page, {**meta, **pagination}

    @staticmethod
    def currency_totals(rows):
        sums = {}
        amount_fields = (
            "net_balance",
            "debit_open_total",
            "credit_open_total",
            "open_invoice_total",
            "overdue_total",
            "unknown_due_date_amount",
            "unclassified_positive_open_amount",
            "unknown_transaction_date_amount",
        )
        count_fields = (
            "open_item_count",
            "overdue_invoice_count",
            "unknown_due_date_count",
            "unclassified_positive_open_count",
            "unknown_transaction_date_count",
        )
        with localcontext() as ctx:
            ctx.prec = 64
            for row in rows:
                value = sums.setdefault(
                    row["currency"],
                    {
                        **{k: Decimal(0) for k in amount_fields},
                        **{k: 0 for k in count_fields},
                        "customer_count": 0,
                    },
                )
                value["customer_count"] += 1
                for field in amount_fields:
                    value[field] += Decimal(row[field])
                for field in count_fields:
                    value[field] += row[field]
        return [
            {
                "currency": code,
                **{
                    k: exact(v) if isinstance(v, Decimal) else v
                    for k, v in values.items()
                },
            }
            for code, values in sorted(sums.items())
        ]
