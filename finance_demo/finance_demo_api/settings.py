"""Finance-only configuration; never loads or overrides the HR application's environment."""
from dataclasses import dataclass, fields
from hashlib import sha256
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from .errors import FinanceError


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    api_prefix: str = "/finance"
    tenant_id: str = "008502d6-3f79-46f0-ab37-9354e3fe80ff"
    client_id: str = "d4c92ba1-9d5f-4dc6-a869-9ab83c5a1d32"
    client_secret_env: str = "FINANCE_DEMO_CLIENT_SECRET"
    d365_base_url: str = "https://org1a43c536.operations.dynamics.com"
    company: str = "usmf"
    customers_entity: str = "CustomersV3"
    transactions_entity: str = "CustTransBiEntities"
    customer_group: str = "20"
    currency: str = "INR"
    payment_terms: str = "Net30"
    demo_customer_prefix: str = "TEST-COPILOT-"
    default_list_limit: int = 5
    maximum_list_limit: int = 20
    metadata_cache_ttl_hours: int = 24
    metadata_total_timeout_seconds: int = 180
    metadata_maximum_bytes: int = 134217728
    authentication_mode: str = "derived_api_key"
    connect_timeout_seconds: float = 5
    read_timeout_seconds: float = 15
    request_total_timeout_seconds: float = 25
    business_operation_timeout_seconds: float = 25
    metadata_read_timeout_seconds: float = 30
    metadata_retry_attempts: int = 3
    metadata_retry_backoff_seconds: float = 1
    read_retry_attempts: int = 2
    token_timeout_seconds: float = 15
    prefer_persistent_cache: bool = True
    cache_directory: str = "/tmp/ubticareers-finance-metadata"
    parser_config_version: int = 2
    schema_version: int = 1
    public_hostname: str = ""
    business_timezone: str = "Asia/Kolkata"
    reporting_scan_timeout_seconds: float = 120
    reporting_cache_ttl_seconds: int = 60
    reporting_retry_after_seconds: int = 5
    reporting_maximum_records: int = 100000
    reporting_maximum_pages: int = 2000
    reporting_page_maximum_bytes: int = 4194304
    reporting_maximum_cached_scopes: int = 4
    reporting_page_size: int = 500
    reporting_default_output_limit: int = 10
    reporting_maximum_output_limit: int = 100
    reporting_maximum_offset: int = 100000

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Settings":
        path = Path(path) if path else Path(__file__).resolve().parents[1] / "config" / "finance_settings.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            # Harmless operational documentation keys may coexist with runtime keys.
            known = {x.name for x in fields(cls)}
            value = cls(**{k: v for k, v in raw.items() if k in known})
            value.validate()
            return value
        except FinanceError:
            raise
        except (OSError, ValueError, TypeError, AttributeError):
            raise FinanceError("finance_configuration_invalid", "Finance configuration is unavailable or invalid.") from None

    def validate(self) -> None:
        from uuid import UUID
        try:
            UUID(self.tenant_id)
            UUID(self.client_id)
            url = urlsplit(self.d365_base_url)
            assert url.scheme == "https" and url.hostname and not url.username and not url.password
            assert not url.query and not url.fragment and url.path in ("", "/")
            assert url.port in (None, 443)
            assert self.api_prefix == "/finance" and self.company == "usmf"
            assert self.customers_entity == "CustomersV3" and self.transactions_entity == "CustTransBiEntities"
            assert self.client_secret_env == "FINANCE_DEMO_CLIENT_SECRET"
            assert self.authentication_mode in {"derived_api_key", "none"}
            assert isinstance(self.enabled, bool) and isinstance(self.prefer_persistent_cache, bool)
            assert 1 <= self.default_list_limit <= self.maximum_list_limit <= 20
            assert 1 <= self.metadata_cache_ttl_hours <= 168
            assert 1 <= self.metadata_maximum_bytes <= 134217728
            assert 1 <= self.metadata_total_timeout_seconds <= 300
            assert 0 < self.connect_timeout_seconds <= 15
            assert 0 < self.read_timeout_seconds <= 60
            assert 0 < self.request_total_timeout_seconds <= 60
            assert 0 < self.business_operation_timeout_seconds <= 60
            assert 0 < self.metadata_read_timeout_seconds <= 90
            assert 1 <= self.metadata_retry_attempts <= 4 and 1 <= self.read_retry_attempts <= 3
            assert 0 <= self.metadata_retry_backoff_seconds <= 10
            assert 0 < self.token_timeout_seconds <= 30
            assert self.demo_customer_prefix == "TEST-COPILOT-"
            assert self.business_timezone == "Asia/Kolkata"
            assert 1 <= self.reporting_scan_timeout_seconds <= 180
            assert 1 <= self.reporting_cache_ttl_seconds <= 300
            assert 1 <= self.reporting_retry_after_seconds <= 60
            assert 1 <= self.reporting_maximum_records <= 100000
            assert 1 <= self.reporting_maximum_pages <= 2000
            assert 1024 <= self.reporting_page_maximum_bytes <= 8388608
            assert 1 <= self.reporting_maximum_cached_scopes <= 8
            assert 1 <= self.reporting_page_size <= 1000
            assert 1 <= self.reporting_default_output_limit <= self.reporting_maximum_output_limit <= 100
            assert 0 <= self.reporting_maximum_offset <= 100000
        except (AssertionError, ValueError, TypeError):
            raise FinanceError("finance_configuration_invalid", "Finance configuration is invalid.") from None

    def secret(self) -> str:
        value = os.environ.get(self.client_secret_env, "")
        if not value or value.isspace():
            raise FinanceError("finance_secret_missing", "The finance client secret VALUE has not been configured.")
        if value == "fb07b3d3-b14c-4880-b49e-04d818160c0f":
            raise FinanceError("finance_secret_invalid", "A Secret ID is not a client secret VALUE.")
        return value

    @property
    def public_base_url(self) -> str:
        return "https://" + self.public_hostname

    @property
    def cache_scope(self) -> str:
        values = {"url": self.d365_base_url.rstrip("/").lower(), "tenant": self.tenant_id,
                  "client": self.client_id, "company": self.company,
                  "customers": self.customers_entity, "transactions": self.transactions_entity,
                  "parser": self.parser_config_version, "schema": self.schema_version}
        return sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
