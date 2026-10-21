"""Run separately with python -m app.worker. Redis persistence is required."""

import asyncio
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from contextlib import AsyncExitStack

from arq import Retry, cron
from arq.connections import RedisSettings
from arq.worker import run_worker

from app.config import get_settings
from app.errors import ConfigurationError, IntegrationError
from app.models import Evaluation, Job
from app.observability import configure_logging, error_fields, log_context, log_event, stage, traced
from app.queue import SubmissionCoordinator
from app.services.ai import AIService
from app.metadata import MetadataStore
from app.services.sqlite_d365 import connected_adapter
from app.services.resumes import ResumeService

class SafeArqLogs(logging.Filter):
    """ARQ normally prints positional job arguments and provider exception text.

    Jobs contain private resume references, filenames and complete JD snapshots.
    Keep runner lifecycle logs, but replace those two unsafe record types before
    a handler can format them. The workflow events carry the useful context.
    """

    def filter(self, record):
        template = str(record.msg)
        if "→" in template:
            reference = record.args[1] if isinstance(record.args, tuple) and len(record.args) > 1 else "unknown"
            record.msg = "arq_job_started job_reference=%s arguments=redacted"
            record.args = (reference,)
        if record.exc_info:
            exception_type = record.exc_info[0].__name__ if record.exc_info[0] else "UnknownError"
            record.msg = "arq_job_failed exception_type=%s"
            record.args = (exception_type,)
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        if hasattr(record, "extra"):
            record.extra = None
        return True


def configure_worker_logging(settings):
    configure_logging(settings.log_level)
    arq_logger = logging.getLogger("arq.worker")
    if not any(isinstance(item, SafeArqLogs) for item in arq_logger.filters):
        arq_logger.addFilter(SafeArqLogs())


async def worker_startup(ctx):
    settings = get_settings()
    configure_worker_logging(settings)
    with log_context(request_id=str(uuid.uuid4())):
        await _worker_startup(ctx, settings)


@traced("worker.startup")
async def _worker_startup(ctx, settings):
    if settings.demo_mode:
        raise RuntimeError("The durable worker requires DEMO_MODE=false; demo evaluations are explicit fixtures")
    ctx.update(
        settings=settings, adapter=connected_adapter(settings), resumes=ResumeService(settings), ai=AIService(settings)
    )
    metadata = getattr(ctx["adapter"], "metadata", None)
    if isinstance(metadata, MetadataStore):
        log_event("worker.metadata_initialize", "start", storage=metadata.storage_name)
        await metadata.initialize()
        ctx["metadata"] = metadata
        log_event("worker.metadata_initialize", "completed", storage=metadata.storage_name)
    ctx["coordinator"] = SubmissionCoordinator(settings, ctx["adapter"], ctx["resumes"], ctx["redis"])
    await ctx["coordinator"].reconcile()


async def recover_submissions(ctx):
    with log_context(request_id=str(uuid.uuid4())):
        await ctx["coordinator"].reconcile()


async def evaluate_application(
    ctx, application_id: str, resume_ref: str, filename: str, requirement_id: str, job_snapshot: dict
):
    with log_context(request_id=str(uuid.uuid4()), application_id=application_id, requirement_id=requirement_id):
        started = time.monotonic()
        log_event("worker.evaluation", "start")
        try:
            metadata = ctx.get("metadata") or getattr(ctx["adapter"], "metadata", None)
            metadata = metadata if isinstance(metadata, MetadataStore) else None
            async with AsyncExitStack() as resources:
                acquired = True
                if metadata:
                    acquired = await resources.enter_async_context(metadata.evaluation_lock(application_id))
                if not acquired:
                    # Another desktop can have its own Redis but the same
                    # cloud DB. Only its current lock holder may run the AI.
                    log_event("worker.evaluation", "retrying", reason="shared_evaluation_running", retry_in_seconds=90)
                    # Do not retain a successful ARQ result for work another
                    # instance has not finished; it could crash before saving.
                    raise Retry(defer=90)
                else:
                    try:
                        result = await _evaluate_application(ctx, application_id, resume_ref, filename, requirement_id, job_snapshot)
                    except asyncio.CancelledError:
                        # ARQ timeout/shutdown must leave an actionable state,
                        # retaining any validated output for a later save retry.
                        if metadata:
                            state = await metadata.get_evaluation_state(application_id) or {}
                            if state.get("status") != "Completed":
                                state.update(status="Failed", error_code="processing_interrupted")
                                await asyncio.wait_for(asyncio.shield(
                                    metadata.save_evaluation_state(application_id, state)), timeout=5)
                        raise
        except Retry:
            log_event("worker.evaluation", "retrying", level=logging.WARNING,
                      elapsed_ms=round((time.monotonic() - started) * 1000, 2))
            raise
        except BaseException as exc:
            cancelled = isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit))
            log_event("worker.evaluation", "cancelled" if cancelled else "failed",
                      level=logging.WARNING if cancelled else logging.ERROR,
                      elapsed_ms=round((time.monotonic() - started) * 1000, 2), **error_fields(exc))
            raise
        else:
            failed = result.get("status") == "failed"
            log_event("worker.evaluation", "failed" if failed else "completed",
                      level=logging.ERROR if failed else logging.INFO, status=result.get("status"),
                      elapsed_ms=round((time.monotonic() - started) * 1000, 2))
            return result


async def _evaluate_application(ctx, application_id, resume_ref, filename, requirement_id, job_snapshot):
    settings, redis = ctx["settings"], ctx["redis"]
    key = f"{settings.redis_prefix}:evaluation:{application_id}"
    metadata = ctx.get("metadata") or getattr(ctx["adapter"], "metadata", None)
    metadata = metadata if isinstance(metadata, MetadataStore) else None

    @traced("worker.evaluation_state_persist")
    async def persist(state):
        log_event("worker.evaluation_state", "saving", status=state.get("status"),
                  storage=metadata.storage_name if metadata else "redis")
        if metadata:
            await metadata.save_evaluation_state(application_id, state)
        else:
            await redis.set(key, json.dumps(state), ex=settings.retention_days * 86400)

    lock = redis.lock(key + ":lock", timeout=600, blocking_timeout=0)
    log_event("worker.evaluation_lock", "start")
    if not await lock.acquire():
        log_event("worker.evaluation_lock", "retrying", level=logging.WARNING,
                  lock_acquired=False, retry_in_seconds=30)
        raise Retry(defer=30)
    log_event("worker.evaluation_lock", "completed", lock_acquired=True)
    try:
        if metadata:
            log_event("worker.application_snapshot", "start", storage=metadata.storage_name)
            record = await metadata.get_application(application_id, settings.d365_company)
            if not record or record["stage"] not in {"accepted", "queued"}:
                log_event("worker.application_snapshot", "rejected", level=logging.ERROR,
                          reason="application_not_acknowledged")
                raise IntegrationError("Evaluation lacks an acknowledged application", retryable=False)
            if (resume_ref != record["resume_ref"] or filename != record["resume_file_name"]
                    or requirement_id != record["requirement_id"] or job_snapshot != record["job_snapshot"]):
                log_event("worker.application_snapshot", "rejected", level=logging.ERROR,
                          reason="snapshot_mismatch")
                raise IntegrationError("Evaluation task does not match its frozen SQLite snapshot", retryable=False)
            log_event("worker.application_snapshot", "completed", state=record["stage"])
            state = await metadata.get_evaluation_state(application_id) or {}
        else:
            previous = await redis.get(key)
            state = json.loads(previous) if previous else {}
        if state.get("status") == "Completed":
            log_event("worker.evaluation", "skipped", reason="already_completed", status="Completed")
            return {"status": "already_completed"}
        attempt = int(ctx.get("job_try", 1))
        log_event("worker.evaluation_attempt", "start", attempt=attempt)
        state.update(status="Processing", attempt=attempt)
        await persist(state)
        try:
            if state.get("evaluation"):
                with stage("worker.cached_evaluation_validate", cached=True):
                    evaluation = Evaluation.model_validate(state["evaluation"])
            else:
                with stage("worker.job_snapshot_validate"):
                    if not resume_ref or not application_id or not requirement_id:
                        raise IntegrationError("Evaluation task is missing required references", retryable=False)
                    job = Job.model_validate(job_snapshot)
                    if job.requirement_id != requirement_id:
                        raise IntegrationError("Job snapshot does not match evaluation task", retryable=False)
                with stage("worker.resume_read"):
                    data = await ctx["resumes"].read(resume_ref)
                    log_event("worker.resume_bytes", "read", bytes_count=len(data))
                with stage("worker.resume_checksum_validate"):
                    state["resume_sha256"] = hashlib.sha256(data).hexdigest()
                    if metadata and state["resume_sha256"] != record["payload"]["resume_sha256"]:
                        log_event("worker.resume_checksum_validate", "rejected", level=logging.ERROR,
                                  reason="resume_snapshot_mismatch")
                        raise IntegrationError("Resume bytes differ from the submitted snapshot", retryable=False)
                state["jd_hash"] = hashlib.sha256(job.model_dump_json().encode()).hexdigest()
                with stage("worker.resume_extract"):
                    text = await asyncio.to_thread(ctx["resumes"].extract, data, filename)
                    log_event("worker.resume_text", "extracted", text_chars=len(text))
                with stage("worker.ai_evaluate", attempt=attempt):
                    evaluation = await ctx["ai"].evaluate(text, job)
                state["evaluation"] = evaluation.model_dump()
                # Reuse validated output after a D365 write failure, so retries
                # do not regenerate a different score or incur duplicate AI calls.
                await persist(state)
                log_event("worker.evaluation_cache", "completed", cached=True)
            with stage("worker.d365_save_evaluation", attempt=attempt):
                await ctx["adapter"].save_candidate_evaluation(
                    {
                        "application_id": application_id,
                        "requirement_id": requirement_id,
                        "evaluation": evaluation.model_dump(),
                        "evaluation_date": state.setdefault("evaluation_date", datetime.now(timezone.utc).isoformat()),
                        "jd_hash": state.get("jd_hash", ""),
                        "resume_sha256": state.get("resume_sha256", ""),
                        "idempotency_key": f"{application_id}:{evaluation.prompt_version}:{evaluation.rubric_version}",
                    }
                )
            if metadata:
                # The adapter also commits the confirmed evaluation identifiers/date.
                with stage("worker.evaluation_acknowledgement", storage=metadata.storage_name):
                    state.update(await metadata.get_evaluation_state(application_id) or {})
                    state["evaluation_confirmed"] = True
            state["status"] = "Completed"
            await persist(state)
            log_event("worker.evaluation_attempt", "completed", attempt=attempt, status="Completed")
            return {"status": "completed"}
        except Exception as exc:
            if metadata:
                state.update(await metadata.get_evaluation_state(application_id) or {})
            retryable = isinstance(exc, IntegrationError) and exc.retryable
            state["status"] = "Retrying" if retryable and attempt < settings.evaluation_max_attempts else "Failed"
            # Preserve only safe category codes, never provider responses.
            state["error_code"] = "integration_error" if isinstance(exc, IntegrationError) else "processing_error"
            await persist(state)
            log_event("worker.evaluation_attempt", "failed", level=logging.WARNING,
                      attempt=attempt, status=state["status"], error_code=state["error_code"],
                      **{**error_fields(exc), "retryable": retryable})
            if retryable and attempt < settings.evaluation_max_attempts:
                log_event("worker.evaluation_retry", "scheduled", attempt=attempt,
                          retry_in_seconds=min(300, 10 * 2 ** (attempt - 1)))
                raise Retry(defer=min(300, 10 * 2 ** (attempt - 1))) from None
            return {"status": "failed"}
    finally:
        await lock.release()
        log_event("worker.evaluation_lock_release", "completed")


def worker_redis_settings():
    with stage("worker.redis.configuration"):
        try:
            return RedisSettings.from_dsn(get_settings().redis_url)
        except ConfigurationError:
            raise
        except Exception:
            log_event("worker.redis.configuration", "rejected", field="redis_url", reason="invalid_setting")
            raise ConfigurationError("Redis connection settings are invalid. Check worker.redis.configuration in the terminal.") from None


class WorkerSettings:
    functions = [evaluate_application]
    cron_jobs = [cron(recover_submissions, minute=set(range(60)), unique=True)]
    on_startup = worker_startup
    redis_settings = worker_redis_settings()
    max_jobs = 4
    job_timeout = 540
    max_tries = get_settings().evaluation_max_attempts
    keep_result = 86400
    retry_jobs = True
    log_results = False


if __name__ == "__main__":
    configure_worker_logging(get_settings())
    try:
        run_worker(WorkerSettings)
    except Exception as exc:
        # ARQ initialization happens before its job exception logger. Do not
        # allow Python to print a provider error/credential in this CLI path.
        log_event("worker.process", "failed", level=logging.ERROR, **error_fields(exc))
        raise SystemExit(1) from None
