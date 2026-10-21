"""Configurable JSON custom-service adapter. D365 remains the system of record."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
import msal
from pydantic import ValidationError

from app.d365_diagnostics import d365_error_diagnostics
from app.errors import ConfigurationError, IntegrationError
from app.failure_diagnostics import entra_diagnostics, exception_diagnostics
from app.models import Candidate, Evaluation, Job, LinkedInProfile, User
from app.observability import log_event, log_request_shape, traced

PATHS = {
    "get_open_requirements": "d365_requirement_path",
    "submit_application": "d365_submit_application_path",
    "save_candidate_evaluation": "d365_save_evaluation_path",
    "get_top_candidates": "d365_top_candidates_path",
    "get_my_applications": "d365_my_applications_path",
    "get_application": "d365_application_path",
}

# Canonical key -> X++ contract key. Override an individual operation through settings.
REQUEST_FIELDS = {
    "company": "Company", "requirement_id": "RequirementId",
    "application_id": "ApplicationId", "candidate_id": "CandidateId",
    "linkedin_issuer": "LinkedInIssuer", "linkedin_sub": "LinkedInSub",
    "owner_issuer": "LinkedInIssuer", "owner_sub": "LinkedInSub",
    "full_name": "FullName", "candidate_name": "FullName", "email": "Email",
    "phone": "Phone", "linkedin_url": "LinkedInUrl", "linkedin_profile": "LinkedInProfileJson",
    "resume_file_name": "ResumeFileName",
    "resume_ref": "ResumeBlobKey", "resume_blob_key": "ResumeBlobKey",
    "resume_sha256": "ResumeSha256", "consent_version": "ConsentVersion",
    "consent_date": "ConsentDateTime", "consent_date_time": "ConsentDateTime",
    "idempotency_key": "IdempotencyKey", "jd_hash": "JDHash", "job_description_hash": "JDHash",
    "consent_accepted": "ConsentAccepted", "limit": "Limit",
    "score": "Score", "meets_minimum_criteria": "MeetsMinimumCriteria",
    "match_label": "MatchLabel", "score_justification": "ScoreJustification",
    "summary": "Summary", "matched_requirements": "MatchedSkillsJson",
    "partially_matched_requirements": "PartiallyMatchedJson",
    "not_evidenced_requirements": "MissingSkillsJson", "relevant_evidence": "EvidenceJson",
    "evaluation_confidence": "EvaluationConfidence", "model_deployment": "ModelDeployment",
    "prompt_version": "PromptVersion", "rubric_version": "RubricVersion",
    "evaluation_date": "EvaluatedDateTime",
}
ALIASES = {
    "job_title": "title", "job_description": "description", "job_location": "location",
    "job_status": "status", "required_qualifications": "qualifications",
    "minimum_experience_years": "min_experience", "maximum_experience_years": "max_experience",
    "number_of_openings": "openings", "published_date_time": "published_date",
    "closing_date_time": "deadline", "evaluation_criteria_json": "evaluation_criteria",
    "applied_date_time": "applied_date", "evaluated_date_time": "evaluation_date",
    "resume_blob_key": "resume_ref", "linked_in_issuer": "owner_issuer",
    "linked_in_sub": "owner_sub", "linkedin_issuer": "owner_issuer", "linkedin_sub": "owner_sub",
    "full_name": "candidate_name", "matched_skills_json": "matched_requirements",
    "partially_matched_json": "partially_matched_requirements",
    "missing_skills_json": "not_evidenced_requirements", "evidence_json": "relevant_evidence",
    "linked_in_profile_json": "linkedin_profile", "linkedin_profile_json": "linkedin_profile",
}
MEMO_FIELDS = {"matched_requirements", "partially_matched_requirements", "not_evidenced_requirements", "relevant_evidence"}


def snake(key: str) -> str:
    key = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", key)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).lower()


def normalize(value: Any, field_map: dict[str, str] | None = None) -> Any:
    """Normalize field names and parse the proposed memo JSON fields, without inventing values."""
    if isinstance(value, list):
        return [normalize(item, field_map) for item in value]
    if not isinstance(value, dict):
        return value
    reverse = {external: canonical for canonical, external in (field_map or {}).items()}
    result: dict[str, Any] = {}
    for external, item in value.items():
        raw_key = snake(external)
        key = reverse.get(external, ALIASES.get(raw_key, raw_key))
        if key in MEMO_FIELDS or key in {"evaluation_criteria", "linkedin_profile"}:
            if isinstance(item, str):
                try:
                    item = json.loads(item)
                except (json.JSONDecodeError, TypeError) as exc:
                    category = "profile" if key == "linkedin_profile" else "evaluation"
                    raise IntegrationError(f"D365 returned malformed {category} JSON", retryable=False) from exc
        result[key] = normalize(item, field_map)
    return result


class D365Service:
    def __init__(self, settings: Any, *, client: httpx.AsyncClient | None = None, msal_app: Any = None):
        self.settings = settings
        self.client = client
        self._msal = msal_app
        self._token: str | None = None
        self._expires_at = 0.0
        self._token_lock = asyncio.Lock()

    @traced("d365.config")
    def _check_config(self) -> None:
        if not self.settings.d365_enabled:
            log_event("d365.config", "failed", level=logging.ERROR, issue_code="d365_disabled")
            raise ConfigurationError("D365 is disabled. Configure D365_ENABLED for connected mode.")
        required = ["d365_base_url", "d365_tenant_id", "d365_client_id", "d365_client_secret"]
        if any(not getattr(self.settings, key, "") for key in required):
            log_event("d365.config", "failed", level=logging.ERROR, issue_code="d365_credentials_missing")
            raise ConfigurationError("D365 base URL and Entra client credentials are required.")
        parsed = urlparse(self.settings.d365_base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
            log_event("d365.config", "failed", level=logging.ERROR, issue_code="d365_resource_url_invalid")
            raise ConfigurationError("D365_BASE_URL must be an HTTPS resource URL.")
        if not re.fullmatch(r"[a-zA-Z0-9.-]+", self.settings.d365_tenant_id):
            log_event("d365.config", "failed", level=logging.ERROR, issue_code="d365_tenant_invalid")
            raise ConfigurationError("D365_TENANT_ID must be a tenant ID or domain.")

    @traced("d365.auth")
    async def get_access_token(self, force_refresh: bool = False) -> str:
        self._check_config()
        async with self._token_lock:
            if not force_refresh and self._token and time.monotonic() < self._expires_at:
                log_event("d365.auth.cache", "hit", token_cache_hit=True)
                return self._token
            log_event("d365.auth.cache", "miss", token_cache_hit=False, force_refresh=force_refresh)
            scope = [f"{self.settings.d365_base_url.rstrip('/')}/.default"]

            def acquire() -> dict:
                if self._msal is None:
                    self._msal = msal.ConfidentialClientApplication(
                        self.settings.d365_client_id,
                        authority=f"https://login.microsoftonline.com/{self.settings.d365_tenant_id}",
                        client_credential=self.settings.d365_client_secret,
                        token_cache=msal.TokenCache(),
                    )
                if force_refresh:
                    self._msal.remove_tokens_for_client()
                return self._msal.acquire_token_for_client(scopes=scope)

            try:
                log_event("d365.auth.acquire", "start", force_refresh=force_refresh)
                response = await asyncio.to_thread(acquire)
            except Exception as exc:
                log_event("d365.auth.acquire", "failed", level=logging.ERROR,
                          issue_code="entra_connection_failed")
                raise IntegrationError("D365 authentication could not be completed") from exc
            token = response.get("access_token")
            if not token:
                issue = response.get("error")
                known_errors = {"invalid_client", "invalid_scope", "invalid_grant", "unauthorized_client",
                                "access_denied", "temporarily_unavailable", "server_error", "interaction_required"}
                code = f"entra_{issue}" if isinstance(issue, str) and issue in known_errors else "entra_token_missing"
                diagnostics = {"issue_code": code,
                    "hint": "Check Entra app credentials, D365 application registration and API permissions; token response text is suppressed.",
                    **entra_diagnostics(response)}
                log_event("d365.auth.acquire", "failed", level=logging.ERROR, **diagnostics)
                raise IntegrationError("D365 authentication failed; check Entra credentials and permissions",
                                       retryable=False, log_diagnostics=diagnostics)
            self._token = token
            self._expires_at = time.monotonic() + max(0, int(response.get("expires_in", 300)) - 60)
            log_event("d365.auth.acquire", "completed", force_refresh=force_refresh)
            return token

    @traced("d365.request.mapping")
    def _body(self, operation: str, payload: dict[str, Any]) -> dict:
        mapping = dict(REQUEST_FIELDS)
        mapping.update(getattr(self.settings, "d365_request_field_maps", {}).get(operation, {}))
        mapped: dict[str, Any] = {}
        for key, value in payload.items():
            if key in mapping and not mapping[key]:
                continue
            if hasattr(value, "model_dump"):
                value = value.model_dump(mode="json", exclude_none=key == "linkedin_profile")
            if key in MEMO_FIELDS:
                value = json.dumps(_pascal_evidence(value), ensure_ascii=False, separators=(",", ":"))
            elif key == "linkedin_profile":
                # Preserve standard OIDC keys inside this memo independently of
                # the PascalCase evaluation evidence schema.
                try:
                    profile = LinkedInProfile.model_validate(value)
                except ValidationError as exc:
                    raise IntegrationError("Only permitted LinkedIn profile fields can be persisted", retryable=False) from exc
                value = json.dumps(profile.model_dump(mode="json", exclude_none=True), ensure_ascii=False, separators=(",", ":"))
            mapped[mapping.get(key, key)] = value
        wrappers = getattr(self.settings, "d365_request_wrappers", {})
        default = "" if operation == "get_open_requirements" else self.settings.d365_parameter_name
        wrapper = wrappers.get(operation, default)
        return {wrapper: mapped} if wrapper else mapped

    @traced("d365.operation")
    async def call_d365_service(self, operation: str, payload: dict[str, Any] | None = None) -> Any:
        log_event("d365.operation.config", "start", operation=operation)
        setting = PATHS.get(operation)
        if setting is None:
            log_event("d365.operation.config", "failed", level=logging.ERROR, issue_code="d365_operation_unsupported")
            raise ConfigurationError("Unsupported D365 operation")
        path = getattr(self.settings, setting, "")
        if not path:
            log_event("d365.operation.config", "failed", level=logging.ERROR,
                      operation=operation, issue_code="d365_operation_path_missing")
            raise ConfigurationError(f"{setting.upper()} is not configured; deploy or configure the D365 operation.")
        if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
            log_event("d365.operation.config", "failed", level=logging.ERROR,
                      operation=operation, issue_code="d365_operation_path_invalid")
            raise ConfigurationError(f"{setting.upper()} must be a relative service path beginning with '/'.")
        log_event("d365.operation.config", "completed", operation=operation, company=self.settings.d365_company)
        token = await self.get_access_token()
        correlation_id = str(uuid.uuid4())
        headers = {"Authorization": f"Bearer {token}", "X-Correlation-ID": correlation_id}
        payload = payload or {}
        if payload.get("idempotency_key"):
            headers["Idempotency-Key"] = str(payload["idempotency_key"])
        url = f"{self.settings.d365_base_url.rstrip('/')}{path}"

        async def perform(client: httpx.AsyncClient) -> Any:
            started = time.monotonic()
            try:
                log_event("d365.request.prepare", "start", operation=operation, correlation_id=correlation_id)
                try:
                    body = self._body(operation, payload)
                except IntegrationError:
                    log_event("d365.request.prepare", "failed", level=logging.ERROR, operation=operation,
                              correlation_id=correlation_id, issue_code="d365_request_mapping_invalid")
                    raise
                log_event("d365.request.prepare", "completed", operation=operation, correlation_id=correlation_id,
                          request_fields_count=len(payload))
                log_request_shape(operation, body)
                log_event("d365.http", "start", operation=operation, correlation_id=correlation_id, attempt=1)
                response = await client.post(url, json=body, headers=headers)
                log_event("d365.http", "received", operation=operation, correlation_id=correlation_id,
                          status_code=response.status_code, attempt=1, response_bytes=len(response.content),
                          elapsed_ms=round((time.monotonic() - started) * 1000, 2))
                if response.status_code == 401:
                    log_event("d365.auth.refresh", "start", operation=operation, correlation_id=correlation_id,
                              status_code=401, attempt=2)
                    headers["Authorization"] = f"Bearer {await self.get_access_token(force_refresh=True)}"
                    log_event("d365.auth.refresh", "completed", operation=operation, correlation_id=correlation_id)
                    log_event("d365.request.prepare", "start", operation=operation, correlation_id=correlation_id,
                              attempt=2)
                    body = self._body(operation, payload)
                    log_event("d365.request.prepare", "completed", operation=operation, correlation_id=correlation_id,
                              request_fields_count=len(payload), attempt=2)
                    log_request_shape(operation, body)
                    log_event("d365.http", "start", operation=operation, correlation_id=correlation_id, attempt=2)
                    response = await client.post(url, json=body, headers=headers)
                    log_event("d365.http", "received", operation=operation, correlation_id=correlation_id,
                              status_code=response.status_code, attempt=2, response_bytes=len(response.content),
                              elapsed_ms=round((time.monotonic() - started) * 1000, 2))
                if response.is_error:
                    flat_body = next(iter(body.values())) if len(body) == 1 and isinstance(next(iter(body.values())), dict) else body
                    sensitive_values = (self.settings.d365_client_secret, token, *payload.values(), *flat_body.values())
                    diagnostics = d365_error_diagnostics(response, sensitive_values=sensitive_values)
                    # Request shape is useful on failure at INFO; values never log.
                    log_request_shape(operation, body, level=logging.INFO)
                    log_event("d365.http", "failed", level=logging.ERROR, operation=operation,
                              status_code=response.status_code, correlation_id=correlation_id,
                              retryable=response.status_code in {408, 429} or response.status_code >= 500,
                              elapsed_ms=round((time.monotonic() - started) * 1000, 2),
                              **diagnostics)
                    raise IntegrationError(
                        f"D365 {operation} failed (HTTP {response.status_code}). Reference: {correlation_id}",
                        retryable=response.status_code in {408, 429} or response.status_code >= 500,
                        diagnostics={**{key: value for key, value in diagnostics.items() if key in {
                            "issue_code", "hint", "provider_message", "activity_id", "table", "field", "max_length",
                            "exception_type", "response_kind", "enum_symbol"}},
                            "upstream_status_code": response.status_code, "correlation_id": correlation_id},
                        log_diagnostics={**diagnostics, "upstream_status_code": response.status_code,
                                         "correlation_id": correlation_id},
                    )
                log_event("d365.http", "completed", operation=operation, correlation_id=correlation_id,
                          status_code=response.status_code, elapsed_ms=round((time.monotonic() - started) * 1000, 2))
                log_event("d365.response.decode", "start", operation=operation, correlation_id=correlation_id)
                data = response.json()
                if isinstance(data, str):
                    data = json.loads(data)
                log_event("d365.response.decode", "completed", operation=operation, correlation_id=correlation_id)
                log_event("d365.response.normalize", "start", operation=operation, correlation_id=correlation_id)
                try:
                    result = normalize(data, getattr(self.settings, "d365_response_field_maps", {}).get(operation, {}))
                except IntegrationError:
                    log_event("d365.response.normalize", "failed", level=logging.ERROR, operation=operation,
                              correlation_id=correlation_id, issue_code="d365_response_mapping_invalid")
                    raise
                log_event("d365.response.normalize", "completed", operation=operation, correlation_id=correlation_id)
                return result
            except (httpx.TimeoutException, httpx.RequestError) as exc:
                log_event("d365.http", "failed", level=logging.ERROR, operation=operation,
                          correlation_id=correlation_id, issue_code="d365_connection_failed",
                          elapsed_ms=round((time.monotonic() - started) * 1000, 2))
                raise IntegrationError(f"D365 {operation} connection failed. Reference: {correlation_id}",
                        log_diagnostics={**exception_diagnostics(exc), "correlation_id": correlation_id, "operation": operation}) from exc
            except (json.JSONDecodeError, TypeError) as exc:
                log_event("d365.response.decode", "failed", level=logging.ERROR, operation=operation,
                          correlation_id=correlation_id, issue_code="d365_response_json_invalid")
                raise IntegrationError("D365 returned an invalid JSON response", retryable=False,
                        log_diagnostics={"issue_code": "d365_invalid_json", "correlation_id": correlation_id,
                                         "hint": "The custom service did not return valid JSON; inspect its deployment and gateway response type."}) from exc

        if self.client is not None:
            return await perform(self.client)
        async with httpx.AsyncClient(timeout=getattr(self.settings, "d365_http_timeout", 30.0)) as client:
            return await perform(client)

    @staticmethod
    def _records(data: Any, key: str) -> list[dict]:
        records = data.get(key) if isinstance(data, dict) else data
        if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
            log_event("d365.response.records", "failed", level=logging.ERROR,
                      issue_code="d365_response_array_invalid")
            raise IntegrationError(f"D365 response must contain a {key} array", retryable=False)
        log_event("d365.response.records", "validated", record_count=len(records))
        return records

    @traced("d365.requirements")
    async def get_open_requirements(self) -> list[Job]:
        data = await self.call_d365_service("get_open_requirements", {})
        jobs = []
        for row in self._records(data, "requirements"):
            if str(row.get("status", "")).lower() != "open":
                continue
            deadline = row.get("deadline")
            if deadline:
                try:
                    parsed = datetime.fromisoformat(str(deadline).replace("Z", "+00:00"))
                    if parsed.replace(tzinfo=parsed.tzinfo or timezone.utc) < datetime.now(timezone.utc):
                        continue
                except ValueError as exc:
                    log_event("d365.requirements.validate", "failed", level=logging.ERROR,
                              issue_code="d365_job_deadline_invalid")
                    raise IntegrationError("D365 returned an invalid job deadline", retryable=False) from exc
            for key in ("required_skills", "preferred_skills", "required_certifications"):
                if isinstance(row.get(key), str):
                    row[key] = [part.strip() for part in re.split(r"[,;\n]", row[key]) if part.strip()]
            try:
                jobs.append(Job.model_validate(row))
            except ValidationError as exc:
                log_event("d365.requirements.validate", "failed", level=logging.ERROR,
                          issue_code="d365_job_fields_invalid",
                          hint="Check the F&O requirement contract mapping and mandatory job response fields.")
                raise IntegrationError("D365 job response is missing required fields", retryable=False) from exc
        log_event("d365.requirements", "validated", record_count=len(jobs))
        return jobs

    @traced("d365.application.acknowledgement")
    async def submit_application(self, payload: dict) -> dict:
        data = await self.call_d365_service("submit_application", {"company": self.settings.d365_company, **payload})
        if not isinstance(data, dict) or data.get("success") is not True:
            log_event("d365.application.acknowledgement", "failed", level=logging.ERROR,
                      issue_code="d365_application_not_acknowledged")
            raise IntegrationError("D365 did not confirm application creation", retryable=False)
        application_id = data.get("application_id")
        if not isinstance(application_id, (str, int)) or not str(application_id).strip():
            log_event("d365.application.acknowledgement", "failed", level=logging.ERROR,
                      issue_code="d365_application_id_missing")
            raise IntegrationError("D365 submitApplication must return a stable ApplicationId; evaluation cannot be linked without it", retryable=False)
        return {**data, "application_id": str(application_id)}

    @traced("d365.evaluation.acknowledgement")
    async def save_candidate_evaluation(self, payload: dict) -> dict:
        application_id = payload.get("application_id")
        if not application_id:
            log_event("d365.evaluation.validate", "failed", level=logging.ERROR,
                      issue_code="d365_application_id_missing")
            raise IntegrationError("An ApplicationId is required to save an evaluation", retryable=False)
        nested = payload.get("evaluation")
        if nested is None:
            nested = {key: value for key, value in payload.items() if key in Evaluation.model_fields}
        if hasattr(nested, "model_dump"):
            nested = nested.model_dump(mode="json")
        try:
            evaluation = Evaluation.model_validate(nested)
        except ValidationError as exc:
            log_event("d365.evaluation.validate", "failed", level=logging.ERROR,
                      issue_code="d365_evaluation_fields_invalid")
            raise IntegrationError("Only validated evaluations can be saved to D365", retryable=False) from exc
        if not evaluation.model_deployment.strip() or not evaluation.prompt_version.strip():
            log_event("d365.evaluation.validate", "failed", level=logging.ERROR,
                      issue_code="d365_evaluation_provenance_missing")
            raise IntegrationError("New evaluation writes require the actual model deployment and prompt version", retryable=False)
        data = await self.call_d365_service("save_candidate_evaluation", {
            "company": self.settings.d365_company, "application_id": application_id,
            "idempotency_key": f"evaluation:{application_id}", **evaluation.model_dump(mode="json"),
            **({"evaluation_date": payload["evaluation_date"]} if payload.get("evaluation_date") else {}),
            **({"requirement_id": payload["requirement_id"]} if payload.get("requirement_id") else {}),
        })
        if not isinstance(data, dict) or data.get("success") is not True:
            log_event("d365.evaluation.acknowledgement", "failed", level=logging.ERROR,
                      issue_code="d365_evaluation_not_acknowledged")
            raise IntegrationError("D365 did not confirm evaluation persistence", retryable=False)
        if str(data.get("application_id", "")) != str(application_id):
            log_event("d365.evaluation.acknowledgement", "failed", level=logging.ERROR,
                      issue_code="d365_application_id_mismatch")
            raise IntegrationError("D365 evaluation acknowledgement has the wrong ApplicationId", retryable=False)
        return data

    @staticmethod
    def _candidate(row: dict) -> Candidate:
        # A flattened custom-service row is composed into the typed internal model.
        row = dict(row)
        if row.get("score") is not None and not row.get("evaluation"):
            row["evaluation"] = {key: row[key] for key in Evaluation.model_fields if key in row}
        # Normalizing JobTitle to title is job-specific; restore application label here.
        if "title" in row and "job_title" not in row:
            row["job_title"] = row["title"]
        try:
            return Candidate.model_validate(row)
        except ValidationError as exc:
            raise IntegrationError("D365 candidate response is incomplete or invalid", retryable=False) from exc

    @traced("d365.candidates.ranking")
    async def get_top_candidates(self, requirement_id: str, limit: int = 25) -> list[Candidate]:
        data = await self.call_d365_service("get_top_candidates", {
            "company": self.settings.d365_company, "requirement_id": requirement_id, "limit": limit,
        })
        candidates = [self._candidate(row) for row in self._records(data, "candidates")]
        if any(candidate.requirement_id != requirement_id for candidate in candidates):
            raise IntegrationError("D365 returned candidates for a different requirement", retryable=False)
        return sorted(candidates, key=lambda c: c.evaluation.score if c.evaluation else -1, reverse=True)[:limit]

    @traced("d365.applications.history")
    async def get_my_applications(self, user: User) -> list[Candidate]:
        data = await self.call_d365_service("get_my_applications", {
            "company": self.settings.d365_company, "owner_issuer": user.issuer, "owner_sub": user.sub,
        })
        result = []
        for row in self._records(data, "applications"):
            candidate = self._candidate(row)
            if candidate.owner_sub != user.sub or candidate.owner_issuer != user.issuer:
                raise IntegrationError("D365 application history did not establish candidate ownership", retryable=False)
            result.append(candidate)
        return result

    @traced("d365.application.detail")
    async def get_application(self, application_id: str) -> Candidate:
        data = await self.call_d365_service("get_application", {
            "company": self.settings.d365_company, "application_id": application_id,
        })
        if not isinstance(data, dict):
            raise IntegrationError("D365 returned an invalid application", retryable=False)
        candidate = self._candidate(data.get("application", data))
        if candidate.application_id != application_id:
            raise IntegrationError("D365 returned a different application identifier", retryable=False)
        return candidate


def _pascal_evidence(value: Any) -> Any:
    """Nested memo values use the proposed X++ evidence schema."""
    if isinstance(value, list):
        return [_pascal_evidence(item) for item in value]
    if isinstance(value, dict):
        return {"".join(word.capitalize() for word in key.split("_")): _pascal_evidence(item) for key, item in value.items()}
    return value
