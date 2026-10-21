"""Finance ASGI application, isolated from the HR application's interpreter."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr, TypeAdapter, ValidationError, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from .business import ACCOUNT_PATTERN, FinanceBusiness
from .d365 import D365Client
from .errors import FinanceError
from .metadata import MetadataManager
from .settings import Settings


class ErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool = False
    outcome_uncertain: bool = False
    verification_required: bool = False


class ErrorResponse(BaseModel):
    success: Literal[False] = False
    error: ErrorDetail


class SuccessResponse(BaseModel):
    success: Literal[True] = True
    data: Any
    meta: dict[str, Any] = Field(default_factory=dict)


class CustomerRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_account: StrictStr
    organization_name: StrictStr | None = None
    company: StrictStr
    currency: StrictStr | None = None
    customer_group: StrictStr | None = None
    payment_terms: StrictStr | None = None


class TransactionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_account: StrictStr
    company: StrictStr
    invoice: StrictStr | None = None
    voucher: StrictStr | None = None
    transaction_date: StrictStr | None = None
    due_date: StrictStr | None = None
    amount: StrictStr | None = None
    transaction_type: StrictStr | None = None
    currency: StrictStr | None = None


class DeletedCustomerRecord(BaseModel):
    customer_account: StrictStr
    company: StrictStr
    deleted: Literal[True]
    verified: Literal[True]


class CustomerResponse(SuccessResponse):
    data: CustomerRecord


class CustomerListResponse(SuccessResponse):
    data: list[CustomerRecord]


class TransactionListResponse(SuccessResponse):
    data: list[TransactionRecord]


class DeletedCustomerResponse(SuccessResponse):
    data: DeletedCustomerRecord


class CreateCustomerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_account: str = Field(min_length=1, max_length=40, pattern=ACCOUNT_PATTERN)
    organization_name: str = Field(min_length=1, max_length=100)

    @field_validator("organization_name")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value or not value.isprintable():
            raise ValueError("Enter a printable organization name.")
        return value


class UpdateCustomerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    organization_name: str = Field(min_length=1, max_length=100)

    @field_validator("organization_name")
    @classmethod
    def clean_name(cls, value):
        return CreateCustomerRequest.clean_name(value)


def connector_key(settings: Settings) -> str:
    message = f"finance-demo/copilot/v1|{settings.tenant_id}|{settings.client_id}"
    return hmac.new(settings.secret().encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def error_response(error: FinanceError) -> JSONResponse:
    detail = ErrorDetail(
        code=error.code, message=error.message, retryable=error.retryable,
        outcome_uncertain=error.uncertain, verification_required=error.uncertain,
    )
    response = JSONResponse(ErrorResponse(error=detail).model_dump(), status_code=error.status_code)
    if error.retryable:
        response.headers["Retry-After"] = "5"
    response.headers["Cache-Control"] = "no-store"
    return response


def success(data: Any, *, meta=None, status=200, data_model=None):
    if data_model is not None:
        try:
            data = TypeAdapter(data_model).validate_python(data)
        except ValidationError:
            raise FinanceError("invalid_d365_response", "D365 returned fields that do not match the finance response schema.", status_code=502) from None
    return JSONResponse(SuccessResponse(data=data, meta=meta or {}).model_dump(mode="json"), status_code=status,
                        headers={"Cache-Control": "no-store"})


def create_app(settings: Settings | None = None, client=None, metadata=None) -> FastAPI:
    settings = settings or Settings.load()
    supplied_client = client is not None
    client = client or D365Client(settings)
    metadata = metadata or MetadataManager(settings, client)
    business = FinanceBusiness(settings, client, metadata)

    @asynccontextmanager
    async def lifespan(application):
        # Mounted ASGI children do not automatically receive lifespans. This module
        # runs as its own supervised ASGI process, so this lifespan always executes.
        if settings.enabled:
            metadata.start()
        yield
        await metadata.close()
        if not supplied_client:
            await client.close()

    application = FastAPI(
        title="ubticareers finance demo", version="1.0.0", lifespan=lifespan,
        docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False,
    )
    application.state.settings = settings
    application.state.metadata = metadata
    application.state.business = business

    @application.middleware("http")
    async def guard(request: Request, call_next):
        # This check uses the effective route path for both stripped proxy paths
        # and Uvicorn's configured root_path=/finance.
        path = request.scope.get("path", "")
        root = request.scope.get("root_path", "")
        if root and path.startswith(root):
            path = path[len(root):] or "/"
        if path == "/health" and request.method == "GET":
            return await call_next(request)
        if not settings.enabled:
            return error_response(FinanceError("finance_disabled", "The temporary finance demo is disabled.", status_code=503))
        try:
            if settings.authentication_mode == "derived_api_key":
                expected = connector_key(settings)
                actual = request.headers.get("X-Finance-Demo-Key", "")
                if len(actual) != 64 or not hmac.compare_digest(actual.encode("utf-8"), expected.encode("utf-8")):
                    return error_response(FinanceError("invalid_connector_key", "A valid finance connector credential is required.", status_code=401))
            elif settings.authentication_mode != "none":
                return error_response(FinanceError("finance_auth_configuration", "The finance authentication configuration is not supported.", status_code=503))
            # Read the secret even if explicit no-auth mode is selected. No business
            # operation is useful until server-side D365 credentials exist.
            settings.secret()
            return await call_next(request)
        except FinanceError as error:
            return error_response(error)
        except Exception:
            return error_response(FinanceError("finance_internal_error", "The finance request could not be completed.", status_code=500))

    @application.exception_handler(FinanceError)
    async def finance_error(request, error):
        return error_response(error)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # Do not echo inputs, headers, URLs, credentials or raw upstream responses.
        return error_response(FinanceError("invalid_request", "Check the account, organization name, limit and allowed request fields.", status_code=422))

    @application.exception_handler(StarletteHTTPException)
    async def http_error(request, error):
        code = "finance_route_not_found" if error.status_code == 404 else "finance_method_not_allowed"
        message = "The finance route was not found." if error.status_code == 404 else "This HTTP method is not available for the finance route."
        return error_response(FinanceError(code, message, status_code=error.status_code))

    async def operation(awaitable, *, write=False):
        try:
            async with asyncio.timeout(settings.business_operation_timeout_seconds):
                return await awaitable
        except TimeoutError:
            raise FinanceError(
                "write_outcome_uncertain" if write else "finance_request_timeout",
                "The write outcome could not be confirmed. Read the exact account before another write." if write else
                "The finance request exceeded its time budget. Try the read again later.",
                status_code=504, retryable=not write, uncertain=write,
            ) from None

    @application.get("/health", include_in_schema=False)
    async def health():
        return {"status": "ok" if settings.enabled else "disabled"}

    @application.get("/ready", include_in_schema=False)
    async def ready():
        metadata.require_customers()
        return success(metadata.status(), meta={"company": settings.company})

    @application.get("/metadata/status", include_in_schema=False)
    async def metadata_status():
        return success(metadata.status(), meta={"company": settings.company})

    @application.get("/swagger2.json", include_in_schema=False)
    async def swagger():
        path = Path(__file__).resolve().parents[1] / "connector" / "swagger2.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise FinanceError("connector_definition_unavailable", "The connector definition is not installed.", status_code=503) from None
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    @application.get("/customers", operation_id="ListFinanceCustomers", response_model=CustomerListResponse)
    async def customers(limit: int = Query(default=settings.default_list_limit, ge=1, le=settings.maximum_list_limit)):
        data, meta = await operation(business.list_customers(limit))
        return success(data, meta=meta, data_model=list[CustomerRecord])

    @application.get("/customers/{account}/transactions", operation_id="GetFinanceCustomerTransactions", response_model=TransactionListResponse)
    async def transactions(account: str, limit: int = Query(default=10, ge=1, le=settings.maximum_list_limit)):
        data, meta = await operation(business.transactions(account, limit))
        return success(data, meta=meta, data_model=list[TransactionRecord])

    @application.get("/customers/{account}", operation_id="GetFinanceCustomer", response_model=CustomerResponse)
    async def customer(account: str):
        return success(await operation(business.get_customer(account)), meta={"company": settings.company}, data_model=CustomerRecord)

    @application.post("/customers", operation_id="CreateFinanceCustomer", response_model=CustomerResponse, status_code=201)
    async def create_customer(body: CreateCustomerRequest):
        data = await operation(business.create_customer(body.customer_account, body.organization_name), write=True)
        return success(data, meta={"company": settings.company, "verified": True}, status=201, data_model=CustomerRecord)

    @application.patch("/customers/{account}", operation_id="UpdateFinanceCustomer", response_model=CustomerResponse)
    async def update_customer(account: str, body: UpdateCustomerRequest):
        data = await operation(business.update_customer(account, body.organization_name), write=True)
        return success(data, meta={"company": settings.company, "verified": True}, data_model=CustomerRecord)

    @application.delete("/customers/{account}", operation_id="DeleteFinanceCustomer", response_model=DeletedCustomerResponse)
    async def delete_customer(account: str):
        data = await operation(business.delete_customer(account), write=True)
        return success(data, meta={"company": settings.company, "verified": True}, data_model=DeletedCustomerRecord)

    return application


app = create_app()
