"""Terminal workflow diagnostics without request bodies, credentials or profiles."""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import math
import re
import sys
import time
import traceback
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import PurePath

from app.diagnostic_templates import valid_message_template
from app.failure_diagnostics import exception_diagnostics


logger = logging.getLogger("app.workflow")
_context: ContextVar[dict] = ContextVar("workflow_log_context", default={})
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:{}/-]{1,120}\Z")
_UUID_FIELDS = {"request_id", "correlation_id", "activity_id"}
_TEXT_FIELDS = {
    "application_id", "candidate_id", "requirement_id", "evaluation_id", "operation", "provider",
    "exception_type", "issue_code", "reason", "company", "field", "table", "enum_symbol",
    "role", "mode", "storage", "storage_backend", "backend", "outbox_stage", "next_stage",
    "status", "state", "stage_tag", "ai_status", "deployment", "prompt_version", "claim",
    "oauth_error", "response_kind", "error_code", "method", "route", "extension", "job_reference",
    "code_location",
    "value_type", "root_exception_type", "provider_stack", "error_fingerprint", "database_state", "cause_code", "provider_code",
}
_NUMBER_FIELDS = {
    "elapsed_ms", "duration_ms", "status_code", "http_status", "attempt", "count", "bytes_count",
    "text_chars", "max_length", "request_fields_count", "response_bytes", "record_count", "entries_count",
    "retry_delay_seconds", "retry_in_seconds", "limit", "schema_version", "max_bytes", "resume_chars",
    "evidence_count", "matched_count", "missing_count", "score", "max_attempts", "queued_count",
    "value_length", "upstream_status_code", "message_count", "inner_exception_count", "validation_error_count",
}
_BOOL_FIELDS = {
    "retryable", "force_refresh", "token_cache_hit", "token_cached", "found", "acquired", "created",
    "cached", "confirmed", "queued", "completed", "recovered", "rejected", "released", "validated",
    "lock_acquired", "authenticated", "rotated", "configured", "consent_accepted", "demo_mode",
    "existing", "name_available", "email_available", "nonce_present", "diagnostic_truncated", "cause_available", "resume_changed", "contact_changed", "consent_changed",
}


def _safe_fields(fields):
    result = {}
    for key, value in fields.items():
        if value is None:
            continue
        if key in _UUID_FIELDS:
            try:
                result[key] = str(uuid.UUID(str(value)))
            except (ValueError, TypeError, AttributeError):
                pass
        elif key in _BOOL_FIELDS and isinstance(value, bool):
            result[key] = value
        elif key in _NUMBER_FIELDS and isinstance(value, (int, float)) and not isinstance(value, bool):
            if (isinstance(value, int) and value.bit_length() <= 64) or (isinstance(value, float) and math.isfinite(value)):
                result[key] = value
        elif key in _TEXT_FIELDS and isinstance(value, str) and _IDENTIFIER.fullmatch(value):
            # Route templates and identifiers are permitted; URLs, query strings,
            # free-form names and contact fields are never accepted as metadata.
            if "//" not in value:
                result[key] = value
        elif key == "message_template" and valid_message_template(value):
            result[key] = value
        elif key in {"provider_message", "hint"} and isinstance(value, str):
            # Only the D365 diagnostics parser may supply these canonical messages.
            # Do not call this logger with external response or exception text.
            if len(value) <= 400 and value.isprintable() and "@" not in value and not re.search(r"https?://", value, re.I):
                result[key] = value
    return result


def log_event(stage: str, outcome: str, *, level: int = logging.INFO, **fields):
    """Log only scalar allowlisted operational metadata, even in DEBUG mode."""
    if not logger.isEnabledFor(level):
        return
    if not _IDENTIFIER.fullmatch(stage) or not _IDENTIFIER.fullmatch(outcome):
        return
    event = {"stage": stage, "outcome": outcome, **_safe_fields({**_context.get(), **fields})}
    metadata = " ".join(f"{key}={json.dumps(value, ensure_ascii=True)}" for key, value in event.items()
                        if key not in {"stage", "outcome"})
    logger.log(level, f"stage={stage} outcome={outcome}" + (" " + metadata if metadata else ""),
               extra={"workflow_event": event})


def log_request_shape(operation: str, body: dict, *, level: int = logging.DEBUG):
    """Known field names/types/lengths at the requested level, never values."""
    if not logger.isEnabledFor(level):
        return
    flat = next(iter(body.values())) if len(body) == 1 and isinstance(next(iter(body.values())), dict) else body
    permitted = {
        "Company", "ApplicationId", "RequirementId", "CandidateId", "EvaluationId", "FirstName", "LastName",
        "FullName", "Email", "Phone", "LinkedInProfileUrl", "LinkedInIssuer", "LinkedInSub", "IdentityKey",
        "Score", "MeetsMinimumCriteria", "MatchLabel", "Summary", "MatchedSkillsJson", "MissingSkillsJson", "EvidenceJson",
    }
    for field, value in flat.items():
        if field in permitted:
            log_event("d365.request.shape", "validated", level=level, operation=operation, field=field,
                      value_type=type(value).__name__, value_length=len(value) if isinstance(value, str) else None)


@contextmanager
def log_context(**fields):
    """Task-local metadata; never leaks across requests, threads or worker jobs."""
    token = _context.set({**_context.get(), **_safe_fields(fields)})
    try:
        yield
    finally:
        _context.reset(token)


def error_fields(exc: BaseException):
    fields = {"exception_type": type(exc).__name__}
    trace = exc.__traceback__
    if trace is not None:
        while trace.tb_next is not None:
            trace = trace.tb_next
        fields["code_location"] = f"{PurePath(trace.tb_frame.f_code.co_filename).name}:{trace.tb_lineno}:{trace.tb_frame.f_code.co_name}"
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        fields["status_code"] = status
    retryable = getattr(exc, "retryable", None)
    if isinstance(retryable, bool):
        fields["retryable"] = retryable
    try:
        fields.update(_safe_fields(exception_diagnostics(exc)))
        # The handled Python exception and returned provider cause are distinct.
        fields["exception_type"] = type(exc).__name__
    except Exception:
        # Instrumentation must never replace the application's original failure.
        fields["issue_code"] = "diagnostic_classification_unavailable"
    return fields


@contextmanager
def stage(name: str, **fields):
    started = time.monotonic()
    log_event(name, "start", **fields)
    try:
        yield
    except BaseException as exc:
        cancelled = isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit))
        log_event(name, "cancelled" if cancelled else "failed", level=logging.WARNING if cancelled else logging.ERROR,
                  **{**fields, **error_fields(exc), "elapsed_ms": round((time.monotonic() - started) * 1000, 2)})
        raise
    else:
        log_event(name, "completed", **{**fields, "elapsed_ms": round((time.monotonic() - started) * 1000, 2)})


def traced(name: str):
    """Instrument an existing function without changing results or exceptions."""
    def decorate(function):
        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            async def asynchronous(*args, **kwargs):
                with stage(name):
                    return await function(*args, **kwargs)
            return asynchronous

        @functools.wraps(function)
        def synchronous(*args, **kwargs):
            with stage(name):
                return function(*args, **kwargs)
        return synchronous
    return decorate


class SafeAccessLogs(logging.Filter):
    def filter(self, record):
        # Uvicorn includes OAuth code/state query strings in its default access log.
        if isinstance(record.args, tuple) and len(record.args) == 5:
            args = list(record.args)
            args[2] = str(args[2]).split("?", 1)[0].split("#", 1)[0]
            record.args = tuple(args)
        return True


class SafeExceptionLogs(logging.Filter):
    def filter(self, record):
        # Keep code locations useful for unexpected errors, without echoing an
        # exception value, provider payload, chained error or local variable.
        if record.exc_info:
            exc_type, _, trace = record.exc_info
            frames = " > ".join(f"{PurePath(frame.filename).name}:{frame.lineno}:{frame.name}"
                                for frame in traceback.extract_tb(trace)[-12:])
            record.msg = "application_exception exception_type=%s code_locations=%s"
            record.args = (getattr(exc_type, "__name__", "UnknownError"), frames)
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        elif "Traceback (most recent call last):" in record.getMessage():
            record.msg = "startup_exception details=see_workflow_stage_logs"
            record.args = ()
        return True


class SafeSDKLogs(logging.Filter):
    """SDK child loggers may bypass their namespace's configured log level."""

    namespaces = ("httpx", "httpcore", "msal", "azure", "openai", "urllib3", "authlib")

    def filter(self, record):
        namespace = next((name for name in self.namespaces
                          if record.name == name or record.name.startswith(name + ".")), None)
        if namespace is None:
            return True
        if record.levelno < logging.WARNING:
            return False
        record.msg = "sdk_warning provider=%s details=redacted_see_workflow_stage_logs"
        record.args = (namespace,)
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def configure_logging(level: str = "INFO"):
    """Enable terminal logs for the API and worker without SDK wire logging."""
    application_logger = logging.getLogger("app")
    application_logger.setLevel(level)
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter("%(asctime)s UTC %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%S")
        formatter.converter = time.gmtime
        handler.setFormatter(formatter)
        root.addHandler(handler)
        root.setLevel(logging.INFO)
    for handler in root.handlers:
        for filter_type in (SafeSDKLogs, SafeExceptionLogs):
            if not any(isinstance(item, filter_type) for item in handler.filters):
                handler.addFilter(filter_type())
    for name, filter_type in (("uvicorn.access", SafeAccessLogs), ("uvicorn.error", SafeExceptionLogs)):
        target = logging.getLogger(name)
        if not any(isinstance(item, filter_type) for item in target.filters):
            target.addFilter(filter_type())
    for name in SafeSDKLogs.namespaces:
        target = logging.getLogger(name)
        target.setLevel(logging.WARNING)
        if not any(isinstance(item, SafeSDKLogs) for item in target.filters):
            target.addFilter(SafeSDKLogs())


class RequestLoggingMiddleware:
    def __init__(self, app, routes=None):
        self.app, self.routes = app, routes if routes is not None else []

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.monotonic()
        status = 500
        route_name = "unmatched"
        for route in self.routes:
            pattern = getattr(route, "path_regex", None)
            if pattern is not None and pattern.fullmatch(scope.get("path", "")):
                route_name = route.path
                break
        with log_context(request_id=request_id):
            log_event("http.request", "start", method=scope.get("method", ""), route=route_name)

            async def traced_send(message):
                nonlocal status
                if message["type"] == "http.response.start":
                    status = message["status"]
                    message["headers"] = [(key, value) for key, value in message.get("headers", [])
                                          if key.lower() != b"x-request-id"]
                    message["headers"].append((b"x-request-id", request_id.encode("ascii")))
                await send(message)

            try:
                await self.app(scope, receive, traced_send)
            except BaseException as exc:
                log_event("http.request", "failed", level=logging.ERROR,
                          route=getattr(scope.get("route"), "path", route_name),
                          elapsed_ms=round((time.monotonic() - started) * 1000, 2), **error_fields(exc))
                raise
            else:
                log_event("http.request", "failed" if status >= 400 else "completed",
                          level=logging.WARNING if status >= 400 else logging.INFO,
                          route=getattr(scope.get("route"), "path", route_name), status_code=status,
                          elapsed_ms=round((time.monotonic() - started) * 1000, 2))
