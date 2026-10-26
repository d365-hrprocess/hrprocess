# Three lean JSON service examples

These files contain **fictional data** for the existing `UBHR_Nive` tables: `UBJobRequirementTable`, `UBCandidateTable`, `UBJobApplicationTable` and `UBHRMatchEvaluationTable`. They match [contracts-manifest.json](../contracts-manifest.json) and require no new table columns. All three operations use HTTP **POST**, bearer authentication and `Content-Type: application/json`.

```text
https://YOUR-ENVIRONMENT.operations.dynamics.com/api/services/UBHRPortalServiceGroup/UBHRPortalService/OPERATION
```

| Operation | Request example | Response example |
| --- | --- | --- |
| `getOpenRequirements` | [request](getOpenRequirements.request.json) | [response](getOpenRequirements.response.json) |
| `submitApplication` | [request](submitApplication.request.json) | [response](submitApplication.response.json) |
| `saveCandidateEvaluation` | [request](saveCandidateEvaluation.request.json) | [response](saveCandidateEvaluation.response.json) |

`getOpenRequirements` accepts `{}` and reads the integration user's current company. Set that user's startup company to `D365_COMPANY`. Both writes accept `{ "_request": { ... } }` and require `Company` to match the same company. `Company` is service context, not a new table field. `Success` and `Message` acknowledge a write.

Application requests send one confirmed full `CandidateName` and the existing `LinkedInProfileUrl`. Deploy the expanded F&O contract first; it retains optional `FirstName`/`LastName` for the previous client during rollout. The backend freezes an `APP-<GUID>` identifier in its configured metadata store **before** it calls F&O. F&O must return that exact `ApplicationId`; retry the identical request with the same identifier. The service creates or reuses the candidate for the same LinkedIn issuer and subject. For a genuinely new application, generate a new application GUID; do not change the old request and reuse its application ID.

Evaluation requests use the confirmed table `UBHRMatchEvaluationTable`. `EvaluationId` is `EVA-` followed by the application's same GUID. `Score` is an integer from 0 to 100; `MeetsMinimumCriteria` and `Success` are JSON booleans. `MatchedSkillsJson`, `MissingSkillsJson` and `EvidenceJson` are strings containing JSON arrays, so their internal quotes are escaped in the outer request. Repeat an evaluation save with the same identifiers to update the one current evaluation for that application.

`OpenDate` and `CloseDate` are job eligibility dates, serialized as `YYYY-MM-DD`; they are separate from portal audit metadata. Required/preferred skills are comma-delimited strings. `EvaluationCriteriaJson` and `RubricVersion` describe the existing job's business scoring criteria. The sample close date is illustrative: configure actual opening and closing dates when preparing a live test requirement.

The D365 requests contain only the core requirement, candidate, application and evaluation data. The backend keeps resume references, hashes, creation/submission timestamps, consent, public LinkedIn profile JSON, idempotency state and full AI/model audit details in SQLite. A direct Postman write does **not** create that SQLite record, upload a resume, populate portal history or run an AI review. Use the React portal with a real resume for a complete workflow test.

## Postman: test the three F&O operations

1. Import [UBHRPortal.postman_collection.json](../UBHRPortal.postman_collection.json). Set `foBaseUrl` to your actual F&O HTTPS resource URL without a trailing slash, `company` to the integration user's company, and `requirementId` to an existing open requirement. Acquire an Entra client-credentials token for `<F&O resource URL>/.default` and keep it in a local, sensitive `accessToken` value. The collection includes no access token or client secret.
2. Run `getOpenRequirements`, then `submitApplication`. Replace the fictional candidate contact/identity values for your development test. Keep the frozen `applicationId` unchanged on a retry. The test checks that F&O preserves it and stores the returned `CandidateId` for the next acknowledgement check. In the F&O development table browser, verify the candidate and application rows and their relationship. A repeat should preserve the application and candidate identifiers and create no duplicate application.
3. Run `saveCandidateEvaluation` after submission. Keep `evaluationId` equal to `EVA-` plus the application's GUID; the collection checks this before sending. Its body is a fictional score/evidence persistence test, not an Azure OpenAI response. Check that the acknowledgement preserves both IDs and that `UBHRMatchEvaluationTable` contains one current row for the application. Repeating the save should update that row; a new application uses a new GUID and matching evaluation ID.

For connected portal validation, submit through React so the backend creates the SQLite technical record, stores the private resume, submits the F&O application, and queues the actual resume/JD comparison. Confirm the worker saves the full result in SQLite and the core result in `UBHRMatchEvaluationTable`. Do not enter D365 tokens or credentials into React; they belong to the backend integration.

The response fixtures show possible successful shapes. Actual generated candidate IDs will differ. These files do not prove that an F&O service is compiled or deployed. Keep SSL verification enabled and investigate live rejections through the F&O deployment, integration-user permissions, company and contract configuration.
