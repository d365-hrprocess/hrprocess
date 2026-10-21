"""Fixed-company, metadata-mapped customer operations; never retry writes."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
from typing import Any

from .errors import FinanceError

ACCOUNT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._'-]{0,39}$"


def validate_account(account: str) -> str:
    if not isinstance(account, str) or not re.fullmatch(ACCOUNT_PATTERN, account):
        raise FinanceError("invalid_account", "Use a customer account of 1–40 letters, digits, dots, hyphens, underscores or apostrophes.", status_code=422)
    return account


def literal(value: str) -> str:
    """OData string literal escaping, not URL construction from user inputs."""
    return "'" + value.replace("'", "''") + "'"


class FinanceBusiness:
    def __init__(self, settings, client, metadata):
        self.settings = settings
        self.client = client
        self.metadata = metadata

    def _filter(self, schema, account: str | None = None) -> str:
        result = f"{schema.field('company')} eq {literal(self.settings.company)}"
        if account is not None:
            result += f" and {schema.field('account')} eq {literal(account)}"
        return result

    def _select(self, schema) -> str:
        return ",".join(dict.fromkeys(schema.fields.values()))

    def _rows(self, payload: Any) -> list[dict]:
        if not isinstance(payload, dict) or not isinstance(payload.get("value"), list):
            raise FinanceError("invalid_d365_response", "D365 returned an unexpected response shape.", status_code=502)
        if not all(isinstance(row, dict) for row in payload["value"]):
            raise FinanceError("invalid_d365_response", "D365 returned an unexpected record shape.", status_code=502)
        return payload["value"]

    def _check_scope(self, schema, row: dict, account: str | None = None):
        company = row.get(schema.field("company"))
        actual_account = row.get(schema.field("account"))
        if not isinstance(company, str) or company.casefold() != self.settings.company.casefold():
            raise FinanceError("d365_scope_mismatch", "D365 returned a record outside the configured company.", status_code=502)
        if not isinstance(actual_account, str) or (account is not None and actual_account != account):
            raise FinanceError("d365_scope_mismatch", "D365 returned an unexpected customer account.", status_code=502)

    async def _find(self, schema, account: str) -> dict | None:
        try:
            payload = await self.client.json_request("GET", schema.entity_set, params={
                "cross-company": "true", "$filter": self._filter(schema, account),
                "$top": "2", "$select": self._select(schema),
            })
        except FinanceError as error:
            # A 404 on the entity-set endpoint does not prove customer absence.
            if error.status_code == 404:
                raise FinanceError("customer_entity_unavailable", "The customer entity cannot currently be read.", status_code=503) from None
            raise
        rows = self._rows(payload)
        if len(rows) > 1:
            raise FinanceError("ambiguous_customer", "The account lookup returned more than one record; no mutation was performed.", status_code=409)
        if not rows:
            return None
        self._check_scope(schema, rows[0], account)
        return rows[0]

    def _customer(self, schema, row: dict) -> dict:
        def get(key):
            field = schema.fields.get(key)
            return row.get(field) if field else None
        return {
            "customer_account": get("account"), "organization_name": get("name"),
            "company": get("company"), "currency": get("currency"),
            "customer_group": get("group"), "payment_terms": get("payment_terms"),
        }

    async def list_customers(self, limit: int) -> tuple[list[dict], dict]:
        schema = self.metadata.require_customers()
        payload = await self.client.json_request("GET", schema.entity_set, params={
            "cross-company": "true", "$filter": self._filter(schema), "$top": str(limit),
            "$select": self._select(schema), "$orderby": f"{schema.field('account')} asc",
        })
        rows = self._rows(payload)
        if len(rows) > limit:
            raise FinanceError("invalid_d365_response", "D365 exceeded the requested record limit.", status_code=502)
        for row in rows:
            self._check_scope(schema, row)
        return [self._customer(schema, row) for row in rows], {
            "company": self.settings.company, "limited": True, "limit": limit, "returned": len(rows),
        }

    async def get_customer(self, account: str) -> dict:
        validate_account(account)
        schema = self.metadata.require_customers()
        row = await self._find(schema, account)
        if row is None:
            raise FinanceError("customer_not_found", "The customer account was not found in the configured company.", status_code=404)
        return self._customer(schema, row)

    async def transactions(self, account: str, limit: int) -> tuple[list[dict], dict]:
        validate_account(account)
        # Explicit account identity must exist; an empty transaction set is not a missing customer.
        await self.get_customer(account)
        schema = self.metadata.require_transactions()
        payload = await self.client.json_request("GET", schema.entity_set, params={
            "cross-company": "true", "$filter": self._filter(schema, account), "$top": str(limit),
            "$select": self._select(schema), "$orderby": f"{schema.field('transaction_date')} desc",
        })
        rows = self._rows(payload)
        if len(rows) > limit:
            raise FinanceError("invalid_d365_response", "D365 exceeded the requested record limit.", status_code=502)
        result = []
        for row in rows:
            self._check_scope(schema, row, account)
            normalized = {key: row.get(field) for key, field in schema.fields.items() if key not in {"account", "company"}}
            normalized.update(customer_account=row[schema.field("account")], company=row[schema.field("company")])
            transaction_type = normalized.get("transaction_type")
            if isinstance(transaction_type, (int, Decimal)) and not isinstance(transaction_type, bool):
                normalized["transaction_type"] = str(transaction_type)
            amount = normalized.get("amount")
            if amount is not None:
                try:
                    value = Decimal(str(amount))
                    if not value.is_finite():
                        raise InvalidOperation
                    normalized["amount"] = format(value, "f")
                except (InvalidOperation, ValueError):
                    raise FinanceError("invalid_transaction_amount", "D365 returned an invalid transaction amount.", status_code=502) from None
            result.append(normalized)
        return result, {
            "company": self.settings.company, "limited": True, "limit": limit,
            "returned": len(result), "complete_balance": False,
        }

    def _allow_write(self, account: str):
        validate_account(account)
        if not account.startswith(self.settings.demo_customer_prefix):
            raise FinanceError("demo_account_required", f"Mutations are restricted to accounts starting with {self.settings.demo_customer_prefix}.", status_code=403)

    async def _write(self, method: str, path: str, *, json: dict | None = None):
        try:
            # There is exactly one write attempt. Subsequent requests only verify the record.
            await self.client.request(method, path, params={"cross-company": "true"}, json=json)
        except FinanceError as error:
            if error.status_code == 409:
                raise FinanceError("customer_conflict", "D365 rejected a conflicting customer write. Read the exact account before any further write.", status_code=409) from None
            if error.uncertain or error.status_code in {401, 403, 408, 429} or error.status_code >= 500:
                raise FinanceError("write_outcome_uncertain", "The write outcome could not be confirmed. Read the exact customer account before attempting another write.", status_code=503, uncertain=True) from None
            raise

    async def _verify(self, schema, account: str, *, expected: dict | None = None, deleted: bool = False):
        try:
            row = await self._find(schema, account)
        except FinanceError:
            raise FinanceError("write_verification_failed", "D365 accepted a write request but its final state could not be verified. Read the exact account before another write.", status_code=503, uncertain=True) from None
        if deleted:
            if row is not None:
                raise FinanceError("write_verification_failed", "The customer is still present after the delete request. Do not report deletion or retry automatically.", status_code=503, uncertain=True)
            return None
        if row is None or any(row.get(field) != value for field, value in (expected or {}).items()):
            raise FinanceError("write_verification_failed", "The read-back does not confirm the requested values. Read the exact account before another write.", status_code=503, uncertain=True)
        return self._customer(schema, row)

    async def create_customer(self, account: str, name: str) -> dict:
        self._allow_write(account)
        schema = self.metadata.require_customers()
        # key_path verifies both the company and customer components before any mutation.
        schema.key_path(self.settings.company, account)
        if await self._find(schema, account) is not None:
            raise FinanceError("customer_exists", "This customer account already exists; no create request was sent.", status_code=409)
        normalized = {
            "account": account, "company": self.settings.company, "name": name,
            "currency": self.settings.currency, "group": self.settings.customer_group,
            "payment_terms": self.settings.payment_terms, "party_type": "Organization",
        }
        payload = {schema.field(k): v for k, v in normalized.items()}
        await self._write("POST", schema.entity_set, json=payload)
        return await self._verify(schema, account, expected={
            schema.field(k): v for k, v in normalized.items() if k != "party_type"
        })

    async def update_customer(self, account: str, name: str) -> dict:
        self._allow_write(account)
        schema = self.metadata.require_customers()
        if await self._find(schema, account) is None:
            raise FinanceError("customer_not_found", "The customer account was not found; no update request was sent.", status_code=404)
        payload = {schema.field("name"): name}
        await self._write("PATCH", schema.key_path(self.settings.company, account), json=payload)
        return await self._verify(schema, account, expected=payload)

    async def delete_customer(self, account: str) -> dict:
        self._allow_write(account)
        schema = self.metadata.require_customers()
        if await self._find(schema, account) is None:
            raise FinanceError("customer_not_found", "The customer account was not found; no delete request was sent.", status_code=404)
        await self._write("DELETE", schema.key_path(self.settings.company, account))
        await self._verify(schema, account, deleted=True)
        return {"customer_account": account, "company": self.settings.company, "deleted": True, "verified": True}
