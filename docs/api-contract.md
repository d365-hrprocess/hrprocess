# TalentMatch API contract

All frontend requests use same-origin `/api` and `/auth` (the Docker gateway routes internally; native Vite defaults to HTTPS localhost port 7242), credentials included. JSON uses snake_case. API errors use `{detail: string}`. No secrets in frontend.

The confirmed `candidate_name` is a full name. In the canonical `D365_CONTRACT_MODE=sqlite` integration it maps to the existing F&O `CandidateName` field without splitting. Deploy the expanded F&O request contract before this backend mapping. See the [AXPP review and rollout](../d365/review/2026-10-25/README.md).

For a LinkedIn user, `User.name` and `User.email` can be empty strings when the provider does not supply them; only a valid email can prefill contact input. Optional `User.linkedin_profile` contains the sanitized standard public-profile fields listed below. Sign-in and ownership use the validated issuer/subject. The application form displays the supplied profile information and collects required contact details before submission.

- `GET /api/health` -> `{status: "ok", demo_mode: boolean, app_name: string}`
- `GET /api/auth/me` -> `{user: User|null, csrf_token: string, demo_mode: boolean, consent_version: string}`. Mutations send `X-CSRF-Token`.
- `POST /api/auth/demo` JSON `{role: "candidate"|"hr"}` -> auth response; requires CSRF, development demo only.
- `POST /api/auth/hr` JSON `{username: string, password: string}` -> auth response. Requires CSRF, exact backend `HR_USERNAME`/`HR_PASSWORD` matching, and HR login rate limiting. Success rotates the session/CSRF token and assigns the server-controlled HR role.
- `POST /api/auth/logout` -> `{success:true}`.
- `/auth/linkedin/login`, `/auth/hr/login` -> OIDC redirect. Optional `return_to` is a validated relative route bound to server-side OAuth state. Callback returns to that saved route on the configured frontend origin, defaulting to candidate applications or /hr. Callback query parameters cannot override it.
- `GET /api/jobs` and HR `/api/hr/jobs` -> `Job[]`; query `search`, `department`, `location`, `work_mode`, `employment_type` supported.
- `GET /api/jobs/{requirement_id}` -> `Job`.
- `POST /api/applications`: multipart `requirement_id`, `candidate_name`, `email`, `resume` PDF/DOCX, `phone`, `linkedin_url`, `consent_accepted=true`, `consent_version` from the current auth response (default `v1`). Full name and valid contact email are required before storing/submitting the resume. Older clients may omit contact fields only when complete contact data is already available in the session. Contact input cannot change the authenticated issuer, subject, or role. `Idempotency-Key` is stable per form submission; changing effective contact details with the same key returns 409. -> `{success:true, application_id:string, ai_status:string, message:string}`.
- `GET /api/me/applications` -> `Application[]`; `GET /api/applications/{id}` -> own Application. Candidate responses omit internal AI status and evaluation; application business status is retained. Metadata can use SQLite or PostgreSQL.
- `GET /api/hr/jobs/{requirement_id}/candidates` -> `{candidates:Candidate[], limit:number, returned_count:number}` (default limit 25, not total population).
- `GET /api/hr/jobs/{requirement_id}/candidates/{application_id}` -> `Candidate`.
- `POST /api/hr/jobs/{requirement_id}/candidates/{application_id}/retry` -> `{success, application_id, ai_status, message}`. HR session and CSRF required; failed connected evaluations only. Reuses valid cached output for save-only recovery and rejects active/completed work.
- `GET /api/hr/applications/{id}/resume` -> authorized PDF/DOCX stream; SQLite mode uses the acknowledged application snapshot's private reference. Legacy/demo mode uses its trusted adapter record.

```ts
interface LinkedInProfile {sub:string; issuer:string; name:string|null; given_name:string|null; family_name:string|null; email:string|null; email_verified:boolean|null; picture:string|null; locale:string|null}
interface User {sub:string; name:string; email:string; role:'candidate'|'hr'; issuer:string; linkedin_profile?:LinkedInProfile|null}
interface Job {requirement_id:string; title:string; description:string; responsibilities:string; required_skills:string[]; preferred_skills:string[]; qualifications:string; department:string; location:string; work_mode:string; employment_type:string; min_experience:number|null; max_experience:number|null; openings:number|null; status:string; published_date:string|null; deadline:string|null}
interface Application {application_id:string; requirement_id:string; job_title:string; candidate_name:string; email:string; applied_date:string|null; hr_status:string; resume_file_name:string; demo_fixture?:boolean}
interface Evidence {requirement:string; resume_section:string; project_name:string; evidence:string; reason:string; evidence_strength:string}
interface Gap {requirement:string; reason:string}
interface Evaluation {score:number; meets_minimum_criteria:boolean; match_label:string; score_justification:string; summary:string; matched_requirements:Evidence[]; partially_matched_requirements:Evidence[]; not_evidenced_requirements:Gap[]; evaluation_confidence:string; relevant_evidence:Evidence[]; model_deployment:string; prompt_version:string; rubric_version:string}
interface Candidate extends Application {ai_status:string; candidate_id:string; linkedin_url:string; evaluation:Evaluation|null; evaluation_date:string|null; demo_fixture:boolean}
```

Production integrations never fall back to fixtures. Demo jobs/candidates/AI results are explicitly fictional fixtures. Demo submission stores a real uploaded document and uses only a labeled sample evaluation, not an actual AI score.
