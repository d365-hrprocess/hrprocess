# TalentMatch AI — Phase 1

TalentMatch AI connects a React recruitment portal to Dynamics 365 Finance & Operations. Phase 1 delivers ERP job publication, LinkedIn candidate sign-in, private resume applications, AI-assisted resume/JD evidence analysis, and an HR review/retry workspace. Screening, interviews, offers, onboarding and employee maintenance are planned later phases.

F&O owns core recruitment records. SQLite or optional Supabase PostgreSQL stores portal metadata and complete evaluation provenance; private Azure Blob stores original documents; Redis coordinates sessions and background work. AI scores support human review and never automatically hire or reject a person.

## Start everything locally with Docker

The complete stack runs at **https://localhost:7242**. The LinkedIn callback is always:

```text
https://localhost:7242/auth/linkedin/callback
```

For hosted Azure deployment, use the separate [single-container ACR/App Service guide](docs/azure-app-service.md) and [IT resource request](docs/azure-it-request.md). `Dockerfile.azure` runs the web/API/ARQ/Redis processes in one non-root container. Azure uses the final public HTTPS hostname for LinkedIn; local Compose remains on port 7242. Company GitHub publishing is an explicit user step, and CI/CD stays disabled until company repository/identity variables are configured.

For the temporary GitHub → local checkout → company Azure Repos workflow, follow [Azure DevOps setup and CI/CD](docs/azure-devops.md). The root `azure-pipelines.yml` verifies the application, publishes the Azure image to ACR and optionally deploys it through an Azure Resource Manager service connection.

Docker Compose groups the HTTPS frontend gateway, API, worker and Redis under **talentmatch**. API/Redis ports are internal. After initial setup, Docker Desktop **Containers → talentmatch → Play** starts the existing group with one action.

From the source repository in PowerShell:

```powershell
.\scripts\docker\build.ps1
.\scripts\docker\setup.ps1
```

The setup helper uses root `.env`; if absent it can copy your existing `backend/.env`. Review connected integration credentials before starting. The default portable deployment is production mode with real services and demo disabled. Do not overwrite a working environment with the example.

Export a **source-free** bundle for another Docker machine:

```powershell
.\scripts\docker\export.ps1 -OutputDirectory .\dist\talentmatch
```

Transfer the generated folder/ZIP, supply private `.env` securely, and run its **setup.cmd** once. It imports images, creates/trusts a unique machine-local HTTPS certificate and starts the stack. The recipient needs Docker, no source code, Python, Node.js, Git or Visual Studio. Later use Docker Desktop's group Play. An `.env` change requires the supplied **start.cmd** to recreate configuration.

See [complete Docker setup, transfer and troubleshooting](docs/docker-local.md). The image archive moves software, not existing records, resumes or secrets. External integrations still require internet and authorized credentials. A first-time Run on a single image cannot configure the whole stack by itself.

## Documentation and shareable Word files

| Guide | Purpose |
| --- | --- |
| [Temporary finance demo](finance_demo/README.md) · [Copilot connector setup](finance_demo/copilot_customconnectorsetup.md) | Isolated Python 3.13.3 API under /finance, one new secret, Swagger 2.0 and rollback |
| [ubticareers technical overview (PDF)](docs/reference/ubticareers-technical-overview.pdf) · [Word document](docs/reference/ubticareers-technical-overview.docx) | Concise technology stack, application flow, Azure hosting, GitHub CI/CD and access summary |
| [Technical reference](docs/technical-reference.md) · [Word document](docs/reference/AI-Powered%20HR%20Recruitment%20Process%20Using%20D365%20-%20Technical%20Document.docx) | Architecture, processing flow, ERP mapping, AI validation, deployment, operations, limitations and roadmap |
| [Business reference](docs/business-reference.md) · [Word document](docs/reference/AI-Powered%20HR%20Recruitment%20Process%20Using%20D365-%20Business%20Document.docx) | Phase 1 business journey, value, governance, responsibilities, pilot criteria and future phases |
| [Local Docker handoff](docs/docker-local.md) | One-group startup, image export/import, certificates, recipient setup and data boundaries |
| [Azure App Service / CI/CD](docs/azure-app-service.md) · [IT request](docs/azure-it-request.md) | Single-container ACR deployment, company GitHub setup, Foundry requirements and public callback |
| [Supabase migration](docs/supabase.md) | Optional PostgreSQL configuration, consistent SQLite migration, shared coordination and rollback |
| [Environment reference](docs/environment.md) | Runtime settings, provider credentials and storage options |
| [Architecture](docs/architecture.md) · [API](docs/api-contract.md) | Data ownership and frontend/backend contracts |
| [D365 source guide](d365/README.md) · [Endpoints](docs/d365-endpoints.md) | Exact existing ERP tables, X++ contracts and three custom-service operations |
| [Authentication](docs/linkedin-auth.md) · [AI behavior](docs/azure-openai.md) | LinkedIn selected-job return, HR sign-in and evidence validation |
| [Operations](docs/deployment.md) · [Debugging](docs/debugging.md) · [Validation record](docs/validation.md) | Rollout, backups, recovery, terminal stages and executed verification |

The maintained Word documents include manual edits. `scripts/generate_reference_docs.py` creates separate drafts from the Markdown references; it does not merge or preserve manual Word edits. Screenshots use fictional demo identities and explicitly labeled sample evaluations.

![Candidate application form with fictional demo identity](docs/screenshots/phase1-candidate-application.png)

## What Phase 1 does

- Shows eligible open ERP jobs and their full requirement details.
- Returns LinkedIn sign-in to the chosen application, displays permitted profile fields, and collects confirmed contact details.
- Validates PDF/DOCX uploads and consent, privately stores the document, freezes the JD/contact snapshot, and submits one stable application to F&O.
- Extracts resume text, asks Azure OpenAI for contextual evidence, validates quoted sources, and attempts bounded correction of invalid model output.
- Saves a complete validated result durably before delivering the selected score/evidence to `UBHRMatchEvaluationTable`; completion requires ERP acknowledgement.
- Gives HR candidate lists, evidence, authorized resume access, refresh, and failed-evaluation retry. Candidates see application business status rather than internal AI diagnostics.

History and HR lists include acknowledged portal submissions. Historic ERP applications and ERP-only recruiter edits are not automatically synchronized. Readable text PDF/DOCX is supported; OCR for scanned documents is future work.

![HR evidence workspace with a fictional sample evaluation](docs/screenshots/phase1-hr-evidence.png)

## Supabase instead of local SQLite

The default is `METADATA_BACKEND=sqlite`. To use shared PostgreSQL metadata, migrate existing records explicitly and set backend-only settings:

```dotenv
METADATA_BACKEND=postgres
DATABASE_URL=postgresql://YOUR_USERNAME:URL_ENCODED_PASSWORD@YOUR_ACTUAL_DATABASE_HOST:5432/postgres
DATABASE_SSLMODE=verify-full
DATABASE_SSLROOTCERT=
DATABASE_CONNECT_TIMEOUT=10
D365_CONTRACT_MODE=sqlite
```

`D365_CONTRACT_MODE=sqlite` is the retained name of the lean three-operation **ERP contract**, independent of the metadata engine. No Supabase browser API key is needed. Follow [the migration guide](docs/supabase.md) before changing the backend.

Do not run independent local workers against one shared database with different Redis servers. Use one active connected stack at a time, or deliberately share the same private Redis endpoint/prefix and Blob container across stacks. Database sharing alone does not coordinate queues or migrate document bytes.

## Native developer workflow

Prerequisites: Python 3.11+, [uv](https://docs.astral.sh/uv/getting-started/installation/), Node.js 22.12+ and npm. From the repository root:

```bash
./scripts/install.sh
./scripts/dev.sh
```

The native development script uses explicitly labeled demo mode at http://localhost:5173 with its own HTTP API on port 8000. Candidate demo access uses fictional fixtures; HR needs backend-only `HR_USERNAME` and `HR_PASSWORD`. The native demo is separate from the connected HTTPS Docker deployment.

For your existing Windows native connected backend, keep Redis running and start these in separate `backend` terminals:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 7242 --ssl-certfile .certs\localhost.pem --ssl-keyfile .certs\localhost-key.pem
```

```powershell
.\.venv\Scripts\python.exe -m app.worker
```

Start `npm run dev` from `frontend` and trust the issuing local CA for Node's Vite proxy using `NODE_EXTRA_CA_CERTS`. The default Vite proxy targets https://localhost:7242. Do not run the native port-7242 server simultaneously with the Docker gateway on that port.

## ERP integration and secrets

The source pack uses existing model `UBHR_Nive` and exactly these tables: `UBJobRequirementTable`, `UBCandidateTable`, `UBJobApplicationTable`, `UBHRMatchEvaluationTable`. The service group exposes `getOpenRequirements`, `submitApplication`, and `saveCandidateEvaluation` under `UBHRPortalServiceGroup/UBHRPortalService`.

Keep optional `D365_TOP_CANDIDATES_PATH`, `D365_MY_APPLICATIONS_PATH`, and `D365_APPLICATION_PATH` blank for the three-operation contract. Metadata history/detail/ranking is served by FastAPI; React never receives ERP/database secrets. Deployed F&O services must retain compatible identifiers, indexes, enums, acknowledgement contracts and Memo-field support.

Use ignored `backend/.env` for native development or root `.env` for Docker. Never commit actual `.env`, database backups, certificates, private resumes or exported image archives. Integration endpoints and credentials belong in backend runtime settings, never `VITE_` variables.

## Verification and production boundary

```bash
./scripts/check.sh
```

The checks cover backend tests, lint, typed frontend tests/build and ERP bundle contracts. Browser verification uses synthetic uploads and fictional demo data. See [the current validation record](docs/validation.md) for executed results and limitations. Local tests do not replace live provider/F&O acceptance.

This Phase 1 release includes production-mode configuration and operational packaging, not a guarantee of complete enterprise readiness. Broader rollout requires approved HR identity/roles, live integration permissions, document-security policy, retention, monitoring, tested backup/restore and capacity planning. Human employment decisions remain with HR.

## Repository map

```text
backend/                  FastAPI, authentication, integrations, metadata, worker, tests
frontend/                 React portal, shared UI, typed API, tests
d365/                     Existing-table X++ source pack and serialized fixtures
deploy/docker/            Source-free runtime templates and recipient scripts
scripts/docker/           Build, local setup, image export and portability helpers
scripts/                  Installation, developer startup, checks, docs and browser smoke
docs/                     Technical/business references, operations, guides and screenshots
docs/reference/           Shareable generated Word documents
compose.yml               Localhost HTTPS connected application group
compose.build.yml         Source-based image build definitions
compose.demo.yml          Explicit fictional development/demo override
.github/                  Automated checks
```
