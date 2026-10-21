# Production logging verification — 21 October 2026

## Change and deployment scope

Logging-only changes cover the recruitment API, authentication/session handlers, submission/reconciliation, evaluation worker, shared exception diagnostics and isolated finance process. Existing stages for resumes/private Blob storage, PostgreSQL/Redis and AI processing now retain safe underlying causes across wrappers. Failed D365 calls include known contract field names, types and string lengths at INFO.

There are no changes to environment variables, runtime versions, dependencies, Dockerfiles, startup/routing, HTTP ports, credentials, database schemas, submission state, retries, deadlines or business operations. Extended D365 templates and fingerprints are server-log metadata, not new browser response fields. Normal SDK wire output remains suppressed.

See [debugging.md](debugging.md) for the exact App Service Log Stream location, field meanings and troubleshooting categories. No additional App Service setting is needed. Deploy the code with the existing image pipeline before expecting these events.

## Executed checks

| Check | Actual result |
| --- | --- |
| `bash scripts/check.sh` | Passed: backend lint, D365 source/DTO contract validation, backend tests, frontend lint/type checking/tests and production bundle |
| Recruitment backend, Python 3.12 | 384 passed, 20 skipped; skipped tests require optional external services/configuration |
| Finance suite, isolated Python 3.13.3: `env -u PYTHONPATH uv run --frozen --extra dev pytest -q` in `finance_demo` | 114 passed, including connector schema and logging/privacy tests |
| Frontend | 78 passed; no frontend source changes |
| Additional logging assertions | Failed D365 DTO shape visible at INFO; inner cause survives outer service/submission stages; extended diagnostic templates excluded from public error diagnostics |
| `git diff --check` | Passed |

Tests exercise nested F&O/Infolog responses, standard runtime/field/duplicate errors, malformed/oversized/deep responses, request-value redaction, safe stack frames, Entra failure codes, HTTP transport categories, cyclic exception chains, safe finance request routes and unchanged pending-submission recovery behavior. Existing tests verify storage, authentication, queue/worker and AI-stage privacy and behavior. Dependency deprecation/build annotation warnings do not fail these checks.

These are source and simulated-provider tests. This change has not been deployed or tested against live Azure/F&O/Copilot from this workspace: no approved Azure identity or production secret is configured here. A new complete container build was not run for this logging-only revision. Existing build instructions copy the changed Python packages without requiring Dockerfile/dependency changes.

## Production follow-up

After the existing company pipeline deploys the revision, reproduce the original pending submission using its original details/key. Capture the `correlation_id`, `activity_id`, `application_id`, failure stage and available cause fields from App Service Log Stream. Do not clear durable state or generate a replacement application merely to collect logs.

If F&O still sends only a generic ErrorException, `cause_available=false` explicitly records that limitation. This logging revision does not fix the underlying F&O HTTP 500 and cannot reconstruct internal details omitted from its response. Inspect/debug the custom service in the authorized F&O environment when necessary.

Rollback uses the existing previous-image procedure. No settings, ERP records or database data need removing for this change.
