import hashlib
import json
import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from arq import Retry
from fastapi import HTTPException

from app.config import Settings
from app.demo import DemoAdapter, sample_evaluation
from app.errors import IntegrationError
from app.metadata import SQLiteMetadata
from app.models import LinkedInProfile, User
from app.queue import SubmissionCoordinator, submission_fingerprint
from app.retry_evaluation import retry
from app.services.sqlite_d365 import SQLiteD365Service
from app.worker import evaluate_application


class Queue:
    def __init__(self):
        self.values, self.jobs = {}, {}
        self.fail_enqueue = self.fail_cache = False

    def lock(self, *args, **kwargs):
        return Mock(acquire=AsyncMock(return_value=True), release=AsyncMock())

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, **kwargs):
        if self.fail_cache:
            raise ConnectionError("queue unavailable")
        self.values[key] = value

    async def enqueue_job(self, name, *args, **kwargs):
        if self.fail_enqueue:
            raise ConnectionError("queue unavailable")
        self.jobs["arq:job:" + kwargs["_job_id"]] = args

    async def exists(self, *keys):
        return sum(key in self.jobs or key in self.values for key in keys)

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
            self.jobs.pop(key, None)

    async def aclose(self):
        pass


@pytest.fixture
async def context(tmp_path):
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        payload = body.get("_request", {})
        if request.url.path == "/jobs":
            return httpx.Response(200, json={"Requirements": [
                {"RequirementId": "REQ-1", "JobTitle": "Engineer", "RequirementStatus": "Open",
                 "JobDescription": "Build Python APIs", "RequiredSkills": "Python; SQL",
                 "OpenDate": "2026-01-01", "CloseDate": "2099-01-01",
                 "EvaluationCriteriaJson": '{"must_have":["Python"]}', "RubricVersion": "v2"},
                {"RequirementId": "REQ-2", "JobTitle": "Closed", "RequirementStatus": "Closed"},
            ]})
        response = {"Success": True, "ApplicationId": payload["ApplicationId"]}
        if request.url.path == "/submit":
            response["CandidateId"] = "CAN-00000000-0000-0000-0000-000000000001"
        else:
            response["EvaluationId"] = payload["EvaluationId"]
        return httpx.Response(200, json=response)
    settings = Settings(_env_file=None, demo_mode=False, d365_enabled=True,
        sqlite_database_path=tmp_path / "metadata.sqlite3", d365_requirement_path="/jobs",
        d365_submit_application_path="/submit", d365_save_evaluation_path="/save",
        d365_tenant_id="tenant", d365_client_id="client", d365_client_secret="test-only")
    msal = Mock(acquire_token_for_client=Mock(return_value={"access_token": "test-only", "expires_in": 3600}))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SQLiteD365Service(settings, client=client, msal_app=msal)
        queue = Queue()
        resumes = Mock(store=AsyncMock(return_value="azure:" + "a" * 32 + ".pdf"),
            read=AsyncMock(return_value=b"valid-resume"), delete=AsyncMock(),
            extract=Mock(return_value="Projects: Built Python APIs"))
        user = User(sub="subject", issuer="issuer", name="Confirmed Applicant", email="confirmed@example.test",
            role="candidate", linkedin_profile=LinkedInProfile(sub="subject", issuer="issuer",
            name="Provider Applicant", picture="https://example.test/avatar.png"))
        ctx = {"settings": settings, "adapter": adapter, "redis": queue, "resumes": resumes,
            "ai": Mock(evaluate=AsyncMock(return_value=sample_evaluation())), "calls": calls,
            "user": user, "job": DemoAdapter().jobs[0], "job_try": 1}
        ctx["coordinator"] = SubmissionCoordinator(settings, adapter, resumes, queue)
        yield ctx


async def submit(ctx, data=b"valid-resume", key="submission-key"):
    user = ctx["user"]
    return await ctx["coordinator"].submit(user, key,
        submission_fingerprint(ctx["job"].requirement_id, data, "555", "", "v1",
                               candidate_name=user.name, email=user.email),
        ctx["job"], data, "resume.pdf", "555", "", "v1")


async def test_minimal_wire_private_metadata_and_stable_submission_survive_restart(context):
    ctx = context
    app_id = (await submit(ctx))["application_id"]
    assert len(app_id) == 40 and app_id.startswith("APP-")
    wire = ctx["calls"][0][1]["_request"]
    assert set(wire) == {"Company", "ApplicationId", "RequirementId", "CandidateName", "Email", "Phone",
        "LinkedInProfileUrl", "LinkedInIssuer", "LinkedInSub"}
    assert wire["CandidateName"] == "Confirmed Applicant"
    ctx["redis"].values.clear()
    ctx["redis"].jobs.clear()
    ctx["adapter"].metadata = SQLiteMetadata(ctx["settings"].sqlite_database_path)
    ctx["coordinator"] = SubmissionCoordinator(ctx["settings"], ctx["adapter"], ctx["resumes"], ctx["redis"])
    assert (await submit(ctx))["application_id"] == app_id
    assert len(ctx["calls"]) == 1
    ctx["resumes"].store.assert_awaited_once()
    record = await ctx["adapter"].metadata.get_application(app_id)
    assert record["payload"]["linkedin_profile"]["picture"] == "https://example.test/avatar.png"
    assert record["payload"]["resume_sha256"] == hashlib.sha256(b"valid-resume").hexdigest()
    assert record["payload"]["consent_accepted"] is True and len(record["payload_sha256"]) == 64
    with pytest.raises(HTTPException) as error:
        await submit(ctx, b"different-resume")
    assert error.value.status_code == 409
    own = await ctx["adapter"].get_my_applications(ctx["user"])
    assert own[0].owner_sub == "subject" and own[0].resume_ref == record["resume_ref"]
    assert await ctx["adapter"].get_my_applications(ctx["user"].model_copy(update={"sub": "other"})) == []
    assert (await ctx["adapter"].get_top_candidates(ctx["job"].requirement_id))[0].application_id == app_id
    assert await ctx["adapter"].get_top_candidates("other-requirement") == []
    old_key = ctx["coordinator"].key(ctx["user"], "submission-key")
    ctx["settings"].d365_company = "USMF"
    assert await ctx["adapter"].get_my_applications(ctx["user"]) == []
    assert ctx["coordinator"].key(ctx["user"], "submission-key") != old_key


@pytest.mark.parametrize("full_name", [
    "Cher", "María del Carmen García", "王 小明", "Alexandria " * 17 + "Applicant",
])
async def test_confirmed_full_name_reaches_candidate_name_without_splitting_or_profile_substitution(context, full_name, caplog):
    ctx = context
    ctx["user"] = ctx["user"].model_copy(update={"name": full_name})
    result = await submit(ctx)
    wire = ctx["calls"][0][1]["_request"]
    assert wire["CandidateName"] == full_name
    assert not {"FirstName", "LastName", "FullName"} & set(wire)
    record = await ctx["adapter"].metadata.get_application(result["application_id"])
    assert record["payload"]["candidate_name"] == full_name
    assert record["payload"]["linkedin_profile"]["name"] == "Provider Applicant"
    assert full_name not in caplog.text


async def test_unknown_ack_retries_identical_application_id_after_redis_loss(context, monkeypatch):
    ctx = context
    original = ctx["adapter"].call_d365_service
    calls = []
    async def interrupted(operation, payload=None):
        calls.append(dict(payload))
        if len(calls) == 1:
            raise IntegrationError("Acknowledgement lost")
        return await original(operation, payload)
    monkeypatch.setattr(ctx["adapter"], "call_d365_service", interrupted)
    with pytest.raises(IntegrationError):
        await submit(ctx)
    ctx["redis"].values.clear()
    await ctx["coordinator"].reconcile()
    assert calls[0] == calls[1] and len(calls[0]["application_id"]) == 40
    ctx["resumes"].store.assert_awaited_once()
    record = await ctx["coordinator"].load(ctx["coordinator"].key(ctx["user"], "submission-key"))
    assert record["stage"] == "queued"


async def test_sqlite_commit_precedes_queue_failure_and_blob_is_retained(context):
    ctx = context
    ctx["redis"].fail_cache = ctx["redis"].fail_enqueue = True
    with pytest.raises(ConnectionError):
        await submit(ctx)
    record = await ctx["coordinator"].load(ctx["coordinator"].key(ctx["user"], "submission-key"))
    assert record["stage"] == "accepted"
    ctx["resumes"].delete.assert_not_awaited()
    ctx["redis"].fail_cache = ctx["redis"].fail_enqueue = False
    await ctx["coordinator"].reconcile()
    assert (await ctx["coordinator"].load(ctx["coordinator"].key(ctx["user"], "submission-key")))["stage"] == "queued"
    assert len(ctx["calls"]) == 1


async def test_cached_full_evaluation_survives_redis_loss_and_minimal_d365_retry(context, monkeypatch):
    ctx = context
    app_id = (await submit(ctx))["application_id"]
    args = ctx["redis"].jobs["arq:job:evaluation:" + app_id]
    original = ctx["adapter"].call_d365_service
    attempts = []
    async def fail_first(operation, payload=None):
        if operation == "save_candidate_evaluation":
            attempts.append(ctx["adapter"]._body(operation, payload))
            if len(attempts) == 1:
                raise IntegrationError("Temporary write failure")
        return await original(operation, payload)
    monkeypatch.setattr(ctx["adapter"], "call_d365_service", fail_first)
    with pytest.raises(Retry):
        await evaluate_application(ctx, *args)
    state = await ctx["adapter"].metadata.get_evaluation_state(app_id)
    assert state["evaluation"]["score_justification"] and state["status"] == "Retrying"
    assert (await ctx["adapter"].get_application(app_id)).evaluation is None
    ctx["redis"].values.clear()
    ctx["adapter"].metadata = SQLiteMetadata(ctx["settings"].sqlite_database_path)
    ctx["job_try"] = 2
    assert await evaluate_application(ctx, *args) == {"status": "completed"}
    assert attempts[0] == attempts[1]
    assert set(attempts[0]["_request"]) == {"Company", "EvaluationId", "ApplicationId", "RequirementId", "Score",
        "MeetsMinimumCriteria", "MatchLabel", "Summary", "MatchedSkillsJson", "MissingSkillsJson", "EvidenceJson"}
    ctx["ai"].evaluate.assert_awaited_once()
    assert (await ctx["adapter"].get_application(app_id)).evaluation.score == 88
    assert await evaluate_application(ctx, *args) == {"status": "already_completed"}
    assert (await submit(ctx))["ai_status"] == "Completed"


async def test_operator_retry_uses_sqlite_cached_output_after_redis_flush(context, monkeypatch):
    ctx = context
    app_id = (await submit(ctx))["application_id"]
    state = {"status": "Failed", "evaluation": sample_evaluation().model_dump(), "evaluation_confirmed": False}
    await ctx["adapter"].metadata.save_evaluation_state(app_id, state)
    ctx["redis"].values.clear()
    ctx["redis"].jobs.clear()
    monkeypatch.setattr("app.retry_evaluation.create_pool", AsyncMock(return_value=ctx["redis"]))
    assert "queued" in await retry(app_id, ctx["settings"])
    retained = await ctx["adapter"].metadata.get_evaluation_state(app_id)
    assert retained["evaluation"] == state["evaluation"] and retained["status"] == "Pending"


async def test_hr_retry_scopes_job_and_queues_failed_cached_evaluation_once(context):
    from app.auth import hr_user, require_csrf
    from app.main import create_app
    ctx = context
    app_id = (await submit(ctx))["application_id"]
    ctx["redis"].jobs.clear()
    await ctx["adapter"].metadata.save_evaluation_state(app_id, {
        "status": "Failed", "evaluation": sample_evaluation().model_dump(), "evaluation_confirmed": False})
    app = create_app(ctx["settings"])
    app.state.adapter, app.state.redis = ctx["adapter"], ctx["redis"]
    app.dependency_overrides[hr_user] = lambda: ctx["user"].model_copy(update={"role": "hr"})
    app.dependency_overrides[require_csrf] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post(f"/api/hr/jobs/OTHER/candidates/{app_id}/retry")).status_code == 404
        route = f"/api/hr/jobs/{ctx['job'].requirement_id}/candidates/{app_id}/retry"
        response = await client.post(route)
        assert response.status_code == 200 and response.json()["ai_status"] == "Pending"
        assert (await client.post(route)).status_code == 409
    retained = await ctx["adapter"].metadata.get_evaluation_state(app_id)
    assert retained["evaluation"]["score"] == 88 and len(ctx["redis"].jobs) == 1


async def test_retry_queue_outage_keeps_failed_state_and_cached_evidence(context):
    ctx = context
    app_id = (await submit(ctx))["application_id"]
    ctx["redis"].jobs.clear()
    state = {"status": "Failed", "evaluation": sample_evaluation().model_dump(), "error_code": "integration_error"}
    await ctx["adapter"].metadata.save_evaluation_state(app_id, state)
    ctx["redis"].fail_enqueue = True
    with pytest.raises(IntegrationError, match="Could not queue"):
        await retry(app_id, ctx["settings"], redis=ctx["redis"], metadata=ctx["adapter"].metadata, failed_only=True)
    assert await ctx["adapter"].metadata.get_evaluation_state(app_id) == state


async def test_shared_database_evaluation_lock_prevents_duplicate_ai(context, monkeypatch):
    ctx = context
    app_id = (await submit(ctx))["application_id"]
    @asynccontextmanager
    async def busy(application_id):
        assert application_id == app_id
        yield False
    monkeypatch.setattr(ctx["adapter"].metadata, "evaluation_lock", busy)
    args = ctx["redis"].jobs["arq:job:evaluation:" + app_id]
    with pytest.raises(Retry):
        await evaluate_application(ctx, *args)
    ctx["ai"].evaluate.assert_not_awaited()


async def test_interrupted_worker_retains_actionable_failed_state(context):
    ctx = context
    app_id = (await submit(ctx))["application_id"]
    args = ctx["redis"].jobs["arq:job:evaluation:" + app_id]
    ctx["ai"].evaluate.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await evaluate_application(ctx, *args)
    state = await ctx["adapter"].metadata.get_evaluation_state(app_id)
    assert state["status"] == "Failed" and state["error_code"] == "processing_interrupted"


async def test_worker_rejects_altered_frozen_references_before_ai(context):
    ctx = context
    app_id = (await submit(ctx))["application_id"]
    args = list(ctx["redis"].jobs["arq:job:evaluation:" + app_id])
    args[1] = "azure:" + "b" * 32 + ".pdf"
    with pytest.raises(IntegrationError, match="frozen SQLite snapshot"):
        await evaluate_application(ctx, *args)
    ctx["ai"].evaluate.assert_not_awaited()
    ctx["resumes"].read.assert_not_awaited()


async def test_job_screenshot_status_dates_and_scoring_criteria(context):
    jobs = await context["adapter"].get_open_requirements()
    assert len(jobs) == 1 and jobs[0].required_skills == ["Python", "SQL"]
    assert jobs[0].deadline == "2099-01-01T23:59:59Z"
    assert jobs[0].evaluation_criteria == {"must_have": ["Python"]}
    assert jobs[0].rubric_version == "v2"


async def test_api_frozen_retry_can_finish_after_job_closes(context, monkeypatch):
    from app.auth import candidate_user, require_csrf
    from app.main import create_app
    ctx = context
    app_id = (await submit(ctx))["application_id"]
    app = create_app(ctx["settings"])
    app.state.adapter, app.state.coordinator, app.state.resumes = ctx["adapter"], ctx["coordinator"], ctx["resumes"]
    app.dependency_overrides[candidate_user] = lambda: ctx["user"]
    app.dependency_overrides[require_csrf] = lambda: None
    closed_lookup = AsyncMock(return_value=[])
    monkeypatch.setattr(ctx["adapter"], "get_open_requirements", closed_lookup)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        fields = {"requirement_id": ctx["job"].requirement_id, "candidate_name": ctx["user"].name,
                  "email": ctx["user"].email, "phone": "555", "consent_accepted": "true", "consent_version": "v1"}
        response = await client.post("/api/applications", data=fields,
            files={"resume": ("resume.pdf", b"valid-resume", "application/pdf")},
            headers={"Idempotency-Key": "submission-key"})
        assert response.status_code == 200 and response.json()["application_id"] == app_id
        changed = await client.post("/api/applications", data=fields,
            files={"resume": ("resume.pdf", b"changed-resume", "application/pdf")},
            headers={"Idempotency-Key": "submission-key"})
        assert changed.status_code == 409
    closed_lookup.assert_not_awaited()


async def test_wrong_acknowledgements_do_not_establish_a_remote_binding(context, monkeypatch):
    ctx = context
    async def wrong(operation, payload=None):
        return {"success": True, "application_id": "OTHER-APP", "candidate_id": "CAN-OTHER"}
    monkeypatch.setattr(ctx["adapter"], "call_d365_service", wrong)
    with pytest.raises(IntegrationError, match="wrong ApplicationId"):
        await submit(ctx)
    record = await ctx["coordinator"].load(ctx["coordinator"].key(ctx["user"], "submission-key"))
    assert record["stage"] == "ready" and not record.get("candidate_id")
    assert await ctx["adapter"].get_my_applications(ctx["user"]) == []


async def test_company_scoped_submission_keys_do_not_return_other_legal_entity(context):
    ctx = context
    first = (await submit(ctx))["application_id"]
    ctx["settings"].d365_company = "USMF"
    second = (await submit(ctx))["application_id"]
    assert first != second
    rows = await ctx["adapter"].get_my_applications(ctx["user"])
    assert len(rows) == 1 and rows[0].application_id == second
