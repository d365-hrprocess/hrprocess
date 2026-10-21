"""Isolated finance process logs. No request bodies, account values or amounts."""

from contextvars import ContextVar
import json
import logging
import math
from pathlib import PurePath
import re
import sys
import time
import uuid

_CONTEXT = ContextVar("finance_log_context", default={})
LOGGER = logging.getLogger("finance.workflow")
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:{}/-]{1,120}\Z")
TEXT = {
    "issue_code",
    "cause_code",
    "stage_tag",
    "exception_type",
    "method",
    "route",
    "entity",
    "state",
    "cache_source",
    "scope_type",
    "provider_code",
    "code_location",
}
NUMBERS = {
    "status_code",
    "upstream_status_code",
    "elapsed_ms",
    "count",
    "entity_count",
    "record_count",
    "pages",
    "bytes_count",
}
FLAGS = {"retryable", "outcome_uncertain", "complete", "cached", "stale"}


class SafeProviderLogs(logging.Filter):
    def filter(self, record):
        if (
            record.name == "uvicorn.access"
            and isinstance(record.args, tuple)
            and len(record.args) == 5
        ):
            record.msg, record.args = (
                "finance_access method=%s status=%s",
                (record.args[1], record.args[4]),
            )
        if record.name.startswith(("httpx", "httpcore")):
            if record.levelno < logging.WARNING:
                return False
            record.msg, record.args = (
                "finance_provider_warning details=see_finance_stage_logs",
                (),
            )
            record.exc_info = record.exc_text = record.stack_info = None
        if record.exc_info:
            kind = getattr(record.exc_info[0], "__name__", "UnknownError")
            record.msg, record.args = "finance_exception exception_type=%s", (kind,)
            record.exc_info = record.exc_text = record.stack_info = None
        return True


def configure_logging():
    LOGGER.setLevel(logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(
            "%(asctime)s UTC %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%S"
        )
        formatter.converter = time.gmtime
        handler.setFormatter(formatter)
        root.addHandler(handler)
    for handler in root.handlers:
        if not any(isinstance(item, SafeProviderLogs) for item in handler.filters):
            handler.addFilter(SafeProviderLogs())
    for name in ("httpx", "httpcore", "uvicorn.access", "uvicorn.error"):
        target = logging.getLogger(name)
        if name in ("httpx", "httpcore"):
            target.setLevel(logging.WARNING)
        if not any(isinstance(item, SafeProviderLogs) for item in target.filters):
            target.addFilter(SafeProviderLogs())


def log_event(stage, outcome, *, level=logging.INFO, **fields):
    values = {"stage": stage, "outcome": outcome}
    if not IDENTIFIER.fullmatch(stage) or not IDENTIFIER.fullmatch(outcome):
        return
    for key, value in {**_CONTEXT.get(), **fields}.items():
        if key == "request_id":
            try:
                values[key] = str(uuid.UUID(str(value)))
            except (ValueError, TypeError):
                pass
        elif (
            key in TEXT
            and isinstance(value, str)
            and IDENTIFIER.fullmatch(value)
            and "//" not in value
        ):
            values[key] = value
        elif (
            key in NUMBERS
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            if (isinstance(value, int) and value.bit_length() <= 64) or (
                isinstance(value, float) and math.isfinite(value)
            ):
                values[key] = value
        elif key in FLAGS and isinstance(value, bool):
            values[key] = value
    LOGGER.log(
        level,
        " ".join(f"{k}={json.dumps(v)}" for k, v in values.items()),
        extra={"finance_event": values},
    )


def failure_fields(error):
    trace = error.__traceback__
    while trace and trace.tb_next:
        trace = trace.tb_next
    location = (
        f"{PurePath(trace.tb_frame.f_code.co_filename).name}:{trace.tb_lineno}:{trace.tb_frame.f_code.co_name}"
        if trace
        else None
    )
    return {
        "code_location": location,
        "exception_type": type(error).__name__,
        "issue_code": getattr(error, "code", "finance_internal_error"),
        "cause_code": getattr(error, "cause_code", None),
        "upstream_status_code": getattr(error, "upstream_status_code", None),
        "stage_tag": getattr(error, "stage", None),
        "retryable": getattr(error, "retryable", False),
        "outcome_uncertain": getattr(error, "uncertain", False),
    }


class FinanceRequestLogs:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path", "").rstrip("/") in {
            "/health",
            "/finance/health",
        }:
            return await self.app(scope, receive, send)
        token = _CONTEXT.set({"request_id": str(uuid.uuid4())})
        started, status = time.monotonic(), 500
        try:

            async def capture(message):
                nonlocal status
                if message["type"] == "http.response.start":
                    status = message["status"]
                await send(message)

            await self.app(scope, receive, capture)
        except BaseException as error:
            log_event(
                "finance.request",
                "failed",
                level=logging.ERROR,
                **failure_fields(error),
            )
            raise
        finally:
            log_event(
                "finance.http",
                "failed" if status >= 400 else "completed",
                level=logging.WARNING if status >= 400 else logging.INFO,
                method=scope.get("method", ""),
                route=getattr(scope.get("route"), "path", "unmatched"),
                status_code=status,
                elapsed_ms=round((time.monotonic() - started) * 1000, 2),
            )
            _CONTEXT.reset(token)


def log_entra_error(response):
    """Only known error numbers leave the Entra response, never its description."""
    codes = {
        "700016": "entra_application_missing",
        "7000215": "entra_secret_invalid",
        "7000222": "entra_secret_expired",
        "7000218": "entra_secret_missing",
        "70011": "entra_scope_invalid",
        "65001": "entra_consent_required",
        "53003": "entra_policy_blocked",
    }
    try:
        payload = response.json()
        description = (
            payload.get("error_description", "") if isinstance(payload, dict) else ""
        )
        match = (
            re.search(r"\bAADSTS([0-9]{5,8})\b", description[:8192])
            if isinstance(description, str)
            else None
        )
        code = match.group(1) if match and match.group(1) in codes else None
    except (ValueError, TypeError):
        code = None
    log_event(
        "finance.token",
        "failed",
        status_code=response.status_code,
        issue_code=codes[code] if code else "entra_token_rejected",
        provider_code="AADSTS" + code if code else None,
    )
