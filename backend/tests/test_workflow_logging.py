"""Operational workflow logs must identify failures without exposing applicants."""

import json
import logging
from unittest.mock import AsyncMock, Mock

import pytest
from arq import Retry

from app.config import Settings
from app.demo import DemoAdapter, sample_evaluation
from app.errors import IntegrationError
from app.models import User
from app.observability import log_context, log_event
from app.queue import SubmissionCoordinator, submission_fingerprint
from app.worker import SafeArqLogs, evaluate_application


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.lock_acquired = True

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, **kwargs):
        self.values[key] = value

    def lock(self, key, **kwargs):
        return Mock(acquire=AsyncMock(return_value=self.lock_acquired), release=AsyncMock())


def events(caplog):
    return [record.workflow_event for record in caplog.records if hasattr(record, "workflow_event")]


async def test_submission_failure_marks_d365_stage_and_keeps_frozen_outbox(caplog):
    caplog.set_level(logging.INFO, logger="app.workflow")
    settings = Settings(_env_file=None, demo_mode=False, d365_contract_mode="legacy")
    adapter = Mock(submit_application=AsyncMock(side_effect=IntegrationError("secret applicant@example.test",
        log_diagnostics={"issue_code": "field_length_or_missing", "upstream_status_code": 500,
                         "activity_id": "cb035259-4dd8-0000-ec66-bed5d84ddd01"})))
    resumes = Mock(store=AsyncMock(return_value="azure:private-resume-reference"), delete=AsyncMock())
    coordinator = SubmissionCoordinator(settings, adapter, resumes)
    user = User(sub="private-sub", issuer="private-issuer", name="Private Applicant",
                email="applicant@example.test", role="candidate")
    job = DemoAdapter().jobs[0]
    args = (user, "private-idempotency-key",
            submission_fingerprint(job.requirement_id, b"private resume", "private phone", "", "v1"),
            job, b"private resume", "private-candidate.pdf", "private phone", "", "v1")
    with log_context(request_id="c58c3df3-5dac-4a10-a7e9-6e8cf61ac324"):
        with pytest.raises(IntegrationError):
            await coordinator.submit(*args)
        log_event("test.context_after_submit", "completed")
    record = await coordinator.load(coordinator.key(user, "private-idempotency-key"))
    assert record["stage"] == "ready"
    adapter.submit_application.side_effect = None
    adapter.submit_application.return_value = {"application_id": "APP-logging-test"}
    with log_context(request_id="c58c3df3-5dac-4a10-a7e9-6e8cf61ac324"):
        assert (await coordinator.submit(*args))["application_id"] == "APP-logging-test"
    resumes.store.assert_awaited_once()
    stages = {(event["stage"], event["outcome"]) for event in events(caplog)}
    assert ("submission.resume_store", "completed") in stages
    assert ("submission.d365_submit", "start") in stages
    assert ("submission.d365_submit", "failed") in stages
    assert ("submission.finish", "failed") in stages
    assert ("submission.lock_release", "completed") in stages
    acknowledgement = next(event for event in events(caplog)
                           if event["stage"] == "submission.application_acknowledgement")
    assert acknowledgement["application_id"] == "APP-logging-test"
    failure = next(event for event in events(caplog)
                   if event["stage"] == "submission.d365_submit" and event["outcome"] == "failed")
    assert failure["elapsed_ms"] >= 0 and failure["exception_type"] == "IntegrationError"
    for item in events(caplog):
        if item["stage"] in {"submission.d365_submit", "submission.finish"} and item["outcome"] == "failed":
            assert item["issue_code"] == "field_length_or_missing" and item["upstream_status_code"] == 500
            assert item["activity_id"] == "cb035259-4dd8-0000-ec66-bed5d84ddd01"
    final = next(event for event in events(caplog) if event["stage"] == "test.context_after_submit")
    assert final["request_id"] == "c58c3df3-5dac-4a10-a7e9-6e8cf61ac324"
    assert "requirement_id" not in final
    assert all(event["request_id"] == "c58c3df3-5dac-4a10-a7e9-6e8cf61ac324"
               for event in events(caplog) if event["stage"].startswith("submission") and "request_id" in event)
    for private in ("private resume", "private phone", "private-sub", "private-issuer", "Private Applicant",
                    "applicant@example.test", "private-candidate.pdf", "private-resume-reference", "private-idempotency-key"):
        assert private not in caplog.text


async def test_worker_retry_logs_stage_and_reuses_ai_with_isolated_attempt_context(caplog):
    caplog.set_level(logging.INFO, logger="app.workflow")
    settings = Settings(_env_file=None, demo_mode=False, d365_contract_mode="legacy")
    redis = MemoryRedis()
    adapter = Mock(save_candidate_evaluation=AsyncMock(side_effect=[
        IntegrationError("private provider output applicant@example.test"), {"success": True},
    ]))
    resumes = Mock(read=AsyncMock(return_value=b"private resume bytes"),
                   extract=Mock(return_value="private resume text"))
    ai = Mock(evaluate=AsyncMock(return_value=sample_evaluation()))
    ctx = {"settings": settings, "redis": redis, "adapter": adapter, "resumes": resumes, "ai": ai, "job_try": 1}
    job = DemoAdapter().jobs[0]
    args = ("APP-logging-test", "azure:private-blob", "private-candidate.pdf", job.requirement_id, job.model_dump())
    with log_context(request_id="e72d4acd-cd58-4ee8-a710-885eed168327"):
        with pytest.raises(Retry):
            await evaluate_application(ctx, *args)
        ctx["job_try"] = 2
        assert await evaluate_application(ctx, *args) == {"status": "completed"}
        log_event("test.context_after_worker", "completed")
    assert json.loads(redis.values[f"{settings.redis_prefix}:evaluation:{args[0]}"])["status"] == "Completed"
    ai.evaluate.assert_awaited_once()
    resumes.read.assert_awaited_once()
    assert adapter.save_candidate_evaluation.await_count == 2
    attempts = [event for event in events(caplog) if event["stage"] == "worker.evaluation_attempt"
                and event["outcome"] == "start"]
    assert [event["attempt"] for event in attempts] == [1, 2]
    assert all(event["application_id"] == args[0] and event["requirement_id"] == job.requirement_id
               for event in attempts)
    assert len({event["request_id"] for event in attempts}) == 2
    final = next(event for event in events(caplog) if event["stage"] == "test.context_after_worker")
    assert final["request_id"] == "e72d4acd-cd58-4ee8-a710-885eed168327" and "application_id" not in final
    assert any(event["stage"] == "worker.evaluation_retry" and event["retry_in_seconds"] == 10
               for event in events(caplog))
    assert any(event["stage"] == "worker.cached_evaluation_validate" and event["outcome"] == "completed"
               for event in events(caplog))
    failure = next(event for event in events(caplog)
                   if event["stage"] == "worker.d365_save_evaluation" and event["outcome"] == "failed")
    assert failure["elapsed_ms"] >= 0 and failure["exception_type"] == "IntegrationError"
    assert not any(event["stage"] == "worker.evaluation" and event["outcome"] == "failed"
                   for event in events(caplog))
    for private in ("private resume bytes", "private resume text", "private-blob", "private-candidate.pdf",
                    "private provider output", "applicant@example.test"):
        assert private not in caplog.text


def test_arq_filters_arguments_and_exception_details_before_terminal_formatting():
    safe = SafeArqLogs()
    start = logging.LogRecord("arq.worker", logging.INFO, __file__, 1, "%6.2fs → %s(%s)%s",
                              (0.1, "evaluation:APP-test:evaluate_application", "private-email@example.test, private JD", ""), None)
    assert safe.filter(start)
    assert "APP-test" in start.getMessage()
    assert "private" not in start.getMessage()
    error = ValueError("provider secret and candidate details")
    failed = logging.LogRecord("arq.worker", logging.ERROR, __file__, 2, "%s failed %s",
                               ("APP-test", error), (ValueError, error, None))
    failed.extra = {"candidate_email": "private-email@example.test"}
    assert safe.filter(failed)
    assert failed.getMessage() == "arq_job_failed exception_type=ValueError"
    assert failed.exc_info is None and failed.extra is None
