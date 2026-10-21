# Finance demo rollback and removal

The finance module must be removed independently of the existing HR application. Removing this code or its connector must not delete ERP records, remove shared infrastructure or clear existing application settings.

## Before deployment

Run the supplied `finance_demo/scripts/capture_azure_baseline.sh` using the existing approved Azure identity. Keep the result privately with the deployment record. Capture the actual current image/digest, App Service `main` container settings, target port, startup command, App Service configuration/authentication, storage setting, slots (if any), `/healthz`, `/api/health` and representative original routes. The historical `phase1-v1` tag is not assumed to be the current baseline.

Do not deploy without recording a recoverable original image reference. Use a fresh finance image tag in the existing ACR; do not overwrite the production tag. Use an existing deployment slot only if one is already provisioned. Do not create a slot/service for this demo.

## Immediate image rollback

1. Disable/suspend the finance deployment workflow or its trigger while restoring the original version, so CI does not immediately deploy it again. Preserve unrelated HR automation.
2. In **App Service → ubtihrprocess → Deployment Center → Containers → main**, restore the exact image/digest from the captured baseline. Preserve its existing registry authentication, environment settings, port **8080**, mounts and startup configuration. Do not change deployment mode or enable a new HTTP port.
3. Apply/restart through the existing approved deployment process. If using Azure CLI, use the recorded resource group `ubti-hrprocess-rg`, App Service `ubtihrprocess` and container `main`, supplying the actual captured original image reference; do not guess it from a tag in this document.
4. Verify the original `/healthz`, `/api/health`, careers routes, HR authentication, uploads and any in-use streaming/WebSocket behaviour. Finance routes should no longer serve finance data.
5. Remove only **`FINANCE_DEMO_CLIENT_SECRET`** from App Service if no finance process remains. Do not replace the entire settings list or delete existing credentials.

## Disable without removing the HR image

Set `enabled` to `false` in `finance_demo/config/finance_settings.json`, rebuild to a new image tag and redeploy through the approved process. This is a non-secret backend configuration change, not another App Service variable. Verify business/metadata/schema routes refuse access and that the original application health is unchanged. Merely removing the secret makes finance unconfigured; it is not a verified successful rollback of the image.

## Permanent source removal

Revert the finance feature commit/integration changes while preserving subsequent unrelated work. Remove the isolated `finance_demo/` directory and only its integration hooks:

- Finance Python build/runtime stage and copy/install lines added to `Dockerfile.azure`.
- Finance process added to the Azure Supervisor configuration.
- Exact `/finance` and `/finance/` routing/error handling additions in Nginx.
- Finance-only test/deployment instructions or CI steps added for this feature.

Do not remove the original Python 3.12 runtime, FastAPI dependencies, Nginx, Redis, Supervisor, original routes or original health services. No database schema migration is required or should be run as part of removing this module. Rebuild and verify a complete container before restoring production traffic.

## Remove the Copilot demo

Remove/disable the six Finance Demo USMF business tools in the agent, remove the associated custom connector connection and then delete the demo custom connector if unused. Remove demo-specific agent instructions/topics. Do not delete other connectors or shared Entra applications/permissions unless their owners separately approve it. Disabling the connector alone does not disable the HTTP API; restore/disable the backend as described above.

## ERP demo records

No rollback script automatically deletes D365 data. Keep a separate record of test accounts created during live verification and the client demo. At handover, list any surviving accounts and their state. Authorized administrators may review/delete those exact `TEST-COPILOT-` records through normal ERP validation. Do not force-delete related records or assume an uncertain write failed.

## Verification boundary

Record which checks were actually completed: original-app baseline, isolated tests, complete-container tests, live D365 metadata/read/CRUD, deployed HTTPS/ingress and actual Copilot connection. If credentials, deployment identity, public network access or a captured baseline are missing, report that specific blocker instead of claiming the rollback or demo is verified.
