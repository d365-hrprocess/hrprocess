"""Submission coordination. Connected screenshot contracts keep a SQLite outbox.

Redis transports jobs and provides locks; the SQLite snapshot survives queue loss.
The explicitly selected legacy contract retains its previous Redis behavior.
"""

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from app.errors import IntegrationError
from app.metadata import MetadataStore, encode
from app.observability import error_fields, log_context, log_event, stage, traced

class SubmissionCoordinator:
    def __init__(self, settings, adapter, resumes, redis=None):
        self.settings, self.adapter, self.resumes, self.redis = settings, adapter, resumes, redis
        metadata = getattr(adapter, "metadata", None)
        self.metadata = metadata if isinstance(metadata, MetadataStore) else None
        self.memory = {}
        self.locks = {}

    def key(self, user, idempotency_key):
        if self.metadata:
            scope = [self.settings.d365_company, user.issuer, user.sub, idempotency_key]
            digest = hashlib.sha256(encode(scope).encode()).hexdigest()
        else:
            # Keep existing Redis keys discoverable in explicitly selected
            # legacy mode when upgrading an already running deployment.
            digest = hashlib.sha256(f"{user.issuer}|{user.sub}|{idempotency_key}".encode()).hexdigest()
        return f"{self.settings.redis_prefix}:submission:{digest}"

    @traced("submission.outbox_load")
    async def load(self, key):
        if self.metadata:
            return await self.metadata.get_submission(key)
        if self.redis:
            value = await self.redis.get(key)
            return json.loads(value) if value else None
        return self.memory.get(key)

    @traced("submission.outbox_persist")
    async def persist(self, key, record):
        log_event("submission.outbox_state", "saving", state=record.get("stage"),
                  storage=self.metadata.storage_name if self.metadata else "redis" if self.redis else "memory")
        if self.metadata:
            await self.metadata.update_submission(key, record)
            return
        if self.redis:
            await self.redis.set(key, json.dumps(record), ex=self.settings.retention_days * 86400)
        else:
            self.memory[key] = record

    @traced("submission.evaluation_enqueue")
    async def enqueue(self, key, record):
        if not self.redis:
            log_event("submission.evaluation_enqueue", "skipped", reason="redis_not_configured")
            return
        # The outbox is already durable before attempting enqueue. A periodic
        # worker reconciliation recovers a crash between D365 acceptance and enqueue.
        await self.redis.enqueue_job(
            "evaluate_application",
            record["application_id"],
            record["resume_ref"],
            record["resume_file_name"],
            record["requirement_id"],
            record["job_snapshot"],
            _job_id=f"evaluation:{record['application_id']}",
            _expires=self.settings.retention_days * 86400,
        )
        record["stage"] = "queued"
        await self.persist(key, record)
        log_event("submission.evaluation_enqueue", "queued", status="Pending")

    async def finish(self, key, record):
        with log_context(application_id=record.get("application_id"), requirement_id=record.get("requirement_id")):
            return await self._finish(key, record)

    @traced("submission.finish")
    async def _finish(self, key, record):
        log_event("submission.outbox_resume", "start", state=record["stage"])
        if record["stage"] == "ready":
            with stage("submission.d365_submit"):
                result = await self.adapter.submit_application(record["payload"])
                if not result.get("application_id"):
                    raise IntegrationError("Application service did not return an identifier", retryable=False)
                if self.metadata and result["application_id"] != record["application_id"]:
                    raise IntegrationError("D365 changed the frozen ApplicationId", retryable=False)
            record.update(application_id=str(result["application_id"]), stage="accepted")
            if self.metadata:
                record.update(candidate_id=result["candidate_id"], hr_status="Submitted")
            with log_context(application_id=record["application_id"]):
                await self.persist(key, record)
                log_event("submission.application_acknowledgement", "completed", state="accepted")
        with log_context(application_id=record["application_id"]):
            if record["stage"] == "accepted":
                await self.enqueue(key, record)
            state = await self.metadata.get_evaluation_state(record["application_id"]) if self.metadata else None
            return {
                "success": True,
                "application_id": record["application_id"],
                "ai_status": "Completed" if self.settings.demo_mode else (state or {}).get("status", "Pending"),
                "message": "Application submitted. Demo evaluation is a fictional sample."
                if self.settings.demo_mode
                else "Application submitted. Evidence evaluation is queued for human review.",
            }

    async def submit(
        self, user, idempotency_key, fingerprint, job, data, filename, phone, linkedin_url, consent_version
    ):
        with log_context(requirement_id=job.requirement_id):
            return await self._submit(user, idempotency_key, fingerprint, job, data, filename, phone,
                                      linkedin_url, consent_version)

    @traced("submission.coordinate")
    async def _submit(
        self, user, idempotency_key, fingerprint, job, data, filename, phone, linkedin_url, consent_version
    ):
        key = self.key(user, idempotency_key)
        log_event("submission.lock", "start", storage="redis" if self.redis else "memory")
        if self.redis:
            lock = self.redis.lock(key + ":lock", timeout=120, blocking_timeout=1)
            if not await lock.acquire():
                log_event("submission.lock", "rejected", level=logging.WARNING, lock_acquired=False,
                          reason="submission_in_progress")
                raise HTTPException(409, "This submission is already in progress. Retry with the same Idempotency-Key.")
        else:
            lock = self.locks.setdefault(key, asyncio.Lock())
            await lock.acquire()
        log_event("submission.lock", "completed", lock_acquired=True)
        try:
            record = await self.load(key)
            if record:
                log_event("submission.idempotency", "reused", application_id=record.get("application_id"),
                          state=record["stage"])
                if record["fingerprint"] != fingerprint:
                    log_event("submission.idempotency", "rejected", level=logging.WARNING,
                              reason="payload_mismatch",
                              resume_changed=record["payload"].get("resume_sha256") != hashlib.sha256(data).hexdigest(),
                              contact_changed=any(record["payload"].get(field) != value for field, value in (
                                  ("candidate_name", user.name), ("email", user.email),
                                  ("phone", phone), ("linkedin_url", linkedin_url))),
                              consent_changed=record["payload"].get("consent_version") != consent_version)
                    raise HTTPException(409, "Idempotency-Key was already used for a different application")
                return await self.finish(key, record)
            log_event("submission.resume_store", "start", bytes_count=len(data))
            resume_ref = await self.resumes.store(data, filename)
            log_event("submission.resume_store", "completed", bytes_count=len(data))
            job_hash = hashlib.sha256(job.model_dump_json().encode()).hexdigest()
            payload = {
                "requirement_id": job.requirement_id,
                "candidate_name": user.name,
                "email": user.email,
                "owner_sub": user.sub,
                "owner_issuer": user.issuer,
                "phone": phone,
                "linkedin_url": linkedin_url,
                "resume_ref": resume_ref,
                "resume_file_name": filename,
                "resume_sha256": hashlib.sha256(data).hexdigest(),
                "job_description_hash": job_hash,
                "consent_accepted": True,
                "consent_version": consent_version,
                "consent_date": datetime.now(timezone.utc).isoformat(),
                "idempotency_key": key.rsplit(":", 1)[-1] if self.metadata
                else hashlib.sha256((key + fingerprint + job_hash).encode()).hexdigest(),
            }
            if user.linkedin_profile is not None:
                # This snapshot comes from validated provider data, not editable
                # form fields. Retry the recorded first submission unchanged.
                payload["linkedin_profile"] = user.linkedin_profile.model_dump(mode="json", exclude_none=True)
            record = {
                "stage": "ready",
                "fingerprint": fingerprint,
                "payload": payload,
                "resume_ref": resume_ref,
                "resume_file_name": filename,
                "requirement_id": job.requirement_id,
                "job_snapshot": job.model_dump(mode="json"),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            if self.metadata:
                application_id = "APP-" + str(uuid.uuid4())
                payload["application_id"] = application_id
                record.update(application_id=application_id, company=self.settings.d365_company,
                              payload_sha256=hashlib.sha256(encode(payload).encode()).hexdigest())
                try:
                    log_event("submission.sqlite_reserve", "start", application_id=application_id)
                    record, created = await self.metadata.reserve(key, record)
                    log_event("submission.sqlite_reserve", "completed", application_id=record["application_id"],
                              cached=not created, state=record["stage"])
                except Exception as exc:
                    log_event("submission.sqlite_reserve", "failed", level=logging.ERROR,
                              exception_type=type(exc).__name__)
                    log_event("submission.resume_cleanup", "start", reason="reservation_failed")
                    await self.resumes.delete(resume_ref)
                    log_event("submission.resume_cleanup", "completed", reason="reservation_failed")
                    raise
                if not created:
                    # A competing process reserved the immutable input first.
                    log_event("submission.resume_cleanup", "start", reason="competing_reservation")
                    await self.resumes.delete(resume_ref)
                    log_event("submission.resume_cleanup", "completed", reason="competing_reservation")
            else:
                try:
                    await self.persist(key, record)
                except Exception as exc:
                    log_event("submission.outbox_persist", "failed", level=logging.ERROR,
                              exception_type=type(exc).__name__)
                    log_event("submission.resume_cleanup", "start", reason="persistence_failed")
                    await self.resumes.delete(resume_ref)
                    log_event("submission.resume_cleanup", "completed", reason="persistence_failed")
                    raise
            return await self.finish(key, record)
        finally:
            try:
                if self.redis:
                    await lock.release()
                else:
                    lock.release()
                log_event("submission.lock_release", "completed")
            except Exception as exc:
                log_event("submission.lock_release", "failed", level=logging.WARNING,
                          exception_type=type(exc).__name__)
                pass

    @traced("submission.reconciliation")
    async def reconcile(self):
        if not self.redis:
            log_event("submission.reconciliation", "skipped", reason="redis_not_configured")
            return
        if self.metadata:
            entries = await self.metadata.submissions(company=self.settings.d365_company)
        else:
            entries = [(key.decode() if isinstance(key, bytes) else key, None)
                       async for key in self.redis.scan_iter(match=f"{self.settings.redis_prefix}:submission:*")]
        log_event("submission.reconciliation_scan", "completed", count=len(entries))
        for key, _ in entries:
            if key.endswith(":lock"):
                continue
            record = await self.load(key)
            if not record or record.get("stage") not in {"ready", "accepted", "queued"}:
                continue
            with log_context(application_id=record.get("application_id"), requirement_id=record.get("requirement_id")):
                await self._recover_entry(key, record)

    async def _recover_entry(self, key, record):
        log_event("submission.reconciliation_entry", "start", state=record["stage"])
        if record["stage"] == "queued":
            application_id = record["application_id"]
            if self.metadata:
                state = await self.metadata.get_evaluation_state(application_id) or {}
            else:
                evaluation = await self.redis.get(f"{self.settings.redis_prefix}:evaluation:{application_id}")
                state = json.loads(evaluation) if evaluation else {}
            if state.get("status") in {"Completed", "Failed"}:
                log_event("submission.reconciliation_entry", "skipped", reason="evaluation_terminal",
                          status=state["status"])
                return
            if await self.redis.exists(
                f"arq:job:evaluation:{application_id}", f"arq:result:evaluation:{application_id}"
            ):
                log_event("submission.reconciliation_entry", "skipped", reason="queue_job_exists")
                return
            record["stage"] = "accepted"
            log_event("submission.reconciliation_entry", "recovering", reason="queue_job_missing")
        lock = self.redis.lock(key + ":lock", timeout=120, blocking_timeout=0)
        if await lock.acquire():
            try:
                await self.finish(key, record)
                log_event("submission.reconciliation_entry", "completed", state=record["stage"])
            except Exception as exc:
                # Do not log candidate data or external responses. Keep
                # durable state to permit a later recovery attempt.
                log_event("submission.reconciliation_entry", "failed", level=logging.WARNING,
                          reason="recovery_pending", **error_fields(exc))
            finally:
                await lock.release()
        else:
            log_event("submission.reconciliation_entry", "skipped", reason="submission_locked",
                      lock_acquired=False)

def submission_fingerprint(requirement_id, data, phone, linkedin_url, consent_version, *, candidate_name=None, email=None):
    fields = [requirement_id, phone, linkedin_url, consent_version]
    if candidate_name is not None or email is not None:
        fields.extend([candidate_name, email])
    metadata = json.dumps(fields, separators=(",", ":"))
    return hashlib.sha256(metadata.encode() + b"\0" + data).hexdigest()
