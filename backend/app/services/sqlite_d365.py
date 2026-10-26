"""Screenshot-table F&O writes with durable portal metadata in SQLite.

Only job lookup, application creation and evaluation persistence call F&O. Portal
history/ranking comes from this application's acknowledged local snapshots.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from app.errors import IntegrationError
from app.metadata import metadata_store
from app.models import Candidate, Evaluation, User
from app.observability import log_event, traced
from app.services.d365 import D365Service, MEMO_FIELDS, _pascal_evidence

LEAN_REQUEST_FIELDS = {
    "company": "Company", "application_id": "ApplicationId", "requirement_id": "RequirementId",
    "owner_issuer": "LinkedInIssuer", "owner_sub": "LinkedInSub", "candidate_name": "CandidateName",
    "email": "Email", "phone": "Phone", "linkedin_url": "LinkedInProfileUrl",
    "evaluation_id": "EvaluationId", "score": "Score", "meets_minimum_criteria": "MeetsMinimumCriteria",
    "match_label": "MatchLabel", "summary": "Summary", "matched_requirements": "MatchedSkillsJson",
    "not_evidenced_requirements": "MissingSkillsJson", "relevant_evidence": "EvidenceJson",
}
LEAN_SUBMIT_FIELDS = {"company", "application_id", "requirement_id", "owner_issuer", "owner_sub", "candidate_name",
                      "email", "phone", "linkedin_url"}
LEAN_EVALUATION_FIELDS = {"company", "application_id", "requirement_id", "evaluation_id", "score",
                          "meets_minimum_criteria", "match_label", "summary", "matched_requirements",
                          "not_evidenced_requirements", "relevant_evidence"}


class SQLiteD365Service(D365Service):
    def __init__(self, settings, *, metadata=None, **kwargs):
        super().__init__(settings, **kwargs)
        self.metadata = metadata or metadata_store(settings)

    @traced("d365.request.mapping")
    def _body(self, operation: str, payload: dict[str, Any]) -> dict:
        import json
        if operation == "get_open_requirements":
            return {}
        allowed = LEAN_SUBMIT_FIELDS if operation == "submit_application" else LEAN_EVALUATION_FIELDS
        if operation not in {"submit_application", "save_candidate_evaluation"}:
            raise IntegrationError("The SQLite D365 contract supports only three operations", retryable=False)
        mapped = {}
        overrides = self.settings.d365_request_field_maps.get(operation, {})
        for key in allowed:
            if key not in payload:
                continue
            value = payload[key]
            if key in MEMO_FIELDS:
                value = json.dumps(_pascal_evidence(value), ensure_ascii=False, separators=(",", ":"))
            external = overrides.get(key, LEAN_REQUEST_FIELDS[key])
            if external:
                mapped[external] = value
        wrapper = self.settings.d365_request_wrappers.get(operation, self.settings.d365_parameter_name)
        return {wrapper: mapped} if wrapper else mapped

    async def call_d365_service(self, operation: str, payload=None):
        # Also allow the screenshot's authoritative status/date spellings on reads.
        result = await super().call_d365_service(operation, payload)
        if operation == "get_open_requirements" and isinstance(result, dict):
            for row in self._records(result, "requirements"):
                if "requirement_status" in row:
                    row["status"] = row["requirement_status"]
                if "close_date" in row and "deadline" not in row:
                    date = row["close_date"]
                    row["deadline"] = f"{date}T23:59:59Z" if date and len(str(date)) == 10 else date
                if "open_date" in row and "published_date" not in row:
                    row["published_date"] = row["open_date"]
        return result

    @traced("d365.application.acknowledgement")
    async def submit_application(self, payload: dict) -> dict:
        application_id = payload.get("application_id")
        record = await self.metadata.get_application(str(application_id or ""), self.settings.d365_company)
        if not record or record["payload"] != payload:
            log_event("d365.application.acknowledgement", "rejected", issue_code="frozen_submission_missing")
            raise IntegrationError("A frozen SQLite submission is required before calling D365", retryable=False)
        if record.get("candidate_id") and record["stage"] in {"accepted", "queued"}:
            log_event("d365.application.acknowledgement", "reused", application_id=application_id, cached=True)
            return {"success": True, "application_id": application_id, "candidate_id": record["candidate_id"]}
        lean = {key: payload[key] for key in ("application_id", "requirement_id", "owner_issuer", "owner_sub",
                                              "candidate_name", "email", "phone", "linkedin_url")}
        lean.update(company=self.settings.d365_company)
        data = await self.call_d365_service("submit_application", lean)
        if not isinstance(data, dict) or data.get("success") is not True:
            log_event("d365.application.acknowledgement", "rejected", issue_code="application_not_confirmed")
            raise IntegrationError("D365 did not confirm application creation", retryable=False)
        if data.get("application_id") != application_id:
            log_event("d365.application.acknowledgement", "rejected", issue_code="application_id_mismatch")
            raise IntegrationError("D365 acknowledgement has the wrong ApplicationId", retryable=False)
        candidate_id = data.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            log_event("d365.application.acknowledgement", "rejected", issue_code="candidate_id_missing")
            raise IntegrationError("D365 must return its stable CandidateId", retryable=False)
        log_event("d365.application.acknowledgement", "validated", application_id=application_id, confirmed=True)
        return data

    @traced("d365.evaluation.acknowledgement")
    async def save_candidate_evaluation(self, payload: dict) -> dict:
        application_id = str(payload.get("application_id", ""))
        record = await self.metadata.get_application(application_id, self.settings.d365_company)
        if not record or record["stage"] not in {"accepted", "queued"} or not record.get("candidate_id"):
            log_event("d365.evaluation.acknowledgement", "rejected", issue_code="application_not_acknowledged")
            raise IntegrationError("An acknowledged SQLite application is required for evaluation", retryable=False)
        requirement_id = record["requirement_id"]
        if payload.get("requirement_id", requirement_id) != requirement_id:
            log_event("d365.evaluation.acknowledgement", "rejected", issue_code="requirement_id_mismatch")
            raise IntegrationError("Evaluation refers to a different job requirement", retryable=False)
        try:
            evaluation = Evaluation.model_validate(payload.get("evaluation"))
        except ValidationError as exc:
            log_event("d365.evaluation.acknowledgement", "rejected", issue_code="evaluation_schema_invalid")
            raise IntegrationError("Only validated evaluations can be saved to D365", retryable=False) from exc
        if not evaluation.model_deployment.strip() or not evaluation.prompt_version.strip():
            log_event("d365.evaluation.acknowledgement", "rejected", issue_code="evaluation_provenance_missing")
            raise IntegrationError("New evaluation writes require the actual model deployment and prompt version",
                                   retryable=False)
        evaluation_id = "EVA-" + (
            application_id[4:] if application_id.startswith("APP-") and len(application_id) == 40
            else hashlib.sha256(application_id.encode()).hexdigest()[:36]
        )
        state = await self.metadata.get_evaluation_state(application_id) or {}
        state.update(evaluation=evaluation.model_dump(mode="json"), evaluation_id=evaluation_id,
                     evaluation_date=payload.get("evaluation_date") or state.get("evaluation_date")
                     or datetime.now(timezone.utc).isoformat(),
                     status="Processing", evaluation_confirmed=False)
        await self.metadata.save_evaluation_state(application_id, state)
        values = evaluation.model_dump(mode="json")
        lean = {key: values[key] for key in LEAN_EVALUATION_FIELDS if key in values}
        # F&O has one supported-skills JSON column. Keep partial evidence there
        # with its EvidenceStrength, while SQLite retains the separate lists.
        lean["matched_requirements"] = values["matched_requirements"] + values["partially_matched_requirements"]
        lean.update(company=self.settings.d365_company, application_id=application_id,
                    requirement_id=requirement_id, evaluation_id=evaluation_id)
        data = await self.call_d365_service("save_candidate_evaluation", lean)
        if not isinstance(data, dict) or data.get("success") is not True:
            log_event("d365.evaluation.acknowledgement", "rejected", issue_code="evaluation_not_confirmed")
            raise IntegrationError("D365 did not confirm evaluation persistence", retryable=False)
        if data.get("application_id") != application_id or data.get("evaluation_id") != evaluation_id:
            log_event("d365.evaluation.acknowledgement", "rejected", issue_code="evaluation_ids_mismatch")
            raise IntegrationError("D365 evaluation acknowledgement has the wrong identifiers", retryable=False)
        state.update(status="Completed", evaluation_confirmed=True)
        state.pop("error_code", None)
        await self.metadata.save_evaluation_state(application_id, state)
        log_event("d365.evaluation.acknowledgement", "validated", application_id=application_id,
                  evaluation_id=evaluation_id, confirmed=True)
        return data

    @traced("sqlite.application.validate")
    async def _from_record(self, record: dict) -> Candidate:
        if record["stage"] not in {"accepted", "queued"} or not record.get("candidate_id"):
            raise IntegrationError("Application has not been acknowledged by D365", status_code=404, retryable=False)
        payload = record["payload"]
        state = await self.metadata.get_evaluation_state(record["application_id"]) or {}
        try:
            return Candidate(
                application_id=record["application_id"], candidate_id=record["candidate_id"],
                requirement_id=record["requirement_id"], job_title=record["job_snapshot"]["title"],
                candidate_name=payload["candidate_name"], email=payload["email"],
                applied_date=record["created_at"], hr_status=record.get("hr_status", "Submitted"),
                ai_status=state.get("status", "Pending"), resume_file_name=record["resume_file_name"],
                linkedin_url=payload["linkedin_url"], owner_sub=payload["owner_sub"],
                owner_issuer=payload["owner_issuer"], resume_ref=record["resume_ref"],
                evaluation=state.get("evaluation") if state.get("evaluation_confirmed") is True else None,
                evaluation_date=state.get("evaluation_date") if state.get("evaluation_confirmed") is True else None,
            )
        except (KeyError, ValidationError) as exc:
            raise IntegrationError("SQLite application metadata is incomplete", retryable=False) from exc

    @traced("sqlite.application.detail")
    async def get_application(self, application_id: str) -> Candidate:
        record = await self.metadata.get_application(application_id, self.settings.d365_company)
        if record is None:
            raise IntegrationError("Application not found in SQLite metadata", status_code=404, retryable=False)
        return await self._from_record(record)

    @traced("sqlite.applications.history")
    async def get_my_applications(self, user: User) -> list[Candidate]:
        rows = await self.metadata.submissions(company=self.settings.d365_company, owner=(user.issuer, user.sub))
        return [await self._from_record(record) for _, record in rows if record["stage"] in {"accepted", "queued"}]

    @traced("sqlite.candidates.ranking")
    async def get_top_candidates(self, requirement_id: str, limit: int = 25) -> list[Candidate]:
        rows = await self.metadata.submissions(company=self.settings.d365_company, requirement_id=requirement_id)
        records = [await self._from_record(record) for _, record in rows if record["stage"] in {"accepted", "queued"}]
        return sorted(records, key=lambda c: c.evaluation.score if c.evaluation else -1, reverse=True)[:limit]


def connected_adapter(settings):
    return SQLiteD365Service(settings) if settings.d365_contract_mode == "sqlite" else D365Service(settings)
