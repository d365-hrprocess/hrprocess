"""Private PostgreSQL metadata shared by the API and durable worker.

No Supabase browser keys or REST table exposure are used. A server-side database
connection owns a separate, RLS-protected schema. Short transactions work with
Supabase's direct, session-pooler and transaction-pooler connection strings.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

import psycopg
from fastapi import HTTPException

from app.errors import ConfigurationError, IntegrationError
from app.metadata import encode
from app.observability import log_event, traced
from app.postgres_diagnostics import postgres_failure

SCHEMA_LOCK = 8247392194581
EVALUATION_LOCK_NAMESPACE = 8247392194582


class PostgresMetadata:
    storage_name = "postgres"

    def __init__(self, settings):
        # Validate without echoing the supplied URI (it contains credentials).
        try:
            parsed = urlsplit(settings.database_url)
            valid = parsed.scheme in {"postgres", "postgresql"} and bool(parsed.hostname) and bool(parsed.path.strip("/"))
            if not valid:
                raise ValueError
            if settings.database_sslmode == "disable" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError
            _ = parsed.port
            options = psycopg.conninfo.conninfo_to_dict(settings.database_url)
            if settings.database_sslmode == "disable":
                for option in ("host", "hostaddr"):
                    if options.get(option) and any(host not in {"localhost", "127.0.0.1", "::1"}
                                                   for host in options[option].split(",")):
                        raise ValueError
        except (ValueError, psycopg.ProgrammingError):
            raise ConfigurationError(
                "PostgreSQL requires a valid DATABASE_URL and TLS; disabled TLS is allowed only for localhost"
            ) from None
        self._url = settings.database_url
        self._options = {
            "connect_timeout": settings.database_connect_timeout,
            "sslmode": settings.database_sslmode,
            # Disable prepared statements for Supabase transaction pooler.
            "prepare_threshold": None,
            "application_name": "talentmatch-metadata",
        }
        if settings.database_sslmode == "verify-full":
            self._options["sslrootcert"] = str(settings.database_sslrootcert) if settings.database_sslrootcert else "system"
        elif settings.database_sslmode == "require":
            # libpq otherwise verifies a CA inherited from the URL, environment,
            # or ~/.postgresql/root.crt even in require mode. An absent CA in a
            # private directory makes this explicit encryption-only setting work
            # consistently without changing the process environment.
            self._unverified_ca_directory = TemporaryDirectory(prefix="talentmatch-noverify-")
            self._options["sslrootcert"] = str(Path(self._unverified_ca_directory.name) / "unused-ca.pem")
        self._initialized = False

    @contextmanager
    def connection(self):
        # psycopg exceptions may contain the password, host or SQL parameters.
        # Only fixed, value-free application errors may leave this boundary.
        operation = "connect"
        try:
            with psycopg.connect(self._url, **self._options) as connection:
                # SQL-level LOCAL settings also work with poolers which reject
                # the PostgreSQL startup packet's options parameter.
                operation = "session_setup"
                connection.execute("SET LOCAL statement_timeout='30s'")
                connection.execute("SET LOCAL lock_timeout='30s'")
                operation = "transaction"
                yield connection
                operation = "commit"
        except psycopg.Error as exc:
            reason, detail, retryable = postgres_failure(exc)
            code = exc.sqlstate
            if not (isinstance(code, str) and len(code) == 5 and code.isascii() and code.isalnum() and code == code.upper()):
                code = None
            log_event("postgres.transaction", "failed", exception_type=type(exc).__name__,
                      storage="postgres", reason=reason, operation=operation, mode=self._options["sslmode"],
                      error_code=code, hint=detail, retryable=retryable)
            raise IntegrationError(detail, status_code=503, retryable=retryable,
                log_diagnostics={"issue_code": "postgres_" + reason, "database_state": code, "hint": detail,
                                 "root_exception_type": type(exc).__name__}) from None

    @traced("postgres.initialize")
    def _initialize(self):
        with self.connection() as connection:
            # Serialize first-start DDL across API, worker and other desktops.
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
            connection.execute("CREATE SCHEMA IF NOT EXISTS talentmatch")
            connection.execute("REVOKE ALL ON SCHEMA talentmatch FROM PUBLIC")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS talentmatch.schema_version (
                    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
                    version INTEGER NOT NULL
                )
            """)
            connection.execute("INSERT INTO talentmatch.schema_version VALUES(TRUE, 1) ON CONFLICT DO NOTHING")
            version = connection.execute("SELECT version FROM talentmatch.schema_version WHERE singleton=TRUE").fetchone()[0]
            if version != 1:
                raise IntegrationError("PostgreSQL metadata schema version is unsupported", retryable=False)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS talentmatch.applications (
                    application_id TEXT PRIMARY KEY,
                    submission_key TEXT NOT NULL UNIQUE,
                    company TEXT NOT NULL,
                    requirement_id TEXT NOT NULL,
                    owner_issuer TEXT NOT NULL,
                    owner_sub TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    record_json TEXT NOT NULL
                )
            """)
            connection.execute("""CREATE INDEX IF NOT EXISTS applications_owner
                ON talentmatch.applications(company, owner_issuer, owner_sub)""")
            connection.execute("""CREATE INDEX IF NOT EXISTS applications_requirement
                ON talentmatch.applications(company, requirement_id)""")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS talentmatch.evaluation_states (
                    application_id TEXT PRIMARY KEY REFERENCES talentmatch.applications(application_id),
                    state_json TEXT NOT NULL
                )
            """)
            for table in ("schema_version", "applications", "evaluation_states"):
                # Identifiers are fixed literals, never request/configuration input.
                connection.execute(f"ALTER TABLE talentmatch.{table} ENABLE ROW LEVEL SECURITY")
                connection.execute(f"REVOKE ALL ON talentmatch.{table} FROM PUBLIC")
            # Supabase may install default privileges for its API roles. Remove
            # these too if those roles exist; RLS also denies them by default.
            connection.execute("""
                DO $$ BEGIN
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN
                        REVOKE ALL ON SCHEMA talentmatch FROM anon;
                        REVOKE ALL ON ALL TABLES IN SCHEMA talentmatch FROM anon;
                    END IF;
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN
                        REVOKE ALL ON SCHEMA talentmatch FROM authenticated;
                        REVOKE ALL ON ALL TABLES IN SCHEMA talentmatch FROM authenticated;
                    END IF;
                END $$
            """)
        self._initialized = True
        log_event("postgres.schema", "validated", schema_version=1, storage="postgres")

    async def initialize(self):
        if not self._initialized:
            await asyncio.to_thread(self._initialize)

    @traced("postgres.transaction")
    async def run(self, callback):
        await self.initialize()

        def perform():
            with self.connection() as connection:
                return callback(connection)

        return await asyncio.to_thread(perform)

    @traced("postgres.submission.reserve")
    async def reserve(self, key: str, record: dict) -> tuple[dict, bool]:
        def perform(connection):
            payload = record["payload"]
            inserted = connection.execute("""
                INSERT INTO talentmatch.applications VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(submission_key) DO NOTHING RETURNING application_id
            """, (record["application_id"], key, record["company"], record["requirement_id"],
                  payload["owner_issuer"], payload["owner_sub"], record["fingerprint"], encode(record))).fetchone()
            if inserted:
                return record, True
            row = connection.execute(
                "SELECT record_json FROM talentmatch.applications WHERE submission_key=%s FOR UPDATE", (key,)
            ).fetchone()
            old = json.loads(row[0])
            if old["fingerprint"] != record["fingerprint"]:
                raise HTTPException(409, "Idempotency-Key was already used for a different application")
            return old, False
        return await self.run(perform)

    async def _one(self, query, parameters):
        def perform(connection):
            row = connection.execute(query, parameters).fetchone()
            return json.loads(row[0]) if row else None
        return await self.run(perform)

    async def get_submission(self, key: str) -> dict | None:
        return await self._one("SELECT record_json FROM talentmatch.applications WHERE submission_key=%s", (key,))

    async def get_application(self, application_id: str, company: str | None = None) -> dict | None:
        if company is not None:
            return await self._one("SELECT record_json FROM talentmatch.applications WHERE application_id=%s AND company=%s",
                                   (application_id, company))
        return await self._one("SELECT record_json FROM talentmatch.applications WHERE application_id=%s", (application_id,))

    @traced("postgres.submission.update")
    async def update_submission(self, key: str, record: dict):
        def perform(connection):
            row = connection.execute("SELECT record_json FROM talentmatch.applications WHERE submission_key=%s FOR UPDATE",
                                     (key,)).fetchone()
            if not row:
                raise IntegrationError("PostgreSQL submission metadata is missing", retryable=False)
            old = json.loads(row[0])
            for field in ("application_id", "fingerprint", "payload", "payload_sha256", "company", "requirement_id", "job_snapshot"):
                if old[field] != record[field]:
                    raise IntegrationError("Frozen application metadata cannot be changed", retryable=False)
            if old.get("candidate_id") and old["candidate_id"] != record.get("candidate_id"):
                raise IntegrationError("D365 acknowledgement changed the CandidateId", retryable=False)
            connection.execute("UPDATE talentmatch.applications SET record_json=%s WHERE submission_key=%s",
                               (encode(record), key))
        await self.run(perform)

    async def submissions(self, *, company=None, owner=None, requirement_id=None):
        clauses, parameters = [], []
        if company is not None:
            clauses.append("company=%s")
            parameters.append(company)
        if owner is not None:
            clauses.extend(["owner_issuer=%s", "owner_sub=%s"])
            parameters.extend(owner)
        if requirement_id is not None:
            clauses.append("requirement_id=%s")
            parameters.append(requirement_id)
        query = "SELECT submission_key, record_json FROM talentmatch.applications"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        return await self.run(lambda connection: [(row[0], json.loads(row[1]))
                                                 for row in connection.execute(query, parameters)])

    async def get_evaluation_state(self, application_id: str):
        return await self._one("SELECT state_json FROM talentmatch.evaluation_states WHERE application_id=%s", (application_id,))

    async def save_evaluation_state(self, application_id: str, state: dict):
        await self.run(lambda connection: connection.execute("""
            INSERT INTO talentmatch.evaluation_states VALUES(%s,%s) ON CONFLICT(application_id)
            DO UPDATE SET state_json=excluded.state_json
        """, (application_id, encode(state))))

    @asynccontextmanager
    async def evaluation_lock(self, application_id: str):
        """Coordinate independent desktop Redis queues through the shared DB.

        A transaction-scoped lock remains bound to its transaction even through
        Supabase transaction pooling; session locks do not provide this property.
        This connection is held only for the bounded worker/retry operation.
        """
        await self.initialize()

        def acquire():
            try:
                connection = psycopg.connect(self._url, **self._options)
                try:
                    connection.execute("SET LOCAL statement_timeout='30s'")
                    connection.execute("SET LOCAL lock_timeout='30s'")
                    connection.execute("SET LOCAL idle_in_transaction_session_timeout='15min'")
                    acquired = connection.execute(
                        "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, %s))",
                        (application_id, EVALUATION_LOCK_NAMESPACE),
                    ).fetchone()[0]
                    return connection, acquired
                except BaseException:
                    connection.close()
                    raise
            except psycopg.Error as error:
                reason, detail, _ = postgres_failure(error)
                raise IntegrationError("PostgreSQL evaluation lock is unavailable", status_code=503,
                    log_diagnostics={"issue_code": "postgres_" + reason, "hint": detail,
                                     "root_exception_type": type(error).__name__}) from None

        # Shield acquisition so cancellation cannot abandon an open connection
        # and an unreleased cross-process lock in a background thread.
        task = asyncio.create_task(asyncio.to_thread(acquire))
        try:
            connection, acquired = await asyncio.shield(task)
        except asyncio.CancelledError:
            connection, _ = await task
            await asyncio.to_thread(connection.close)
            raise
        try:
            yield acquired
        finally:
            await asyncio.shield(asyncio.to_thread(connection.close))
