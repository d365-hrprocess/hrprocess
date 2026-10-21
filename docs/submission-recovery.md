# Resume submission recovery — 21 October 2026

## Observed incident

The submitted production log reports a durable submission in stage `ready`, followed by an actual HTTP 500 from the F&O custom `submitApplication` operation. Correlation ID `ea32061e-2d5a-4ee7-a348-cf833572dc6c` maps to F&O activity `cb035259-4dd8-0000-ec66-bed5d84ddd01` at 07:35:07 UTC (1:05:07 PM IST). The response is a generic ErrorException; its underlying X++/Infolog message is not in the supplied log. The configured recruitment company in that trace is DAT. Do not substitute the separate finance demo's USMF configuration.

A subsequent request changed the application fingerprint while reusing the same idempotency key. The backend correctly returned 409 to protect the already-frozen application. The portal allowed those fields/resume to change after an uncertain attempt; that UI behavior is fixed in this revision. The resume-upload stage and ERP application acknowledgement are separate. This incident does not establish a Blob upload failure.

## Portal fix

After an uncertain submission/network/ERP failure, the page retains the exact original multipart body and key, freezes contact/resume/consent inputs, and offers Retry original application. It does not silently create a fresh key or replace a pending resume. Validation/auth/rate-limit responses known to occur before reservation leave the form editable. The API now returns the existing bounded, sanitized F&O diagnostic fields; the candidate sees a support activity reference, while technical details remain in the JSON diagnostic/log for support. Unknown provider text, credentials and candidate values are not exposed.

Backend idempotency, frozen outbox data, application IDs, resume storage and ERP validation are unchanged. Do not delete a resume, Redis key or PostgreSQL application record to bypass a conflict. Do not refresh merely to force a new key while an earlier application is still pending.

## Remaining F&O investigation

Ask the F&O administrator/developer to inspect the activity above in the environment's server telemetry/Infolog/developer trace and provide the actual exception. Check the deployed service class/contract and wrapper against d365/xpp; verify the actual legal entity, field/EDT capacities, existing unique indexes, initValue/validateWrite mandatory fields and configured enum symbols. These are investigation targets, not a diagnosis. Do not guess an enum ordinal, truncate IDs, disable validation or change the legal entity without the exception and actual metadata.

Once the actual F&O failure is corrected, the existing worker reconciliation can resume the original durable record using its frozen ApplicationId and resume reference. Confirm the real D365 acknowledgement and one resulting application before declaring recovery. A changed new attempt must not replace or erase the pending original. If a user already changed the failed form and sees 409, have HR/support review that original pending record rather than repeatedly uploading a new application.

## Verification and deployment

Executed locally: the repository check script passes backend lint/contract-bundle checks, 353 backend tests with 20 external-service tests skipped, frontend lint/type checks, 78 frontend tests and production frontend build. New tests simulate D365 500 → changed retry rejected → unchanged retry accepted with one stored resume and one application; frontend retries retain the exact original body/key, and pre-reservation validation failures still allow corrections.

Not performed: live F&O repair or acceptance, Azure deployment or public production validation. The connected cloud session has no Azure outbound identity, credentials or access to the company deployment workflow. Changes are pushed only to the authorized connected development repository. Transfer to the company repository from an approved local checkout and run its existing successful pipeline; keep its production workflow, image repository and sitecontainer settings intact. Do not replace the company's working workflow with the older connected-repository template.

After deployment, check the original /healthz revision, jobs, candidate/HR sessions and finance health/readiness. Verify the pending original application recovers only after the actual F&O exception is fixed. Roll back the image to its recorded previous digest if portal regressions appear; no database/schema/settings migration is required by this patch.
