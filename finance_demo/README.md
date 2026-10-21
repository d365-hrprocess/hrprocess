# Temporary D365 Finance demo

Small customer/transaction API for Copilot Studio, isolated from the HR application's Python interpreter, dependencies and health checks. It runs on **Python 3.13.3** behind the existing Nginx `/finance` prefix on the existing external port 8080. The HR application keeps its Python 3.12 runtime.

## Configuration

All new non-secret settings are in `config/finance_settings.json`. Fixed target: tenant `008502d6-3f79-46f0-ab37-9354e3fe80ff`, client `d4c92ba1-9d5f-4dc6-a869-9ab83c5a1d32`, ERP `https://org1a43c536.operations.dynamics.com`, company USMF, entities `CustomersV3` / `CustTransBiEntities`, group 20, INR and Net30. Mutations are restricted to `TEST-COPILOT-` accounts.

Add only **`FINANCE_DEMO_CLIENT_SECRET`** to App Service. It must hold the actual Entra secret VALUE. The supplied Secret ID is not a credential. No finance `.env` file is automatically loaded into the HR process. The supplied secret expiry, **27 December 2026**, is operational information, not a token expiry or coded shutdown date.

D365 application access/roles and existing ingress compatibility remain prerequisites; configuration cannot grant ERP permissions or bypass App Service Easy Auth.

## Endpoints

Origin: `https://ubtihrprocess-egg3f7awbsbjdjd5.centralindia-01.azurewebsites.net`

| Method | Path | Purpose |
|---|---|---|
| GET | `/finance/health` | Minimal liveness only; no finance data |
| GET | `/finance/ready` | Protected readiness and capability status |
| GET | `/finance/metadata/status` | Protected sanitized metadata diagnostics |
| GET | `/finance/customers?limit=5` | Limited USMF customer list |
| GET | `/finance/customers/{account}` | Exact customer lookup |
| GET | `/finance/customers/{account}/transactions?limit=10` | Limited recent transaction history |
| POST | `/finance/customers` | Create an Organization test customer |
| PATCH | `/finance/customers/{account}` | Change a test customer's organization name |
| DELETE | `/finance/customers/{account}` | Delete a test customer and verify absence |
| GET | `/finance/swagger2.json` | Protected Swagger 2.0 definition |

All endpoints except minimal liveness require `X-Finance-Demo-Key`. This key is derived from the single finance client secret; it is not the raw secret. The connection credential is separate from backend-to-D365 bearer authentication.

## Metadata and outcomes

Metadata warms asynchronously with bounded download time/size and retries, namespace/inheritance parsing, entity/property/key validation, live entity read probes and atomic scoped caches. Startup, health and credentials are independent of the HR application. A request during warming returns a prompt retryable readiness error, not a multi-minute download wait. A valid last-known-good cache is retained if refresh fails, with its age/failure reported.

Customer capability is checked independently from transaction capability. Metadata presence alone is not permission or CRUD proof. Monetary amounts are decimal strings; limited history is never a complete balance. Reads use the fixed company and exact account filters. Writes are not automatically retried. A transport/verification failure can produce an uncertain outcome; check the exact record before retrying.

## Local verification

From the repository root, install and test only the isolated finance project:

```sh
cd finance_demo
uv sync --frozen --extra dev --python 3.13.3
uv run --frozen --extra dev python -m pytest
```

These source/mock/schema tests do not require a real finance secret and do not modify ERP data. Do not install the finance packages into the existing backend environment. The full Azure image still needs a complete-container regression check; local unit tests alone do not establish deployment readiness.

## Copilot and operations

- Import `connector/swagger2.json` (13 business operations: six original tools and seven financial reporting tools).
- Follow [the exact custom connector setup](copilot_customconnectorsetup.md).
- Paste [the final agent instructions](copilot_instructions.md).
- Enable user confirmation for create, update and delete tools.
- Use [ROLLBACK.md](ROLLBACK.md) for removal and restoring the prior deployment.

The public hostname is populated from the existing deployment configuration. See [VERIFICATION.md](VERIFICATION.md) for whether live public routing, ingress, D365 metadata/CRUD and the actual Copilot connection were verified; mocks and a JSON 503 do not prove live readiness.

## Deploy using the existing pipeline

1. From an approved Azure CLI session, run `bash finance_demo/scripts/capture_azure_baseline.sh` before changing the deployed image. Keep the ignored `finance-demo-baseline/` output privately. Resolve any Easy Auth or ingress incompatibility first.
2. Merge this feature into the company repository through your normal local transfer/review process. Preserve the company repository's current Azure login and `main` sitecontainer deployment workflow. The connected development repository does not push to the company repository.
3. Add only `FINANCE_DEMO_CLIENT_SECRET` in App Service, using the actual secret VALUE. Preserve all existing settings, including database TLS certificate paths and storage settings.
4. Run the existing Verify application / deployment workflow. It builds `Dockerfile.azure`; keep its commit-specific image tag and digest in the existing `ubticareers` ACR repository. Do not overwrite `phase1-v1` or replace the App Service startup command, target port or pull identity.
5. Verify the original `/healthz`, careers and HR routes, then follow the protected finance readiness and Copilot connection steps. A successful HR health check alone does not validate finance.

For authorized live verification from a secure machine with the same secret privately set, run `uv run --frozen python scripts/verify_deployed.py` from `finance_demo/`. It performs only reads. To explicitly authorize a complete live demo CRUD sequence against an unused account, run `uv run --frozen python scripts/verify_deployed.py --allow-writes --account TEST-COPILOT-102101`. Use a fresh account and record any uncertain/surviving record; the script never retries writes or automatically cleans up after a failed sequence. Actual Copilot tool execution must still be checked separately in its Test panel.

## Complete posted-financial reporting add-on (v1.1.0)

Existing CRUD, recent-history routes, auth/key and HR runtime remain intact. New read-only tools cover per-customer signed balances, positive open invoices, overdue invoices, exact posted invoice remaining amounts (including settled invoices), complete-source payment history and company-wide outstanding/overdue customers. QuickChart and chart endpoints are excluded from this change at the user's request.

Calculations use all accessible pages of CustTransBiEntities in USMF and metadata-verified AmountCur/SettleAmountCur, record keys and LedgerTransType members. Customer names join against paginated CustomersV3, without a 20-customer reporting cap. No total comes from the old limited history endpoint. Decimal arithmetic preserves signed credits/payments and separates currencies. Unknown classifications/dates are disclosed; malformed amounts or incomplete scans cannot produce final totals. Past cutoff dates use current remaining amounts and do not reconstruct historical settlements.

The first reporting request starts a bounded background scan and returns retryable report_preparing; repeat after five seconds. Only completed results are published, cached for 60 seconds and labeled with source counts, fetch window, scope and cache age. Output pagination defaults to 10, maximum 100, independently of calculations. Source budgets and business timezone Asia/Kolkata are in the existing finance settings file; no extra App Service variable is needed.

After normal image deployment, update the existing custom connector from connector/swagger2.json, select/add its seven new operations and replace the old agent instructions. See section 11 of copilot_customconnectorsetup.md. These Power Platform changes cannot be applied by backend deployment alone. Live financial reconciliation and actual Copilot tool tests must be performed separately; source tests are not proof of either.
