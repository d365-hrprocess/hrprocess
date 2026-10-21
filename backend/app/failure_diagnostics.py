"""Mechanism-based failure categories. Exception values are never emitted."""

from __future__ import annotations

import asyncio
import re
import sqlite3

import httpx
from pydantic import ValidationError

from app.postgres_diagnostics import postgres_failure


def exception_diagnostics(exc: BaseException) -> dict:
    """Inspect a bounded exception chain and preserve existing structured causes."""
    chain, seen = [], set()
    current = exc
    while current is not None and id(current) not in seen and len(chain) < 6:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    result = {}
    # Outer integration wrappers often have the most accurate service context.
    for error in chain:
        diagnostics = getattr(error, "log_diagnostics", None) or getattr(error, "diagnostics", None)
        if isinstance(diagnostics, dict):
            for key, value in diagnostics.items():
                result.setdefault(key, value)
    result.setdefault("root_exception_type", type(chain[-1]).__name__)
    if "issue_code" in result:
        return result
    for error in chain:
        if isinstance(error, httpx.HTTPError):
            categories = (
                (
                    httpx.ConnectTimeout,
                    "network_connect_timeout",
                    "Connection establishment timed out; inspect DNS, firewall and provider availability.",
                ),
                (
                    httpx.ReadTimeout,
                    "network_read_timeout",
                    "The provider did not return data within the read budget; verify writes before retrying.",
                ),
                (
                    httpx.WriteTimeout,
                    "network_write_timeout",
                    "Sending the provider request timed out; write outcome may be uncertain.",
                ),
                (
                    httpx.PoolTimeout,
                    "network_pool_timeout",
                    "The HTTP connection pool was exhausted; inspect request concurrency and provider latency.",
                ),
                (
                    httpx.TimeoutException,
                    "network_timeout",
                    "The provider request timed out; inspect its stage and verify any unconfirmed write.",
                ),
                (
                    httpx.ConnectError,
                    "network_connection_failed",
                    "Connection failed; inspect DNS, TLS trust, firewall and provider availability.",
                ),
                (
                    httpx.RemoteProtocolError,
                    "network_protocol_failed",
                    "The provider closed or violated the HTTP protocol; inspect its service status.",
                ),
            )
            for kind, code, hint in categories:
                if isinstance(error, kind):
                    if isinstance(error, httpx.ConnectError):
                        text = str(error).lower()
                        if any(
                            x in text
                            for x in (
                                "certificate verify failed",
                                "certificate_verify_failed",
                                "self-signed",
                                "certificate has expired",
                            )
                        ):
                            code, hint = (
                                "network_tls_verification_failed",
                                "TLS certificate verification failed; inspect the existing CA trust and provider certificate without disabling TLS.",
                            )
                        elif any(
                            x in text
                            for x in ("getaddrinfo", "name or service not known", "failed to resolve", "no such host")
                        ):
                            code, hint = (
                                "network_dns_failed",
                                "The provider hostname could not be resolved; inspect configured DNS and network access.",
                            )
                        elif "connection refused" in text:
                            code, hint = (
                                "network_connection_refused",
                                "The provider connection was refused; inspect service availability and firewall access.",
                            )
                    result.update(issue_code=code, hint=hint)
                    return result
            result.update(
                issue_code="network_request_failed",
                hint="The HTTP request failed before a usable response. Inspect the provider stage and connection.",
            )
            return result
        module = type(error).__module__
        if module.startswith("psycopg"):
            reason, detail, _ = postgres_failure(error)
            state = getattr(error, "sqlstate", None)
            if isinstance(state, str) and re.fullmatch(r"[0-9A-Z]{5}", state):
                result["database_state"] = state
            result.update(issue_code="postgres_" + reason, hint=detail)
            return result
        if module.startswith("redis"):
            code = "redis_timeout" if "Timeout" in type(error).__name__ else "redis_connection_failed"
            result.update(
                issue_code=code,
                hint="Inspect the existing Redis service, connectivity and lock/queue stage; do not clear submission keys.",
            )
            return result
        if module.startswith("azure."):
            status = getattr(error, "status_code", None)
            if isinstance(status, int):
                result["upstream_status_code"] = status
            code = {
                401: "blob_authentication_failed",
                403: "blob_access_denied",
                404: "blob_resource_missing",
                409: "blob_conflict",
                429: "blob_throttled",
            }.get(status, "blob_operation_failed")
            result.update(
                issue_code=code,
                hint="Inspect private container access and the exact storage stage; preserve stored resumes and avoid logging storage URLs.",
            )
            return result
        if module.startswith("openai"):
            status = getattr(error, "status_code", None)
            if isinstance(status, int):
                result["upstream_status_code"] = status
            code = {
                400: "ai_request_rejected",
                401: "ai_authentication_failed",
                403: "ai_access_denied",
                404: "ai_deployment_missing",
                429: "ai_throttled",
            }.get(status, "ai_provider_failed")
            result.update(
                issue_code=code,
                hint="Inspect deployment/API compatibility, access, quota and the AI stage; do not log prompts or model responses.",
            )
            return result
        if isinstance(error, ValidationError):
            result.update(
                issue_code="schema_validation_failed",
                validation_error_count=error.error_count(),
                hint="The input or provider result does not match its schema; inspect the named validation stage, not raw field values.",
            )
            return result
        if isinstance(error, sqlite3.Error):
            result.update(
                issue_code="sqlite_operation_failed",
                hint="Inspect local metadata storage availability, permissions and locking; preserve the durable outbox.",
            )
            return result
        if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
            result.update(
                issue_code="operation_deadline_exceeded",
                hint="The operation exceeded its time budget; inspect the innermost stage and any unconfirmed write.",
            )
            return result
        if isinstance(error, OSError):
            result.update(
                issue_code="filesystem_or_connection_failed",
                hint="Inspect the storage/network stage and permissions; paths and exception values are omitted.",
            )
            return result
    result.update(
        issue_code="application_failure",
        hint="Inspect the failing stage and code location; provider may not have supplied a more specific cause.",
    )
    return result


_ENTRA_ERRORS = {
    "700016": (
        "entra_application_missing",
        "The client application was not found in the configured tenant; verify client and tenant IDs.",
    ),
    "7000215": ("entra_secret_invalid", "Entra rejected the client secret VALUE; a Secret ID is not a secret value."),
    "7000222": (
        "entra_secret_expired",
        "The application client secret has expired; rotate it through the approved secret settings.",
    ),
    "7000218": ("entra_secret_missing", "Entra requires a client secret or client assertion for this application."),
    "70011": ("entra_scope_invalid", "The requested resource scope is invalid; verify the configured D365 resource."),
    "65001": ("entra_consent_required", "The application requires approved tenant consent for this resource."),
    "53003": (
        "entra_policy_blocked",
        "Tenant conditional-access policy blocked authentication; ask the identity administrator to review it.",
    ),
    "700027": (
        "entra_assertion_invalid",
        "Entra rejected the client assertion; check the approved identity configuration.",
    ),
}


def entra_diagnostics(response):
    """Known Entra error numbers only; never return token/error_description text."""
    value = response.get("error_description", "") if isinstance(response, dict) else ""
    if isinstance(value, str):
        match = re.search(r"\bAADSTS([0-9]{5,8})\b", value[:8192])
        if match and match.group(1) in _ENTRA_ERRORS:
            code, hint = _ENTRA_ERRORS[match.group(1)]
            return {"issue_code": code, "hint": hint, "provider_code": "AADSTS" + match.group(1)}
    return {}
