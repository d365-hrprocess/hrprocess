"""Bounded server-to-server Entra/D365 transport, separate from HR credentials."""
import asyncio
from decimal import Decimal
import json as jsonlib
import math
import time
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx

from .errors import FinanceError
from .settings import Settings
from .observability import log_event, log_entra_error


def parse_json(response: httpx.Response):
    try:
        return jsonlib.loads(response.content, parse_float=Decimal)
    except (ValueError, UnicodeError):
        raise FinanceError("d365_invalid_response", "D365 returned an invalid JSON response.", 502) from None


class D365Client:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.http = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.read_timeout_seconds, connect=settings.connect_timeout_seconds),
            follow_redirects=False,
        )
        self._owns_client = http_client is None
        self._access_token = ""
        self._token_deadline = 0.0
        self._token_lock = asyncio.Lock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        if self._owns_client:
            await self.http.aclose()

    async def access_token(self, *, force: bool = False) -> str:
        if not force and self._access_token and time.monotonic() < self._token_deadline:
            return self._access_token
        async with self._token_lock:
            if not force and self._access_token and time.monotonic() < self._token_deadline:
                return self._access_token
            secret = self.settings.secret()
            log_event("finance.token", "start")
            try:
                async with asyncio.timeout(self.settings.token_timeout_seconds):
                    result = await self.http.post(
                        f"https://login.microsoftonline.com/{self.settings.tenant_id}/oauth2/v2.0/token",
                        data={"grant_type": "client_credentials", "client_id": self.settings.client_id,
                              "client_secret": secret, "scope": self.settings.d365_base_url.rstrip("/") + "/.default"},
                        timeout=httpx.Timeout(self.settings.token_timeout_seconds,
                                              connect=self.settings.connect_timeout_seconds),
                    )
            except (httpx.HTTPError, TimeoutError) as error:
                log_event("finance.token", "failed", exception_type=type(error).__name__)
                raise FinanceError("d365_token_unavailable", "Finance could not obtain a D365 access token.", 503, True) from None
            log_event("finance.token", "received", status_code=result.status_code)
            if result.status_code != 200:
                log_entra_error(result)
                raise FinanceError("d365_token_rejected", "Entra rejected the finance application's token request. Verify the secret VALUE, expiry and application access.", 503, False)
            try:
                payload = parse_json(result)
                token = payload["access_token"]
                expiry = float(payload["expires_in"])
                if not isinstance(token, str) or not token or not math.isfinite(expiry) or expiry <= 0:
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise FinanceError("d365_token_invalid", "Entra returned an invalid token response.", 502) from None
            self._access_token = token
            # Refresh before actual token expiry; very short tokens are not held indefinitely.
            self._token_deadline = time.monotonic() + max(0, expiry - min(60, expiry / 10))
            return token

    def url(self, path: str) -> str:
        value = urlsplit(path)
        if value.scheme or value.netloc or path.startswith("/") or any(part in {".", ".."} for part in path.split("/")) or value.query or value.fragment:
            raise FinanceError("finance_invalid_path", "Invalid internal D365 resource path.", 500)
        return self.settings.d365_base_url.rstrip("/") + "/data/" + path

    def _error(self, response: httpx.Response, *, write: bool) -> FinanceError:
        status = response.status_code
        log_event("finance.d365.response", "failed", upstream_status_code=status)
        def upstream_error(*args):
            # Only the HTTP status is retained, never the ERP error body/headers.
            return FinanceError(*args, upstream_status_code=status)
        if status == 404:
            return upstream_error("d365_not_found", "The requested D365 resource was not found.", 404)
        if status in (401, 403):
            return upstream_error("d365_access_denied", "D365 denied access to this operation. Verify the application's mapped D365 user and permissions.", 403)
        if status == 409:
            return upstream_error("d365_conflict", "D365 rejected the write because the record conflicts with existing data.", 409)
        if status in (400, 422):
            return upstream_error("d365_validation_failed", "D365 rejected the request. Verify the customer values, configured group, currency, terms and related-record rules.", 422)
        if status == 429:
            return upstream_error("d365_throttled", "D365 temporarily throttled this operation.", 503, not write, write)
        if 300 <= status < 400:
            return upstream_error("d365_login_redirect", "D365 returned a redirect instead of API data. Verify the environment URL and application authorization.", 502)
        return upstream_error("d365_unavailable", "D365 could not complete this operation.", 503, not write, write)

    async def request(self, method: str, path: str, *, params=None, json=None) -> httpx.Response:
        method = method.upper()
        if method not in {"GET", "POST", "PATCH", "DELETE"}:
            raise FinanceError("finance_invalid_method", "Unsupported internal D365 operation.", 500)
        write = method != "GET"
        url = self.url(path)
        attempts = 1 if write else self.settings.read_retry_attempts
        try:
            async with asyncio.timeout(self.settings.request_total_timeout_seconds):
                for attempt in range(attempts):
                    token = await self.access_token()
                    try:
                        response = await self.http.request(
                            method, url, params=params, json=json,
                            headers={"Authorization": "Bearer " + token, "Accept": "application/json", "OData-Version": "4.0"},
                        )
                    except httpx.HTTPError as error:
                        log_event("finance.d365.transport", "failed", exception_type=type(error).__name__)
                        if not write and attempt + 1 < attempts:
                            await asyncio.sleep(min(2 ** attempt, 2))
                            continue
                        cause = next((code for kind, code in (
                            (httpx.ReadTimeout, "d365_read_timeout"), (httpx.ConnectTimeout, "d365_connect_timeout"),
                            (httpx.WriteTimeout, "d365_send_timeout"), (httpx.PoolTimeout, "d365_pool_timeout"),
                        ) if isinstance(error, kind)), "d365_network_error")
                        raise FinanceError("d365_network_error", "The D365 request could not be completed. Verify the exact record before retrying a write.",
                            503, not write, write, cause_code=cause) from None
                    if not write and response.status_code == 401 and attempt + 1 < attempts:
                        self._token_deadline = 0
                        await response.aclose()
                        continue
                    if not write and response.status_code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                        await response.aclose()
                        await asyncio.sleep(min(2 ** attempt, 2))
                        continue
                    if not 200 <= response.status_code < 300:
                        raise self._error(response, write=write)
                    return response
        except TimeoutError:
            raise FinanceError("d365_request_timeout", "The D365 request timed out. Verify the exact record before retrying a write.", 503, not write, write) from None
        raise FinanceError("d365_unavailable", "D365 is unavailable.", 503, not write, write)

    async def json_request(self, method: str, path: str, *, params=None, json=None):
        response = await self.request(method, path, params=params, json=json)
        return parse_json(response)

    async def reporting_page(self, entity: str, original_params: dict, next_link: str | None = None):
        """GET one bounded page. Never forward bearer tokens to a nextLink host.

        Scope/select/order invariants are retained even if a server omits them
        from its nextLink. An explicitly changed invariant fails closed.
        """
        target = self.url(entity)
        params = dict(original_params)
        if next_link is not None:
            if not isinstance(next_link, str) or len(next_link) > 16384:
                raise FinanceError("unsafe_pagination_link", "D365 returned an invalid pagination link.", 502)
            candidate = urlsplit(urljoin(target, next_link))
            expected = urlsplit(target)
            try:
                valid = (candidate.scheme == "https" and candidate.hostname == expected.hostname
                         and candidate.port in (None, 443) and not candidate.username and not candidate.password
                         and not candidate.fragment and candidate.path == expected.path)
            except ValueError:
                valid = False
            pairs = parse_qsl(candidate.query, keep_blank_values=True)
            query = dict(pairs)
            allowed = set(original_params) | {"$skiptoken", "$skip"}
            if (not valid or len(query) != len(pairs) or set(query) - allowed
                    or any(k in query and query[k] != v for k, v in original_params.items())
                    or not ({"$skiptoken", "$skip"} & query.keys())):
                raise FinanceError("unsafe_pagination_link", "D365 pagination changed the approved entity, scope or query.", 502)
            params.update(query)
        try:
            async with asyncio.timeout(self.settings.request_total_timeout_seconds):
                for attempt in range(self.settings.read_retry_attempts):
                    token = await self.access_token()
                    try:
                        async with self.http.stream("GET", target, params=params, headers={
                            "Authorization": "Bearer " + token,
                            "Accept": "application/json;IEEE754Compatible=true",
                            "OData-Version": "4.0",
                            "Prefer": f"odata.maxpagesize={self.settings.reporting_page_size}",
                        }) as response:
                            if response.status_code == 401 and attempt + 1 < self.settings.read_retry_attempts:
                                self._token_deadline = 0
                                continue
                            if response.status_code in (429, 500, 502, 503, 504) and attempt + 1 < self.settings.read_retry_attempts:
                                await asyncio.sleep(min(2 ** attempt, 2))
                                continue
                            if response.status_code != 200:
                                raise self._error(response, write=False)
                            log_event("finance.d365.page", "received", entity=entity, status_code=response.status_code)
                            content = bytearray()
                            async for block in response.aiter_bytes():
                                content.extend(block)
                                if len(content) > self.settings.reporting_page_maximum_bytes:
                                    raise FinanceError("report_page_size_limit", "A reporting page exceeded the configured size budget; no total is available.", 503)
                            try:
                                return jsonlib.loads(content, parse_float=Decimal)
                            except (ValueError, UnicodeError):
                                raise FinanceError("d365_invalid_response", "D365 returned invalid reporting JSON.", 502) from None
                    except httpx.HTTPError:
                        if attempt + 1 == self.settings.read_retry_attempts:
                            raise FinanceError("d365_network_error", "The complete reporting scan could not finish; no total is available.", 503, True) from None
                        await asyncio.sleep(min(2 ** attempt, 2))
        except TimeoutError:
            raise FinanceError("report_page_timeout", "A D365 reporting page timed out; no total is available.", 503, True) from None
        raise FinanceError("d365_unavailable", "The complete reporting scan could not finish.", 503, True)
