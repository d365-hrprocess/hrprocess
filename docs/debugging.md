# Terminal logs and debugging

The API, evaluation worker and isolated finance process write stage logs to stdout/stderr. Major operations record progress, failures and timings. Failure context survives integration wrappers and submission reconciliation, so the outer request/worker event retains the available provider cause. Logs exclude candidate details and integration credentials.

## Production: no new settings required

After deploying this revision through the existing image pipeline, open **Azure Portal → App Services → ubtihrprocess → Monitoring → Log stream**. Select application/container logs. The existing supervisor forwards the API, worker and finance process output there; their `stage` names distinguish the processes. No additional environment variable, logging service or application restart setting is introduced. Existing `LOG_LEVEL=INFO` is sufficient; a restart alone does not install these source changes.

Find the portal's failure reference in `correlation_id`, or its support activity in `activity_id`. Read nearby events with the same `request_id`, then follow `application_id` into background recovery/worker events. Save the relevant interval from Log Stream or the existing container logs promptly; the live stream is not a new permanent log archive.

The change adds diagnostics only. It does not alter submission IDs, durable state, resume handling, retries, deadlines, authentication, routing, database schemas or ERP business validation. Logs describe future attempts after deployment; they cannot recover a previously omitted provider response.

## Enable and capture logs

After the updated code is pushed, pull it from PowerShell in your repository folder:

```powershell
git pull --ff-only origin main
cd backend
uv sync --frozen
```

`LOG_LEVEL=INFO` is the default. It includes normal stage progress and failures. Set `LOG_LEVEL=DEBUG` in `backend/.env` for additional diagnostic events, then restart both API and worker:

```dotenv
LOG_LEVEL=DEBUG
```

Debug level does not enable raw provider responses, tokens, request bodies, candidate contact information, resumes, or AI prompts. Return to `INFO` after troubleshooting.

Known D365 contract field names, value types and string lengths are logged at **INFO when a D365 HTTP request fails**, and at DEBUG during normal request preparation. For example, `field="ApplicationId" value_type="str" value_length=40` helps compare the submitted shape against an existing F&O field size. Actual field values stay excluded. Do not enable SDK wire/debug logging to obtain these diagnostics.

For Windows, open PowerShell in the `backend` folder. Create the ignored output directory:

```powershell
New-Item -ItemType Directory -Force .local | Out-Null
```

Start the existing HTTPS API and capture both stdout and stderr while keeping the output visible:

```powershell
uv run --frozen uvicorn app.main:app --host 127.0.0.1 --port 7242 --ssl-certfile .certs\localhost.pem --ssl-keyfile .certs\localhost-key.pem 2>&1 |
    Tee-Object -FilePath .local\api-debug.log
```

In a second PowerShell window, also in the same `backend` folder, start and capture the worker:

```powershell
uv run --frozen python -m app.worker 2>&1 |
    Tee-Object -FilePath .local\worker-debug.log
```

Keep Redis running and use the same environment and metadata backend/connection in both processes. SQLite processes must resolve the same durable file; PostgreSQL processes must use the same database. A successful API submission cannot progress through connected AI review without the worker. The PowerShell pipeline is for displaying/capturing logs; use the process output to diagnose startup failures rather than treating the pipeline's success as proof that the server started.

On an existing deployment, inspect the API and worker service logs separately. For the connected Compose arrangement:

```bash
docker compose logs --follow api worker
```

## Read a processing trace

Stage events contain these fields when they apply:

| Field | Meaning |
| --- | --- |
| `stage` | The operation being performed, such as submission, storage, D365, queueing, or evaluation |
| `outcome` | Stage progress (`start`, `completed`, `failed`) or a specific event such as `received`, `queued`, `skipped`, or `reused` |
| `elapsed_ms` | Duration of a completed or failed stage |
| `request_id` | The UUID identifying a browser/API request; returned in the `X-Request-ID` response header |
| `application_id` | The stable application ID linking submission and background evaluation |
| `correlation_id` | The UUID identifying one outgoing D365 call; also included in the portal's D365 failure reference |
| `issue_code`, `hint` | An identified provider/configuration problem and a suggested check |
| `provider_message` | A recognized, safe D365 validation message; never an arbitrary raw response body |
| `activity_id` | A D365 activity ID, when the response supplies a valid one |
| `upstream_status_code` | Provider HTTP status, distinct from the portal's integration status |
| `root_exception_type`, `code_location` | Available inner exception type and backend filename/line/function; no exception values or local variables |
| `message_template` | Bounded technical words from unfamiliar D365 messages; quoted text, numbers, addresses, request values and words outside a closed vocabulary become `[redacted]` |
| `error_fingerprint` | Hash of the safe message templates for grouping similar failures; not a hash of the original private response |
| `provider_stack` | Only recognized `UBHRPortalService` method names returned by F&O; no arguments or filesystem paths |
| `message_count`, `inner_exception_count`, `diagnostic_truncated` | Available bounded message/type counts and whether diagnostics were limited |
| `cause_available` | Whether a recognized cause pattern was returned; false means the internal cause remains unknown |
| `provider_code`, `database_state` | Recognized Entra AADSTS code or PostgreSQL SQLSTATE; no raw identity/database error text |

An illustrative trace has this structure; the exact stage names depend on the operation:

```text
stage=application.submit outcome=start request_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
stage=d365.http outcome=failed request_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" application_id="APP-bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb" operation="submit_application" status_code=500 correlation_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc" issue_code="company_mismatch"
stage=application.submit outcome=failed request_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" elapsed_ms=683
```

Start with the most specific failed stage: an outer application/request failure can be the result of an inner storage or D365 failure. A start event without a later completion or failure means the operation is still pending or its process stopped before reporting an outcome. Check both elapsed time and service health.

The API request ID ties together work done while handling that request. The stable application ID connects it to the separate worker process, retries, and the saved evaluation. Each outgoing D365 operation has its own correlation ID; an automatic authentication-refresh retry retains that ID and changes the `attempt` field.

In PowerShell, find the reference shown in the portal or a known application ID:

```powershell
Select-String -Path .local\api-debug.log,.local\worker-debug.log -SimpleMatch "5457f25f-092c-4df4-ad28-8c7922e8338e"
```

Replace the example with the actual ID. To narrow the output to failures:

```powershell
Select-String -Path .local\api-debug.log,.local\worker-debug.log -Pattern 'outcome=failed'
```

## Follow the application stages

| Stage area | What its logs help establish |
| --- | --- |
| Startup | API/worker startup, configuration mode, SQLite initialization, and Redis readiness |
| Authentication | Login redirect, callback validation, profile processing, and session creation |
| Job reads | D365 token acquisition, request, safe HTTP status, response parsing, and job mapping |
| Submission | `application.submit`, `application.validate`, and `job.lookup` identify candidate authorization, job availability, upload validation, document storage, and frozen SQLite submission intent |
| D365 application write | Sending the existing-table contract, provider acknowledgement, and acknowledgement persistence |
| Queue | Evaluation scheduling, duplicate/recovery handling, and enqueue acknowledgement |
| Worker | Job start, retained-state loading, document retrieval, checksum verification, and text extraction |
| AI review | Model request, structured response validation, evidence checks, and durable evaluation caching |
| D365 evaluation write | Core-result save, provider acknowledgement, and final local completion |
| Recovery | Retained submission reconciliation and bounded evaluation retries |
| Finance | `finance.http`, `finance.operation`, `finance.token`, `finance.d365.*`, `finance.metadata`, and `finance.reporting.scan` identify request status, uncertain-write cause, token failures, metadata loading and complete-source scan state without account values or financial amounts |

Some operations skip stages deliberately: a repeated acknowledged submission can reuse its original result, and a persistence retry can reuse the already validated AI evaluation. Demo mode does not call the connected D365/Azure services.

## Diagnose a D365 HTTP 500

An event for `submit_application` with HTTP 500 means the upstream F&O service failed. FastAPI reports an upstream integration failure as HTTP 502 to the browser. It does not establish which F&O validation failed or whether a response was lost after a commit.

The diagnostic parser recognizes the source pack's safe validation errors, including:

- The request company differs from the integration user's configured current company.
- A known table field is missing, has an incompatible type, or exceeds its existing string length.
- An existing status enum lacks the configured symbol, such as `Pending` or `Completed`.
- Candidate/application/evaluation table validation rejects existing business defaults.
- A requirement no longer accepts applications or stable IDs do not match the existing record.

Recognized failures include an `issue_code`, a canonical `provider_message`, and a `hint`. Field names and configured length limits can identify the AOT property to check without displaying the submitted value. Check these diagnostics before changing the service or table.

For unfamiliar JSON errors, the parser also examines nested errors/Infolog messages and recognizes common duplicate-record, mandatory-field, field-size, type-conversion, null-reference, permission, concurrency, timeout and serialization patterns. Treat these categories as diagnostic clues and confirm the relevant configuration/service code before changing it. Only approved table/field names from the repository's AOT inventory are retained. These new templates, fingerprints and service frames stay in server logs; they are not added to browser error responses.

If F&O returns only a generic `ErrorException`, expect `issue_code="d365_unclassified"` and `cause_available=false`. A redacted template and support activity identify what the provider actually returned; they cannot reveal an internal Infolog exception that F&O omitted. Malformed, HTML and oversized responses are identified by `response_kind` without dumping their content. An authorized F&O developer must inspect/debug the service if the internal cause was not returned; LCS access is not a prerequisite for using these backend diagnostics. Do not disable TLS, remove validation, or change table constraints solely because the HTTP status is 500.

For an idempotency mismatch, `resume_changed`, `contact_changed` and `consent_changed` report comparison results only. Preserve the original pending submission; these flags do not authorize creating a replacement application or deleting durable state. Nginx's request-body buffering warning during a resume upload is normal buffering, not proof of a failed upload.

## Other integration failures

| Diagnostic | What to inspect |
| --- | --- |
| `entra_*`, known `AADSTS...` | Client/tenant/resource configuration, actual secret VALUE/expiry, approved consent/policy; token responses and secrets remain suppressed |
| `network_connect_timeout`, `network_read_timeout`, `network_write_timeout`, `network_pool_timeout` | Connection, provider response, send or pool-exhaustion stage respectively; verify any unconfirmed write before retrying |
| `network_dns_failed`, `network_tls_verification_failed`, `network_connection_refused` | DNS, existing CA trust/provider certificate or service availability; do not disable certificate verification |
| `postgres_*`, `database_state` | Named metadata operation, connection/TLS/authentication, SQLSTATE, locks or limits; fixed hints survive wrapper exceptions |
| `redis_*` | Queue/session/lock connectivity; preserve submission and evaluation keys |
| `blob_*` | Private-container validation versus upload/read/delete stage, provider HTTP status and existing storage access |
| `ai_*`, `schema_validation_failed` | Deployment/API compatibility, quota, structured-result validation or evidence stage; prompts/resumes/model output stay excluded |
| Finance `cause_code`, `stage_tag`, `outcome_uncertain` | Existing finance error classification plus token/transport/metadata/reporting logs. A healthy liveness response alone does not prove metadata readiness or successful ERP writes |

Unrecognized failures retain exception type, safe code location and stage with a generic hint. Logging cannot guarantee an actionable internal cause when the provider does not disclose one. Fix the underlying configuration/service issue separately; logging changes do not turn a failed write into a successful one.

For a retained failed evaluation, repair the identified configuration/service problem, restart with the corrected environment, then follow the recovery instructions in [deployment](deployment.md#redis-durability-and-recovery). Do not create another application just to retry an evaluation.

## Logging boundaries

Application logs omit OAuth codes/state query values, cookies, authorization headers, secrets, raw request/response bodies, resume text, AI prompts/results, candidate names/contact details, and storage object references. The Uvicorn access-log filter removes query strings; ARQ job logging avoids raw job arguments and result dumps. Unexpected application exceptions retain safe code locations and the exception type rather than the arbitrary exception value. The application's DEBUG setting does not enable verbose HTTP SDK wire logging.

Request, application, correlation, and activity IDs remain operational identifiers. Review the relevant trace before sharing it, restrict log-file access, and apply your deployment's log retention policy. `.local` and `*.log` files are ignored by Git.

## Portable Docker logs

The source-free runtime bundle uses root `.env`; run its start wrapper after editing settings so containers are recreated. Open Docker Desktop service logs or run `docker compose logs --follow api worker`. The visible portal/health URL is `https://localhost:7242` (`/api/health`). A database/PostgreSQL connection failure is separate from the LLM and ERP stages. Do not print `DATABASE_URL`, `.env` or container environment dumps while collecting logs.
