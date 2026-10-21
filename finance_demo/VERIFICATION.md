# Finance demo implementation and verification

## Scope and routing

The module is an independent Python **3.13.3** process on loopback port **8020**, behind the existing Nginx listener on **8080**. Nginx strips `/finance` exactly once; the finance ASGI root path is `/finance`. The main Python **3.12** environment, backend dependency files, frontend source, authentication, database schema, original proxy locations and original `/healthz` implementation are unchanged. No Azure resource or App Service setting was modified during implementation.

Only one new runtime secret is read: `FINANCE_DEMO_CLIENT_SECRET`. Other finance settings are populated in `config/finance_settings.json`. The connector uses its derived HMAC key as connection authentication, with no second environment variable or new Entra application. The protected API-key mode from the detailed specification is retained; it does not introduce an end-user sign-in flow.

## Existing deployment baseline

- Public hostname was obtained from the repository's `docs/azure-devops.md` and the existing deployment record: `ubtihrprocess-egg3f7awbsbjdjd5.centralindia-01.azurewebsites.net`.
- Existing resource group: `ubti-hrprocess-rg`; App Service: `ubtihrprocess`; ACR: `hrprocess`; image repository: `ubticareers`.
- Source baseline: external port 8080; original API 8000; original readiness service 8010; Supervisor entrypoint; empty custom startup command expected by the existing deployment instructions.
- Actual current Azure image/digest, startup overrides, slots, Easy Auth and ingress policies could not be queried: the cloud environment has no attached Azure identity/CLI session.
- Public baseline requests to `/`, `/hr`, `/api/health`, `/healthz` and `/.auth/me` were rejected by this cloud environment's outbound proxy (`CONNECT 403`). These are not responses from App Service and do not establish an Azure authorization failure.
- Run `scripts/capture_azure_baseline.sh` from an approved Azure CLI session before deployment. Keep its output privately. Do not assume the historical `phase1-v1` image is the current rollback image.

## Executed source checks

| Check | Actual result |
|---|---|
| Existing backend Ruff and ERP contract-bundle checks | Passed |
| Existing backend tests on Python 3.12 | 352 passed; 20 skipped because their external test services were not configured |
| Existing frontend lint and TypeScript checks | Passed |
| Existing frontend tests | 76 passed |
| Existing frontend production build | Passed |
| Finance API, business, transport, metadata, runtime and connector tests on Python 3.13.3 | 85 passed |
| Swagger 2.0 schema validator | Passed; six business operations, 23,764-byte definition |
| Finance source/helper Python checks and baseline script shell syntax | Passed |

Finance tests use mocked HTTP/ERP responses. They cover token caching/expiry refresh, malformed/unsafe XML, namespaces, aliases and inherited properties, required fields/composite keys, separate transaction capability, fresh/stale/corrupt/wrong-environment caches, cross-manager download locking, size/time limits, denied ERP access, missing/wrong connector credentials, Secret ID rejection, disabled mode, limited history and decimal precision, duplicate create, exact write read-back and uncertain writes without retries.

Deprecation warnings come from upstream HTTPX/TestClient and JSON Schema resolver compatibility. No checks were disabled to turn a failure into a pass.

## Complete image and local HTTP regression checks

The entire `Dockerfile.azure` built successfully, including the frontend, unchanged HR dependencies and isolated finance dependencies. The final local OCI image manifest is `sha256:6d0fc39d72c558cdf8628491e632afe378ceb89eef8ebf3678a6963ef446a5fa`; this is a local build reference, not an image pushed to ACR. An OCI image was exported locally. The default Docker VFS storage ran out of space, so a temporary separate BuildKit worker used `/tmp`; the existing daemon storage/default builder were not changed.

The exact merged filesystem from the built image was started through its original entrypoint and Supervisor, as UID/GID **10001**, inside a local container. This used development/demo HR fixtures and no live ERP credentials. It was not an Azure deployment or a production-mode external-service test.

Passed checks:

- Original `/`, `/hr`, `/jobs`, `/api/jobs`, `/api/health` and `/healthz` returned 200.
- Original HR password sign-in, session cookies, CSRF rotation and authorized HR jobs access worked; unauthenticated application upload stayed blocked.
- Missing finance secret: finance data/diagnostic/schema routes returned safe JSON 503, while original HR routes and health stayed available.
- Invalid/missing connector key with a local fixture secret: protected routes returned JSON 401.
- Correct fixture key: protected Swagger and sanitized metadata diagnostics returned JSON; the private CLI derived exactly the expected stable HMAC key without exposing the source secret.
- No live D365 credential was supplied: finance readiness/data correctly stayed unavailable. These expected 503 checks are failure-isolation evidence, not proof that finance is live-ready.
- Ordinary account mutation (`AST-001`) was refused with 403 before an ERP write.
- Duplicate `/finance/finance/...` did not resolve as a business route; bare `/finance` did not redirect to an incorrect host/prefix.
- Disabled finance configuration refused protected routes without affecting original HR health.
- Stopped finance process: Nginx returned JSON 503; write outcomes were marked uncertain and non-retryable; original HR health remained 200.
- The candidate's finance interpreter reported **3.13.3**; the HR interpreter reported **3.12.15**.

Use `scripts/check_container_http.py --base-url http://127.0.0.1:18080` to repeat the read-only local HTTP checks against an appropriately started local candidate. The fixture secret used in local tests is not an Entra credential and was never sent to a live ERP.

## Remaining live verification — not completed

1. Actual client secret VALUE was not supplied. The supplied Secret ID was never used as a credential. The documented secret expiry is **27 December 2026**.
2. Azure baseline, Easy Auth and ingress compatibility must be captured. If ingress requires OAuth, preserve it; API-key authentication cannot bypass that layer. Current custom image has no SSH daemon; the setup guide includes the identical local private CLI alternative.
3. Live metadata download and the actual CustomersV3/CustTransBiEntities field mappings/read permissions remain unverified.
4. Live AST-001 reads and a fresh test customer's create/read/update/read/delete/absence checks were not run. No live ERP records were created; no demo accounts require cleanup from this implementation session.
5. No image was pushed to ACR, no Azure App Service was restarted or redeployed, and no actual Copilot connector connection/Test-panel run was performed.

Run the explicit `scripts/verify_deployed.py` check only after deploying and confirming readiness. Writes require its `--allow-writes --account TEST-COPILOT-...` switch. A passing source suite or valid error JSON must not be presented as successful live metadata/CRUD/Copilot verification.

## Rollback

Follow `ROLLBACK.md`: restore the privately captured original image/configuration, remove only finance hooks/config and the new secret setting, and disable/remove only the demo connector. Source/image removal does not delete ERP data or shared infrastructure.
