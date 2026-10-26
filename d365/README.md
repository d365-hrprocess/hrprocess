# Paste guide: existing F&O tables with SQLite portal metadata

The [25 October AXPP review](review/2026-10-25/README.md) inventories the full 14-table project and documents the coordinated full-name fix. New application requests use the existing `CandidateName` field. Deploy the expanded F&O contract first; its optional first/last-name members keep the old backend compatible during rollout. The name fix requires no new tables, fields or App Service settings. Future ERP schema proposals in that review are separate from this change.

Use your existing model **`UBHR_Nive`** and these exact existing tables:

- `UBJobRequirementTable`
- `UBCandidateTable`
- `UBJobApplicationTable`
- `UBHRMatchEvaluationTable`

The flow is **show open jobs in React → submit candidate/application to F&O → save AI result in `UBHRMatchEvaluationTable`**. SQLite keeps the portal's timestamps, document references, hashes, consent, full profile/evaluation and retry metadata. No new D365 tables, fields, EDTs or enums are required. The source leaves existing technical metadata fields unchanged.

Use [TABLES.md](TABLES.md) for the complete screenshot field inventory and selected fields. This package replaces the earlier baseline-table/six-operation design. It is pasteable source, not an importable model or compiled deployable package; X++ build, database synchronization where metadata changed, and live service acceptance must take place in your F&O development environment.

For the reported class/parser errors and stale requirement form bindings, follow [build-errors.md](build-errors.md). It includes the corrected locking syntax and the four exact old-to-current requirement field mappings.

## 1. Open the existing F&O project

1. Open the supported Visual Studio instance on your F&O developer VM/unified development environment. It must include the Dynamics 365 Finance and Operations tools and matching platform/application metadata; a plain laptop Visual Studio 2022 installation cannot compile X++ by itself.
2. Open your existing project in model `UBHR_Nive`, or create an F&O project targeting that **existing model**. Add the four existing table elements to the project if needed; do not create replacements.
3. Inspect existing EDT lengths, enum symbols and index properties using [TABLES.md](TABLES.md). In particular, application/candidate/evaluation IDs need 40-character capacity, `IdentityKey` needs 64 characters, and JD/evidence text must not truncate.
4. Verify existing unique indexes on `CandidateId`, `IdentityKey`, `ApplicationId` and `EvaluationId`, and unambiguous requirement IDs in the company. Retry safety depends on these indexes, even though no new columns are needed. If an index/EDT must change, make that metadata change in your development model and synchronize after build.
5. Inspect the existing tables' `initValue()` / `validateWrite()` rules. The service honors them; if your model already requires another screenshot field such as `CandidateSource` or `RecruiterId`, provide its existing valid default through your normal table logic before testing. Do not bypass validation or add new columns.
6. The service uses standard `System` and `Newtonsoft.Json` .NET types. If the compiler cannot resolve them, add references to the supported assemblies already included with your F&O platform. Do not install an arbitrary NuGet assembly into AOS.

## 2. Paste the five contracts and service class

Create these **six classes** in this order, using each matching file from [xpp/](xpp/):

| Class | Purpose |
| --- | --- |
| `UBHRPortalRequirementContract` | Selected existing requirement fields for React |
| `UBHRPortalRequirementsContract` | `Requirements` collection returned by job lookup |
| `UBHRPortalApplicationRequestContract` | Candidate contacts/identity and stable application/job IDs |
| `UBHRPortalEvaluationRequestContract` | Stable evaluation/application/job IDs and core AI result |
| `UBHRPortalResultContract` | Actual success acknowledgement and persisted IDs |
| `UBHRPortalService` | Reads the job and writes the existing candidate/application/evaluation tables |

For each class:

1. Right-click the project → **Add → New item → Class**.
2. Set the exact class name from the table above.
3. Open the X++ editor, remove the generated empty class, and paste the **entire matching `.xpp` file**, including attributes and class declaration. Do not paste a complete class inside the generated braces.
4. Save and repeat. Once all six classes exist, build the project.

Existing enum element names were not supplied. The service resolves existing types through `DictField`/`DictEnum` and uses these private configuration methods:

| Edit point in `UBHRPortalService` | Default |
| --- | --- |
| `requirementStatusFieldId()` | `RequirementStatus` on `UBJobRequirementTable` |
| `openRequirementStatus()` | `Open` |
| `pendingAIStatus()` | `Pending` |
| `completedAIStatus()` | `Completed` |

If your existing enum/string values differ, change only these methods to the actual **enum element names/string values**. Do not guess numeric ordinals or create new enums. Eligible job responses always return canonical `RequirementStatus="Open"`, even if your local active symbol is `Active` or `Approved`. `ApplicationStatus` and any candidate status use the existing table's `initValue()` defaults. Eligibility uses `OpenDate` / `CloseDate` as inclusive UTC calendar dates; blank dates impose no bound. `JobStatus` and `ClosingDateTime` are not eligibility sources for this simplified service.

`getOpenRequirements()` has no parameter. The two writes have exactly one parameter named **`_request`**; keep the name because it is the JSON wrapper sent by FastAPI.

## 3. Create the service and service group

1. Add or open the AOT **Service** named `UBHRPortalService`.
2. Set its **Class** property to the X++ class `UBHRPortalService`.
3. Under **Operations**, expose only these three public methods. If replacing the earlier source pack, remove its obsolete `getTopCandidates`, `getMyApplications` and `getApplication` operation references from this service; those reads now use SQLite.

| Operation | Request | Return |
| --- | --- | --- |
| `getOpenRequirements` | `{}` | `UBHRPortalRequirementsContract` |
| `submitApplication` | `{"_request": {...}}` | `UBHRPortalResultContract` |
| `saveCandidateEvaluation` | `{"_request": {...}}` | `UBHRPortalResultContract` |

4. Add an AOT **Service Group** named `UBHRPortalServiceGroup`.
5. Add the **AOT service** `UBHRPortalService` under the group's Services node. Keep the service-reference name `UBHRPortalService`; it becomes the middle endpoint path segment. A class by itself is not a service group.
6. Set `AutoDeploy=Yes` if the property is available in your platform. Current F&O service groups deploy through the supported model build/deployment process.
7. Save and build. Synchronize the development database if you changed EDT/index metadata. Follow your development environment's service refresh/restart procedure if the successfully built group has not appeared.

All three operations use **HTTP POST**:

```text
https://<your-fo-host>/api/services/UBHRPortalServiceGroup/UBHRPortalService/getOpenRequirements
https://<your-fo-host>/api/services/UBHRPortalServiceGroup/UBHRPortalService/submitApplication
https://<your-fo-host>/api/services/UBHRPortalServiceGroup/UBHRPortalService/saveCandidateEvaluation
```

For sandbox/production, deploy the model through your supported F&O build/package pipeline. This source folder cannot deploy directly to F&O.

## 4. Grant access to the integration user

1. Create/use a security privilege with **Service Operation** entry points for the three operations. Use the designer's operation selector and its invoke permission.
2. Grant effective table access: `UBJobRequirementTable` **Read**; the other three tables **Read/Create/Update**. No Delete permission is needed.
3. Reference the privilege from a duty and a dedicated integration role. Assign that role to the enabled integration user.
4. Give the user access to the intended legal entity and set its startup/default company to the same value as `D365_COMPANY`.
5. Register a server-to-server application in your Entra tenant. Record tenant ID, client ID and a client secret **value**.
6. In F&O, open **System administration → Setup → Microsoft Entra applications** (older environments say Azure Active Directory applications), add that client ID and map it to the integration user's User ID.

The backend requests `<D365_BASE_URL>/.default` using client credentials. The resource is your F&O origin, not Microsoft Graph. Job reads run in the mapped user's company; write requests require `Company` to equal `curExt()`. This version does not switch companies from browser input.

Keep the token and secret on the backend. The browser never receives the integration credentials. Test using the dedicated user; a successful administrator test does not prove the role is sufficient.

## 5. Put endpoints in `backend/.env`

Merge [backend.env.example](backend.env.example) into your existing ignored **`backend/.env`**, preserving LinkedIn, HR Entra, private Blob, Redis and Azure OpenAI settings:

```dotenv
DEMO_MODE=false
D365_ENABLED=true
D365_CONTRACT_MODE=sqlite
SQLITE_DATABASE_PATH=.local/talentmatch.sqlite3
D365_BASE_URL=https://<your-fo-host>
D365_TENANT_ID=<tenant-id>
D365_CLIENT_ID=<client-id>
D365_CLIENT_SECRET=<secret-value>
D365_COMPANY=<integration-users-startup-company>
D365_PARAMETER_NAME=_request
D365_REQUIREMENT_PATH=/api/services/UBHRPortalServiceGroup/UBHRPortalService/getOpenRequirements
D365_SUBMIT_APPLICATION_PATH=/api/services/UBHRPortalServiceGroup/UBHRPortalService/submitApplication
D365_SAVE_EVALUATION_PATH=/api/services/UBHRPortalServiceGroup/UBHRPortalService/saveCandidateEvaluation
D365_REQUEST_WRAPPERS={"get_open_requirements":""}
```

With `D365_CONTRACT_MODE=sqlite`, history, detail and HR ranking read acknowledged portal-submitted records from SQLite. Leave `D365_TOP_CANDIDATES_PATH`, `D365_MY_APPLICATIONS_PATH` and `D365_APPLICATION_PATH` empty; no additional F&O methods are required. `legacy` explicitly selects the earlier full six-operation adapter and needs its old contracts; it is not this source pack.

**SQLite:** the API and worker must resolve `SQLITE_DATABASE_PATH` to the same durable local file. Start both from `backend/` for the relative default, or use one shared absolute path. In containers, mount the same persistent local volume and use `/app/data/talentmatch.sqlite3` for both. Back up the database using a SQLite-consistent backup procedure; it contains candidate data. Do not place a WAL-mode SQLite file on SMB/NFS or run independently copied databases across hosts. This simple deployment is for one host/shared local volume; multiple API/worker hosts need a server database design.

Actual resume files remain in private Blob Storage. SQLite contains filenames/references, metadata and evaluation results, not uploaded PDF/DOCX bytes. Redis remains required for connected sessions/queue delivery. API and worker must share the Redis namespace and Blob configuration as well as SQLite.

## 6. Keep the React endpoint at FastAPI

There is **no D365 endpoint to put in frontend code**. React continues to use `/api` and `/auth`. For your HTTPS backend on port 7242, use the Vite proxy target in `frontend/vite.config.ts`:

```typescript
proxy: {
  '/api': { target: 'https://127.0.0.1:7242', changeOrigin: true, secure: true },
  '/auth': { target: 'https://127.0.0.1:7242', changeOrigin: true, secure: true },
}
```

Keep Node's mkcert root-CA trust configured, then restart Vite. Preserve the exact LinkedIn callback:

```dotenv
BACKEND_BASE_URL=https://localhost:7242
FRONTEND_BASE_URL=http://localhost:5173
LINKEDIN_REDIRECT_URI=https://localhost:7242/auth/linkedin/callback
```

From **`backend/`**, start your HTTPS API:

```powershell
uv run --frozen uvicorn app.main:app --host 127.0.0.1 --port 7242 --ssl-certfile .certs\localhost.pem --ssl-keyfile .certs\localhost-key.pem
```

From another terminal in **`backend/`**, start the connected worker:

```powershell
uv run --frozen python -m app.worker
```

Then run `npm run dev` from `frontend/`. Backend `.env` changes require restarting API and worker. Never use `VITE_*` variables for D365/Azure/provider secrets.

## 7. Test the three steps

Use a synthetic candidate and text-based resume:

1. **Job lookup:** ensure an existing requirement has a unique `RequirementId`, full JD, active `RequirementStatus`, valid `OpenDate` / `CloseDate` and real advertised numbers in the integration user's company. React must show that same requirement.
2. **Application:** sign in from that job, confirm contacts, upload a PDF/DOCX and accept consent. Verify one `UBCandidateTable` row and one linked `UBJobApplicationTable` row. Repeat the same submission after a timeout; its stable `ApplicationId` must not create a duplicate.
3. **AI persistence:** run the worker with real Blob/Azure OpenAI settings. Verify the actual score/evidence in `UBHRMatchEvaluationTable`, the same `OverallScore` on the application and completed `AIStatus`. Verify full comparison details, consent and private document metadata in SQLite. A pending or failed AI run must not display a fabricated score.

Import [UBHRPortal.postman_collection.json](UBHRPortal.postman_collection.json) for the three F&O operations; [examples/](examples/) provides request/response bodies. Direct Postman fixtures are fictional and do not create SQLite metadata or upload a resume, so use the portal for the complete application-to-AI workflow.

Verify invalid/cross-company IDs, changed retry payloads and duplicate evaluation requests. Candidate history and HR ranking show records acknowledged through this portal; they do not import pre-existing F&O applications. External recruiter edits in F&O are not automatically synchronized back to SQLite in this three-operation version; the portal currently shows its initial human-review `Submitted` snapshot.

## Validation boundary

[validate_bundle.py](validate_bundle.py) checks source names, screenshot inventory, fixture contracts and backend compatibility. The cloud workspace has no X++ compiler or F&O deployment access. A static or mocked contract pass cannot establish X++ compilation, database synchronization, enum compatibility, service authorization or a live AI/D365 save; those remain the acceptance steps above.
