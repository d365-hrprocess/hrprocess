"""Streamed, scoped D365 CSDL cache and independent capability readiness.

No metadata or ERP credential is required by the HR application's health check.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
import time

from defusedxml.ElementTree import iterparse
import httpx

from .d365 import D365Client
from .errors import FinanceError
from .settings import Settings

EDM = {"http://docs.oasis-open.org/odata/ns/edm", "http://schemas.microsoft.com/ado/2009/11/edm"}
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@dataclass(frozen=True)
class EntitySchema:
    entity_set: str
    type_name: str
    keys: tuple[str, ...]
    properties: dict[str, str]
    fields: dict[str, str]

    def field(self, normalized: str) -> str:
        try:
            return self.fields[normalized]
        except KeyError:
            raise FinanceError("metadata_field_unavailable", f"The required {normalized} field is unavailable.", 503) from None

    def key_path(self, company: str, account: str) -> str:
        company_field, account_field = self.field("company"), self.field("account")
        if set(self.keys) != {company_field, account_field} or any(self.properties[k] != "Edm.String" for k in self.keys):
            raise FinanceError("metadata_customer_key_invalid", "D365 customer composite keys are not verified.", 503)
        values = {company_field: company, account_field: account}
        return self.entity_set + "(" + ",".join(k + "=" + literal(values[k]) for k in self.keys) + ")"

    def to_dict(self) -> dict:
        result = asdict(self)
        result["keys"] = list(self.keys)
        return result

    @classmethod
    def from_dict(cls, raw: dict) -> "EntitySchema":
        if not isinstance(raw, dict) or not isinstance(raw.get("properties"), dict) or not isinstance(raw.get("fields"), dict):
            raise ValueError("invalid schema")
        result = cls(raw["entity_set"], raw["type_name"], tuple(raw["keys"]), raw["properties"], raw["fields"])
        if not NAME.fullmatch(result.entity_set) or not result.keys or not isinstance(result.type_name, str):
            raise ValueError("invalid entity")
        if not all(isinstance(k, str) and NAME.fullmatch(k) and isinstance(v, str) for k, v in result.properties.items()):
            raise ValueError("invalid properties")
        if not all(isinstance(k, str) and isinstance(v, str) and v in result.properties for k, v in result.fields.items()):
            raise ValueError("invalid field map")
        if not all(k in result.properties for k in result.keys):
            raise ValueError("invalid keys")
        return result


def _resolve_fields(properties: dict[str, str], choices: dict[str, tuple[str, ...]]) -> dict[str, str]:
    result = {}
    lower = {key.lower(): key for key in properties}
    for field, aliases in choices.items():
        for alias in aliases:
            if alias.lower() in lower:
                result[field] = lower[alias.lower()]
                break
        if field not in result:
            raise FinanceError("metadata_required_field_missing", f"The required {field} property is missing from D365 metadata.")
    return result


CUSTOMER_FIELDS = {
    "company": ("dataAreaId",), "account": ("CustomerAccount",),
    "name": ("OrganizationName", "CustomerName", "Name"),
    "currency": ("SalesCurrencyCode", "CurrencyCode"),
    "group": ("CustomerGroupId", "CustomerGroup"),
    "payment_terms": ("PaymentTerms", "PaymentTermsId"),
    "party_type": ("PartyType",),
}
TRANSACTION_FIELDS = {
    "company": ("dataAreaId",), "account": ("AccountNum",),
    "invoice": ("Invoice", "InvoiceId"), "voucher": ("Voucher",),
    "transaction_date": ("TransDate", "TransactionDate"), "due_date": ("DueDate",),
    "amount": ("AmountCur", "TransactionAmount", "Amount"),
    "transaction_type": ("TransType", "TransactionType"),
}


def parse_metadata(path: Path, settings: Settings) -> dict:
    """Parse namespace-aware CSDL with BaseType inheritance and bounded structure.

    Discard completed EntityTypes/EntitySets while parsing rather than retaining
    an entire 128 MiB XML tree. No network/entity expansion is permitted.
    """
    types, sets, aliases, enums = {}, {}, {}, {}
    current_schema = ""
    stack = []
    root_checked = False
    try:
        for event, element in iterparse(path, events=("start", "end"), forbid_dtd=True, forbid_entities=True, forbid_external=True):
            if event == "start":
                stack.append(element)
                if len(stack) > 128:
                    raise ValueError("metadata nesting")
                if not root_checked:
                    root_checked = True
                    if not element.tag.endswith("}Edmx"):
                        raise ValueError("not CSDL")
                if element.tag.rsplit("}", 1)[-1] == "Schema":
                    ns = element.tag[1:].split("}", 1)[0] if element.tag.startswith("{") else ""
                    if ns not in EDM:
                        raise ValueError("unrecognized EDM namespace")
                    current_schema = element.attrib.get("Namespace", "")
                    if not current_schema:
                        raise ValueError("missing schema namespace")
                    if element.attrib.get("Alias"):
                        aliases[element.attrib["Alias"]] = current_schema
                continue
            local = element.tag.rsplit("}", 1)[-1]
            namespace = element.tag[1:].split("}", 1)[0] if element.tag.startswith("{") else ""
            disposable = False
            if namespace in EDM and local == "EntityType":
                name = element.attrib.get("Name", "")
                if not NAME.fullmatch(name):
                    raise ValueError("invalid type name")
                properties = {}
                keys = []
                for child in element:
                    tag = child.tag.rsplit("}", 1)[-1]
                    if tag == "Property":
                        pname, ptype = child.attrib.get("Name", ""), child.attrib.get("Type", "")
                        if not NAME.fullmatch(pname) or not ptype:
                            raise ValueError("invalid property")
                        properties[pname] = ptype
                    elif tag == "Key":
                        keys = [x.attrib["Name"] for x in child if x.tag.rsplit("}", 1)[-1] == "PropertyRef"]
                types[current_schema + "." + name] = {"base": element.attrib.get("BaseType"), "properties": properties, "keys": keys}
                disposable = True
            elif namespace in EDM and local == "EntitySet":
                name, type_name = element.attrib.get("Name", ""), element.attrib.get("EntityType", "")
                if not NAME.fullmatch(name) or not type_name or name in sets:
                    raise ValueError("invalid entity set")
                sets[name] = type_name
                disposable = True
            elif namespace in EDM and local == "EnumType":
                enums[current_schema + "." + element.attrib.get("Name", "")] = [x.attrib.get("Name") for x in element if x.tag.rsplit("}", 1)[-1] == "Member"]
                disposable = True
            elif local in {"Action", "Function", "ComplexType", "Annotations", "EntityContainer", "Schema"}:
                disposable = True
            if len(types) > 100000 or len(sets) > 100000:
                raise ValueError("metadata complexity")
            if disposable:
                element.clear()
                if len(stack) >= 2:
                    stack[-2].remove(element)
            stack.pop()
    except FinanceError:
        raise
    except Exception:
        # XML parser exceptions can contain input fragments; never forward them.
        raise FinanceError("metadata_invalid_xml", "D365 metadata is malformed, truncated or unsafe.", 503, True) from None

    def qualified(name):
        namespace, dot, tail = name.rpartition(".")
        return aliases.get(namespace, namespace) + dot + tail

    def inherited(name, visited=None):
        name = qualified(name)
        visited = set() if visited is None else visited
        if name in visited or len(visited) > 50 or name not in types:
            raise FinanceError("metadata_invalid_inheritance", "D365 metadata has unresolved or cyclic entity inheritance.")
        visited.add(name)
        entry = types[name]
        props, keys = inherited(entry["base"], visited) if entry["base"] else ({}, [])
        props = {**props, **entry["properties"]}
        keys = entry["keys"] or keys
        if not keys or not all(k in props for k in keys):
            raise FinanceError("metadata_invalid_keys", "D365 entity keys are missing or unresolved.")
        return props, keys

    def entity(name, choices):
        if name not in sets:
            raise FinanceError("metadata_entity_missing", f"The configured {name} entity set is absent.")
        props, keys = inherited(sets[name])
        mapping = _resolve_fields(props, choices)
        return EntitySchema(name, qualified(sets[name]), tuple(keys), props, mapping)

    customers = entity(settings.customers_entity, CUSTOMER_FIELDS)
    for label, property_name in customers.fields.items():
        if label != "party_type" and customers.properties[property_name] != "Edm.String":
            raise FinanceError("metadata_customer_type_invalid", f"The customer {label} field is not a supported string property.")
    party_type = qualified(customers.properties[customers.field("party_type")])
    if party_type != "Edm.String" and "Organization" not in enums.get(party_type, []):
        raise FinanceError("metadata_party_type_invalid", "The customer party type does not support Organization.")
    # Fails closed before accepting any schema suitable for PATCH/DELETE.
    customers.key_path(settings.company, "TEST-COPILOT-SCHEMA")
    transaction_error = None
    transactions = None
    try:
        transactions = entity(settings.transactions_entity, TRANSACTION_FIELDS)
        for candidate in ("CurrencyCode", "Currency"):
            actual = next((k for k in transactions.properties if k.lower() == candidate.lower()), None)
            if actual and transactions.properties[actual] == "Edm.String":
                transactions.fields["currency"] = actual
                break
        if transactions.properties[transactions.field("account")] != "Edm.String" or transactions.properties[transactions.field("company")] != "Edm.String":
            raise FinanceError("metadata_transaction_type_invalid", "Transaction account/company fields have unsupported types.")
        if transactions.properties[transactions.field("amount")] not in {"Edm.Decimal", "Edm.Int64", "Edm.Int32"}:
            raise FinanceError("metadata_transaction_type_invalid", "The transaction amount field is not an exact numeric property.")
    except FinanceError as exc:
        transactions = None
        transaction_error = exc.as_dict()
    return {"entity_count": len(sets), "customers": customers.to_dict(),
            "transactions": transactions.to_dict() if transactions else None,
            "transaction_schema_error": transaction_error}


class MetadataManager:
    def __init__(self, settings: Settings, client: D365Client):
        self.settings, self.client = settings, client
        self._task = None
        self._refresh_lock = asyncio.Lock()
        self._customers = None
        self._transactions = None
        self._loaded = None
        self._downloaded_at = None
        self._loaded_at = None
        self._last_refresh_at = None
        self._last_error = None
        self._source = None
        self._state = "disabled" if not settings.enabled else "warming"
        self._customer_capability = {"state": "unverified", "error": None}
        self._transaction_capability = {"state": "unverified", "error": None}
        self.cache_directory = self._cache_directory()
        self.cache_path = self.cache_directory / (settings.cache_scope + ".json")

    def _cache_directory(self) -> Path:
        preferred = []
        if self.settings.prefer_persistent_cache and os.environ.get("WEBSITES_ENABLE_APP_SERVICE_STORAGE", "").lower() == "true":
            preferred.append(Path("/home/ubticareers-finance-metadata"))
        preferred.append(Path(self.settings.cache_directory))
        for path in preferred:
            try:
                path.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryFile(dir=path):
                    pass
                return path
            except OSError:
                continue
        # A missing metadata cache must not crash finance construction or HR.
        return Path(tempfile.gettempdir()) / "ubticareers-finance-metadata"

    def start(self):
        if self.settings.enabled and (not self._task or self._task.done()):
            self._task = asyncio.create_task(self._run(), name="finance-metadata-warmup")

    async def close(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self):
        while True:
            await self.refresh()
            # Retry failed warmup in the background, never by blocking a tool request.
            capabilities_unavailable = (
                self._customer_capability["state"] != "readable"
                or self._transaction_capability["state"] != "readable"
            )
            interval = 60 if self._last_error or capabilities_unavailable else max(60, self.settings.metadata_cache_ttl_hours * 3600)
            await asyncio.sleep(interval)

    def require_customers(self) -> EntitySchema:
        if self._customer_capability["state"] != "readable" or self._customers is None:
            error = self._customer_capability.get("error")
            raise FinanceError("finance_warming" if self._state == "warming" else "finance_customers_unavailable",
                               "Finance customer metadata/access is warming or unavailable; retry readiness shortly.",
                               503, self._state == "warming" or bool(error and error.get("retryable")))
        return self._customers

    def require_transactions(self) -> EntitySchema:
        self.require_customers()
        if self._transaction_capability["state"] != "readable" or self._transactions is None:
            raise FinanceError("finance_transactions_unavailable", "Recent transaction capability is unavailable or unverified; customer operations may still be available.", 503, False)
        return self._transactions

    def _age(self):
        if not self._downloaded_at:
            return None
        return max(0, time.time() - datetime.fromisoformat(self._downloaded_at).timestamp())

    def status(self) -> dict:
        age = self._age()
        return {
            "state": self._state,
            "ready": self._customer_capability["state"] == "readable",
            "cache_source": self._source, "downloaded_at": self._downloaded_at,
            "loaded_at": self._loaded_at, "last_refresh_at": self._last_refresh_at,
            "cache_age_seconds": round(age, 1) if age is not None else None,
            "stale": bool(age is not None and age >= self.settings.metadata_cache_ttl_hours * 3600),
            "refresh_error": self._last_error,
            "entity_count": self._loaded["entity_count"] if self._loaded else 0,
            "entities": {"customers": self._customers.to_dict() if self._customers else None,
                         "transactions": self._transactions.to_dict() if self._transactions else None},
            "capabilities": {"customers": self._customer_capability, "transactions": self._transaction_capability},
            "write_permissions": "unverified_until_live_demo_crud",
        }

    def _read_cache(self):
        try:
            if self.cache_path.stat().st_size > 16 * 1024 * 1024:
                return None
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if raw["version"] != self.settings.parser_config_version or raw["scope"] != self.settings.cache_scope:
                return None
            payload = raw["payload"]
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            if raw["sha256"] != sha256(encoded).hexdigest():
                return None
            date = datetime.fromisoformat(raw["downloaded_at"])
            if date.tzinfo is None or date.timestamp() > time.time() + 60:
                return None
            customers = EntitySchema.from_dict(payload["customers"])
            if customers.entity_set != self.settings.customers_entity:
                return None
            expected_fields = _resolve_fields(customers.properties, CUSTOMER_FIELDS)
            if expected_fields != customers.fields:
                return None
            if any(customers.properties[p] != "Edm.String" for label, p in expected_fields.items() if label != "party_type"):
                return None
            customers.key_path(self.settings.company, "TEST-COPILOT-CACHE")
            if payload.get("transactions"):
                transactions = EntitySchema.from_dict(payload["transactions"])
                if transactions.entity_set != self.settings.transactions_entity:
                    return None
                transaction_fields = _resolve_fields(transactions.properties, TRANSACTION_FIELDS)
                if any(transactions.fields.get(k) != v for k, v in transaction_fields.items()):
                    return None
                if transactions.properties[transaction_fields["amount"]] not in {"Edm.Decimal", "Edm.Int64", "Edm.Int32"}:
                    return None
                if any(transactions.properties[transaction_fields[key]] != "Edm.String" for key in ("account", "company")):
                    return None
            if not isinstance(payload["entity_count"], int) or payload["entity_count"] < 1:
                return None
            return raw
        except (OSError, ValueError, TypeError, KeyError, FinanceError):
            return None

    def _apply(self, raw, source):
        self._loaded = raw["payload"]
        self._customers = EntitySchema.from_dict(self._loaded["customers"])
        self._transactions = EntitySchema.from_dict(self._loaded["transactions"]) if self._loaded.get("transactions") else None
        self._downloaded_at = raw["downloaded_at"]
        self._loaded_at = utcnow()
        self._source = source

    def _write_cache(self, payload, downloaded_at):
        raw = {"version": self.settings.parser_config_version, "scope": self.settings.cache_scope,
               "downloaded_at": downloaded_at, "payload": payload,
               "sha256": sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
        self.cache_directory.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".metadata-", dir=self.cache_directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(raw, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.cache_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return raw

    @asynccontextmanager
    async def _file_lock(self):
        self.cache_directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.cache_directory / (self.settings.cache_scope + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(.2)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    async def _download(self) -> dict:
        attempts = self.settings.metadata_retry_attempts
        last_error = None
        for attempt in range(attempts):
            temporary = None
            try:
                token = await self.client.access_token()
                fd, name = tempfile.mkstemp(prefix="finance-csdl-", suffix=".xml", dir=self.cache_directory)
                temporary = Path(name)
                with os.fdopen(fd, "wb") as stream:
                    async with self.client.http.stream(
                        "GET", self.client.url("$metadata"),
                        headers={"Authorization": "Bearer " + token, "Accept": "application/xml"},
                        timeout=httpx.Timeout(self.settings.metadata_read_timeout_seconds,
                                              connect=self.settings.connect_timeout_seconds),
                    ) as response:
                        if response.status_code != 200:
                            raise self.client._error(response, write=False)
                        size = 0
                        async for block in response.aiter_bytes():
                            size += len(block)
                            if size > self.settings.metadata_maximum_bytes:
                                raise FinanceError("metadata_size_limit", "D365 metadata exceeds the configured size limit.", 503)
                            stream.write(block)
                        if not size:
                            raise FinanceError("metadata_empty", "D365 returned empty metadata.", 503, True)
                return await asyncio.to_thread(parse_metadata, temporary, self.settings)
            except FinanceError as exc:
                last_error = exc
                if not exc.retryable:
                    raise
            except (httpx.HTTPError, OSError):
                last_error = FinanceError("metadata_download_failed", "D365 metadata could not be downloaded.", 503, True)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            if attempt + 1 < attempts:
                await asyncio.sleep(self.settings.metadata_retry_backoff_seconds * (2 ** attempt))
        raise last_error or FinanceError("metadata_download_failed", "D365 metadata could not be downloaded.", 503, True)

    async def _probe(self, schema: EntitySchema):
        fields = list(dict.fromkeys(schema.fields.values()))
        payload = await self.client.json_request("GET", schema.entity_set,
            params={"cross-company": "true", "$filter": schema.field("company") + " eq " + literal(self.settings.company),
                    "$select": ",".join(fields), "$top": "1"})
        if not isinstance(payload, dict) or not isinstance(payload.get("value"), list):
            raise FinanceError("d365_probe_invalid", "D365 entity probe did not return an OData collection.", 502)
        for record in payload["value"]:
            if not isinstance(record, dict) or any(key not in record for key in fields) or str(record[schema.field("company")]).lower() != self.settings.company.lower():
                raise FinanceError("d365_probe_invalid", "D365 entity probe returned unexpected fields or company.", 502)

    async def _probe_capabilities(self):
        if not self._customers:
            return
        try:
            await self._probe(self._customers)
            self._customer_capability = {"state": "readable", "verified_at": utcnow(), "error": None}
        except FinanceError as exc:
            self._customer_capability = {"state": "unavailable", "error": exc.as_dict()}
        if self._transactions:
            try:
                await self._probe(self._transactions)
                self._transaction_capability = {"state": "readable", "verified_at": utcnow(), "error": None}
            except FinanceError as exc:
                self._transaction_capability = {"state": "unavailable", "error": exc.as_dict()}
        else:
            self._transaction_capability = {"state": "unavailable", "error": self._loaded.get("transaction_schema_error") if self._loaded else None}

    async def refresh(self):
        if not self.settings.enabled:
            self._state = "disabled"
            return
        async with self._refresh_lock:
            self._last_refresh_at = utcnow()
            self._last_error = None
            if self._customers is None:
                self._state = "warming"
            try:
                async with asyncio.timeout(self.settings.metadata_total_timeout_seconds):
                    async with self._file_lock():
                        cached = await asyncio.to_thread(self._read_cache)
                        if cached:
                            self._apply(cached, "cache")
                        age = self._age()
                        if not cached or age is None or age >= self.settings.metadata_cache_ttl_hours * 3600:
                            try:
                                payload = await self._download()
                                raw = await asyncio.to_thread(self._write_cache, payload, utcnow())
                                self._apply(raw, "download")
                            except FinanceError as exc:
                                self._last_error = exc.as_dict()
                                if not self._customers:
                                    raise
                # The metadata total budget is separate from short live GET probes.
                await self._probe_capabilities()
                self._state = "ready" if self._customer_capability["state"] == "readable" else "unavailable"
            except FinanceError as exc:
                self._last_error = exc.as_dict()
                self._state = "unavailable"
                if not self._customers:
                    self._customer_capability = {"state": "unavailable", "error": exc.as_dict()}
            except (TimeoutError, OSError):
                exc = FinanceError("metadata_warmup_timeout", "Finance metadata warmup could not complete within its budget.", 503, True)
                self._last_error = exc.as_dict()
                if self._customers:
                    await self._probe_capabilities()
                    self._state = "ready" if self._customer_capability["state"] == "readable" else "unavailable"
                else:
                    self._state = "unavailable"
                    self._customer_capability = {"state": "unavailable", "error": exc.as_dict()}
