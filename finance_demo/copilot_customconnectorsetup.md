# Copilot Studio setup — this Azure deployment

Target API origin:

`https://ubtihrprocess-egg3f7awbsbjdjd5.centralindia-01.azurewebsites.net`

Finance base path: `/finance`.

This module requires a new container image built from this repository's `Dockerfile.azure`; adding a setting to the old image does not install the module. Reuse the existing ACR and App Service. Do not create an additional Azure service or replace the HR application's settings.

## 1. Add exactly one App Service setting

In Azure Portal, open **App Service → ubtihrprocess → Settings → Environment variables → App settings → Add**:

```text
Name:  FINANCE_DEMO_CLIENT_SECRET
Value: the actual Microsoft Entra client secret VALUE
```

Enter the value privately in Azure; do not put it in source code, this document, Copilot instructions or the connector definition. The supplied identifier `fb07b3d3-b14c-4880-b49e-04d818160c0f` is the **Secret ID**, not the secret VALUE, and must never be entered as the value.

Save the setting and restart the application through the normal deployment process. Preserve every existing application setting. Tenant, client, ERP URL, company, limits, metadata settings and hostname are already in `finance_demo/config/finance_settings.json`; do not add variables for those values.

The secret's supplied expiry is **27 December 2026**. Rotate before expiry; the backend uses actual token expiry rather than treating this date as a token lifetime.

No new app registration or separate OAuth setup is needed for the derived connector key: the backend uses the supplied existing Entra application. The Entra application must already be authorized by the D365 environment to read `CustomersV3` and `CustTransBiEntities` and create/update/delete the designated test customers. A secret alone does not grant ERP permissions.

## 2. Check ingress and automatic metadata warm-up

Before declaring the demo ready, verify that existing App Service authentication/ingress accepts calls from the connector. App Service Easy Auth runs before Python. If it requires an Entra token or ingress blocks Copilot, keep that policy intact and arrange approved connector OAuth/network access. Do not disable site-wide authentication or expose ERP writes merely to pass a test.

After the new image starts, metadata warming runs in the isolated finance process. It acquires its own D365 token and downloads/validates `$metadata`; no short-lived bearer token needs to be copied into Copilot.

Actual endpoints:

- Minimal public liveness: `https://ubtihrprocess-egg3f7awbsbjdjd5.centralindia-01.azurewebsites.net/finance/health`
- Protected readiness: `https://ubtihrprocess-egg3f7awbsbjdjd5.centralindia-01.azurewebsites.net/finance/ready`
- Protected safe metadata diagnostics: `https://ubtihrprocess-egg3f7awbsbjdjd5.centralindia-01.azurewebsites.net/finance/metadata/status`
- Protected Swagger download: `https://ubtihrprocess-egg3f7awbsbjdjd5.centralindia-01.azurewebsites.net/finance/swagger2.json`

Liveness is not proof of metadata readiness, ERP permissions or CRUD. Readiness must return HTTP 200 with customer capability ready after live read probing. Inspect transaction capability separately: a successful customer probe does not prove transaction access. A 503 response means the finance module is not ready; do not describe it as working.

## 3. Obtain the connector key privately

If an existing approved **App Service application-container console/SSH** is available, enter that container (not the separate Kudu container) and run:

```sh
cd /opt/finance
/opt/finance/venv/bin/python -m finance_demo_api.cli show-connector-key
```

The current custom image does not install an SSH daemon, so the portal SSH button is not assumed to work. Do not add an SSH server or change the main application just to obtain this key. If an application-container console is unavailable, derive the identical key locally from the connected source using the same single secret VALUE. From the repository's `finance_demo` directory in PowerShell:

```powershell
uv sync --frozen --python 3.13.3
$financeDemoSecret = Read-Host "Finance Entra secret VALUE" -AsSecureString
try {
    $env:FINANCE_DEMO_CLIENT_SECRET = [System.Net.NetworkCredential]::new("", $financeDemoSecret).Password
    uv run --frozen python -m finance_demo_api.cli show-connector-key
} finally {
    Remove-Item Env:\FINANCE_DEMO_CLIENT_SECRET -ErrorAction SilentlyContinue
    Remove-Variable financeDemoSecret -ErrorAction SilentlyContinue
}
```

This is the same credential used by the backend, not another App Service setting or a new secret. Keep both the secret and displayed derived key private.

This administrator command intentionally displays the derived connector credential. Copy it privately; do not include its output in screenshots, support logs, prompts or committed files. There is no public endpoint that reveals it.

The connector key is the hexadecimal HMAC-SHA256 digest derived from the secret VALUE and fixed tenant/client settings. It is stable across process restarts and workers. It is **not** the raw D365 secret. No second App Service variable or persistent secret store is needed.

To call protected diagnostics from a private Bash terminal without typing the key into command history:

```sh
read -r -s -p 'Finance connector key: ' finance_demo_key
printf '\n'
curl --fail-with-body --silent --show-error \
  -H "X-Finance-Demo-Key: ${finance_demo_key}" \
  'https://ubtihrprocess-egg3f7awbsbjdjd5.centralindia-01.azurewebsites.net/finance/ready'
curl --fail-with-body --silent --show-error \
  -H "X-Finance-Demo-Key: ${finance_demo_key}" \
  'https://ubtihrprocess-egg3f7awbsbjdjd5.centralindia-01.azurewebsites.net/finance/metadata/status'
unset finance_demo_key
```

Use the same credential in the Copilot connection. Do not put it in a tool input, agent instruction or conversation.

## 4. Import the custom connector

In the target Power Platform environment, open **Power Apps → Custom connectors → New custom connector → Import an OpenAPI file**. Use the custom connector management link in Copilot Studio if that UI directs you there.

- Connector name: **Finance Demo USMF**.
- Import file: `finance_demo/connector/swagger2.json` from this repository.
- Verify scheme **HTTPS**, host **ubtihrprocess-egg3f7awbsbjdjd5.centralindia-01.azurewebsites.net**, base URL **/finance**.
- Paths are `/customers` and `/customers/{account}`; do not add another `/finance` to a path or operation.
- The document is Swagger/OpenAPI **2.0**; do not substitute FastAPI's generated OpenAPI 3 definition.

## 5. Configure the connection and test

In the connector's **Security** page, verify **API Key** authentication:

| Setting | Value |
|---|---|
| Parameter label | Finance Demo Key |
| Parameter name | `X-Finance-Demo-Key` |
| Parameter location | Header |

Create/save the connector, then open **Test → New connection** and paste the privately derived connector key in the credential field. The Swagger security definition supplies the header. Never add the raw key as a normal operation parameter for the AI to populate.

Test **GetFinanceCustomer** with account `AST-001`. A real JSON success is required; an HTML sign-in page, well-formed error or 503 is not a successful customer read. Test **GetFinanceCustomerTransactions** for `AST-001` with limit 10 separately.

## 6. Add the business tools

In Copilot Studio, open your agent → **Tools → Add a tool → Connector/custom connector**, select **Finance Demo USMF** and its connection. Add the baseline operations below (the UI may show their friendly summaries), then the eight v1.1.0 operations in section 11:

| Operation ID | Purpose |
|---|---|
| `ListFinanceCustomers` | Bounded USMF customer list |
| `GetFinanceCustomer` | Exact account lookup |
| `GetFinanceCustomerTransactions` | Limited recent transactions |
| `CreateFinanceCustomer` | Create a TEST-COPILOT- Organization customer |
| `UpdateFinanceCustomer` | Update that test customer's organization name |
| `DeleteFinanceCustomer` | Delete that test customer subject to ERP validation |

Do not add health, readiness, metadata, schema or key-administration functions as conversational tools; the supplied connector does not expose them as operations.

## 7. Enable confirmation on ALL write tools

For **CreateFinanceCustomer**, **UpdateFinanceCustomer** and **DeleteFinanceCustomer**, open each tool's settings and set **Ask the end user before running → Yes**. This is **not enabled by default**; agent instructions alone are not the whole configuration.

If that setting is unavailable in your current UI/orchestration mode, implement an explicit confirmation topic: collect inputs → display the proposed action/account/company/name → ask a Yes/No question → invoke the write action only from the **Yes** branch. The **No/cancel** branch ends without an action. Ensure the agent cannot bypass this branch by invoking the write action independently.

## 8. Paste the supplied agent instructions

Copy the ready-to-paste text in `finance_demo/copilot_instructions.md` into the agent's Instructions field. Keep credential values out of instructions. Save the agent.

## 9. Run the client demo in the Test panel

Use a **fresh** account for each complete CRUD demonstration, for example `TEST-COPILOT-102101`. First read that exact account to confirm absence; if it exists, choose another fresh account. Do not reuse or delete someone else's test record.

1. “Show 5 USMF customers.”
2. “Show customer AST-001.”
3. “Show the latest 10 transactions for AST-001.” Confirm the answer says these are limited records and does not invent a complete balance.
4. “Create customer TEST-COPILOT-102101 named Copilot Demo Customer.” The agent must ask for confirmation. Confirm explicitly.
5. “Show customer TEST-COPILOT-102101.” Verify the returned name, group 20, INR and Net30.
6. “Change TEST-COPILOT-102101 to Copilot Demo Customer Updated.” Confirm explicitly, then read it again.
7. “Delete TEST-COPILOT-102101.” Confirm explicitly. The tool must report verified deletion; a later exact read must report not found.
8. Propose another create/update/delete and reply **Cancel** when asked. Confirm that no write tool ran and the ERP record is unchanged.
9. Try to change AST-001. The agent and API must refuse because it is not a TEST-COPILOT- account.

Inspect the test trace, not only the agent's prose. Record actual tool execution and ERP results. If a write outcome is uncertain, verify the exact account before any retry; do not automatically repeat it. Record any surviving demo accounts for manual cleanup.

Use the **Copilot Studio Test panel** for this controlled demo. Publishing/channel availability depends on your organization's Copilot Studio and connector licensing; do not assume this setup grants publishing rights.

## 10. Troubleshooting and rotation

| Symptom | Check / action |
|---|---|
| 401 JSON | Missing/wrong derived connector key. Confirm the connection uses the header, not a body/query parameter. Recreate the connection credential after rotation. |
| 403 JSON | Test-prefix rule, D365 permissions or ingress restriction. Inspect sanitized diagnostics; do not widen the account prefix to bypass the restriction. |
| 404 JSON | Exact customer absent, wrong deployment/path, or finance disabled. Confirm `/finance` occurs only once. An upstream permission/timeout error is not absence. |
| 503 JSON | Missing secret, warming metadata, disabled capability or upstream unavailability. Inspect protected `/ready` and `/metadata/status`; wait for successful readiness, not an arbitrary delay. |
| 504 / uncertain write | Do not retry the write. Perform an exact read and establish the actual ERP state first. |
| HTML / redirect to login | Easy Auth or another ingress layer is handling the request before Python. Preserve its policy and arrange approved connector OAuth/access. An API key cannot override Easy Auth. |
| Old/missing connector actions | Save/update the connector, allow propagation, refresh/re-add the tool and recreate/reselect the connection if necessary. Changes are not always immediate. |
| D365 login failure | Verify client secret VALUE, not Secret ID, application authorization and actual secret validity. Do not paste D365 bearer tokens into Copilot. |

Rotating `FINANCE_DEMO_CLIENT_SECRET` changes the derived connector key. Save only the new secret setting in App Service, restart/reload as required, privately run the CLI again and update/recreate the Copilot connection credential. Existing connector keys will stop working. Keep the new value out of logs and source control.

## 11. Upgrade the existing connector for complete financial reports

This add-on includes balances, open/overdue invoices, exact invoice remaining amounts, payment history and company reports. Chart functionality is excluded at your request. No new App Service setting, key, Azure service or deployment change is needed. Keep the current customer CRUD and recent-history tools.

1. Build/deploy the new revision through the existing authorized ACR/App Service pipeline. Metadata parser/cache version 2 rewarms once to capture actual enum members. Original HR health is independent.
2. Privately call the protected `/finance/ready`. Existing customer/transaction capabilities should remain readable. Inspect `data.reporting`: `schema_verified` means the required AmountCur/SettleAmountCur properties, keys and actual LedgerTransType members were resolved. This is not proof of live ERP amount reconciliation. Unsupported reporting fields/types disable only the add-on.
3. Power Apps → Custom connectors → existing **FinanceDemoUSMF → Edit → Update from OpenAPI file**, upload the updated `finance_demo/connector/swagger2.json` (version **1.1.0**), save/update. Keep the existing host, `/finance` base path and API key connection. If your UI has no file-update command, import a versioned connector and switch tools only after testing; retain the old working connector until then.
4. Refresh the connection and test the new actions. First complete-data request returns retryable `report_preparing` promptly. Wait five seconds and repeat the same request. Full scans prepare separately with a 120-second budget, at most 50,000 records per source and 2,000 pages. Bound violations and later-page failures return errors, never partial balances. Completed results are cached for 60 seconds with disclosed age/fetch window; at most two scopes are retained in the existing single finance worker. Output pagination up to 100 rows does not limit underlying calculations.
5. Copilot Studio → your agent → Tools: refresh/select and add all seven new operations below. Backend deployment and connector updates do not automatically add agent tools. Select **Shared account → FinanceDemoUSMF** for the demo audience; operations use the backend application's permissions, not each user's D365 roles.

| New operation ID | Inputs |
|---|---|
| GetCustomerBalance | account; optional currency |
| GetCustomerOpenInvoices | account; optional currency, limit, offset |
| GetCustomerOverdueInvoices | account; optional currency, as_of_date, limit, offset |
| GetInvoiceRemainingAmount | account, invoice_number; optional currency |
| GetCustomerPaymentHistory | account; optional currency, date_from, date_to, limit, offset |
| ListCustomerBalances | optional currency, limit, offset; no account |
| ListOverdueCustomers | optional currency, as_of_date, limit, offset; no account |

6. Replace the old Instructions text with the updated **copilot_instructions.md**, save, and start a fresh test conversation. This removes the old blanket refusal for outstanding/overdue, while retaining the prohibition on calculating balances from the limited recent-history tool. Keep explicit confirmation enabled for all three existing CRUD write tools. New financial tools are read-only.
7. Run these prompts and inspect actual tool traces:

- “Show AST-001's outstanding balance and open invoices.” Expect GetCustomerBalance/GetCustomerOpenInvoices.
- “How much does AST-001 owe?” Expect GetCustomerBalance, with separate currencies/net/invoice/credit totals.
- “What remains on invoice FTI-00000022 for AST-001?” Expect GetInvoiceRemainingAmount. This example is not a claim the invoice exists; report absence/ambiguity honestly.
- “Which AST-001 invoices are overdue as of 2026-10-21?” Expect GetCustomerOverdueInvoices.
- “List overdue customers in USMF as of 2026-10-21.” Expect ListOverdueCustomers without forcing an account.
- “Show customers' outstanding amounts in INR.” Expect ListCustomerBalances, positive-net display filter and separate all-source totals.
- “Show AST-001 payment history in INR.” Expect GetCustomerPaymentHistory, not the limited recent preview.
- Test missing account clarification for customer-specific requests, no account question for company reports, mixed currencies, empty results, exact invoice ambiguity, further output pages, preparing/failure and unknown dates/classifications.
- Repeat fresh TEST-COPILOT- CRUD with confirmation and cancellation using section 9. Ordinary customer writes remain prohibited.

Actual routes, all protected by the existing X-Finance-Demo-Key header (GET, no body):

```text
https://ubtihrprocess-egg3f7awbsbjdjd5.centralindia-01.azurewebsites.net/finance/customers/AST-001/balance?currency=INR
https://ubtihrprocess-egg3f7awbsbjdjd5.centralindia-01.azurewebsites.net/finance/customers/AST-001/open-invoices?currency=INR
https://ubtihrprocess-egg3f7awbsbjdjd5.centralindia-01.azurewebsites.net/finance/customers/AST-001/overdue-invoices?currency=INR&as_of_date=2026-10-21
https://ubtihrprocess-egg3f7awbsbjdjd5.centralindia-01.azurewebsites.net/finance/customers/AST-001/invoices/FTI-00000022?currency=INR
https://ubtihrprocess-egg3f7awbsbjdjd5.centralindia-01.azurewebsites.net/finance/customers/AST-001/payments?currency=INR
https://ubtihrprocess-egg3f7awbsbjdjd5.centralindia-01.azurewebsites.net/finance/reports/customer-balances?currency=INR
https://ubtihrprocess-egg3f7awbsbjdjd5.centralindia-01.azurewebsites.net/finance/reports/overdue-customers?currency=INR&as_of_date=2026-10-21
```

### Amount basis and reconciliation

Original minus settled AmountCur gives signed remaining amounts in transaction currency. Gross positive invoice totals are separate from net customer balances and negative unapplied credits. All source pages are read; output pagination does not change totals. Invoice/overdue classification is restricted to metadata-verified named types. Unknown types/amounts/dates are never guessed; inspect classification flags and unknown counts/amounts. An incomplete classification is disclosed even if net balance scanning is complete.

Due today is not overdue. Sentinel/missing dates are unknown. A past as_of_date changes date eligibility using **current** remaining values; it does not reconstruct historical settlement balances. Compare with the same posted D365 company/account/currency records and original/settled/remaining values, not an ERP screen showing accounting currency or a historical settlement reconstruction. Observe fetch window and 60-second cache age; OData pagination is not an ERP-wide point-in-time snapshot.

Run the read-only deployed helper from finance_demo with the same secret available privately:

```sh
uv run --frozen python scripts/verify_reporting.py --account AST-001 --currency INR --as-of-date 2026-10-21
```

Optional `--invoice-number` must name an actual posted invoice. The helper does not create/update/delete customers or test Copilot. The original verify_deployed.py helper retains its explicit --allow-writes gate for fresh test-account CRUD.

| Result | Meaning/action |
|---|---|
| report_preparing / report_busy | Complete scan preparing; repeat the same read after five seconds. Do not manufacture a total from recent history. |
| report_scan_timeout / record/page/size limit | Full scan could not finish within bounds; no final totals are available. Review scope/volume, not a partial-source workaround. |
| financial_fields_unverified / financial_enum_unverified | Monetary field/type/enum contract unsupported or unverified; inspect protected metadata diagnostics. Baseline tools remain separate. |
| invoice_ambiguous | Inspect posted invoice rows and qualify currency/voucher/key; never pick the first. |
| Old outstanding/overdue refusal | Update connector, add the seven new tools, replace instructions, save and start a fresh conversation. |

Record mocked checks, live ERP reconciliation, deployed API checks and actual Copilot tests separately in VERIFICATION.md. Passing mocked arithmetic checks is not proof that the live environment's amounts or agent answers were verified.
