# Existing F&O tables and selected fields

Use your existing model **`UBHR_Nive`** and the four portal integration tables. The name fix uses their existing `CandidateName` columns and needs no new field. The [AXPP review](review/2026-10-25/README.md) inventories all 14 project tables and documents future proposals separately. Existing technical fields remain unchanged and are not assigned by the lean service.

## Fields used by the three-operation service

| Existing table | Selected fields |
| --- | --- |
| `UBJobRequirementTable` | `RequirementId`, `JobTitle`, `JobDescription`, `Responsibilities`, `RequiredSkills`, `PreferredSkills`, `RequiredQualifications`, `RequiredCertifications`, `Department`, `JobLocation`, `WorkMode`, `EmploymentType`, `MinimumExperienceYears`, `MaximumExperienceYears`, `NumberOfOpenings`, `RequirementStatus`, `OpenDate`, `CloseDate`, `EvaluationCriteriaJson`, `RubricVersion` |
| `UBCandidateTable` | `CandidateId`, `CandidateName`, `Email`, `Phone`, `LinkedInProfileUrl`, `LinkedInIssuer`, `LinkedInSub`, `IdentityKey`; optional legacy first/last name input |
| `UBJobApplicationTable` | `ApplicationId`, `CandidateId`, `RequirementId`, `CandidateName`, `ApplicationStatus`, `AIStatus`, `OverallScore` |
| `UBHRMatchEvaluationTable` | `EvaluationId`, `ApplicationId`, `RequirementId`, `Score`, `MeetsMinimumCriteria`, `MatchLabel`, `Summary`, `MatchedSkillsJson`, `MissingSkillsJson`, `EvidenceJson` |

`ApplicationStatus` retains the value supplied by the existing table's `initValue()` implementation. The service does not invent an enum element for human review. `AIStatus` starts at the configured `Pending` symbol and becomes `Completed` only when the evaluation transaction commits. `OverallScore` receives the same validated score as the evaluation. AI does not alter human review status.

## Integration-table inventory

The original screenshot inventory below is supplemented by the current AXPP's new display/number fields. The complete 203-column project inventory, including declared types and sizes, is [existing-columns.csv](review/2026-10-25/existing-columns.csv).

### `UBJobRequirementTable`

`PositionId`, `DepartmentId`, `JobTitle`, `JobLocation`, `EmploymentType`, `WorkMode`, `RequirementStatus`, `RecruiterId`, `OpenDate`, `CloseDate`, `Remarks`, `MinimumExperienceYears`, `RequirementId`, `ExternalRequirementId`, `RequiredQualifications`, `NumberOfOpenings`, `WorkflowStatus`, `JobDescription`, `Responsibilities`, `RequiredSkills`, `PreferredSkills`, `RequiredCertifications`, `PublishedDateTime`, `HiringManagerWorkerRecId`, `EvaluationCriteriaJson`, `RubricVersion`, `MaximumExperienceYears`, `Department`, `JobStatus`, `ClosingDateTime`.

### `UBCandidateTable`

`CandidateId`, `FirstName`, `LastName`, `LocationName`, `Qualification`, `CurrentCompany`, `Designation`, `ExperienceYears`, `CandidateStatus`, `Remarks`, `Email`, `Phone`, `NoticePeriodDays`, `RelevantExperienceYears`, `LinkedInProfileUrl`, `NaukriProfileId`, `SourceId`, `CandidateSource`, `LinkedInIssuer`, `LinkedInSub`, `IdentityKey`, `LinkedInProfileJson`, `ConsentVersion`, `ConsentDateTime`, `CandidateNumber`, `CandidateName`.

### `UBJobApplicationTable`

`ApplicationId`, `CandidateId`, `RequirementId`, `ApplicationDate`, `ApplicationStatus`, `RecruiterId`, `Remarks`, `OverallScore`, `ResumeFileName`, `ResumeBlobKey`, `ResumeSha256`, `ConsentVersion`, `JDHash`, `AIStatus`, `ApplicationNumber`, `CandidateName`, `Summary`, `UBScreeningRecommendation`, `CandidateNumber`.

### `UBHRMatchEvaluationTable`

`EvaluationId`, `ApplicationId`, `RequirementId`, `Score`, `MeetsMinimumCriteria`, `MatchLabel`, `Summary`, `MatchedSkillsJson`, `MissingSkillsJson`, `EvidenceJson`, `ModelDeployment`, `PromptVersion`, `RubricVersion`, `EvaluatedDateTime`.

## Metadata to keep in SQLite

The backend stores application/creation/evaluation timestamps, filename and private Blob reference, resume/JD hashes, consent version/date, the allowlisted LinkedIn profile snapshot, frozen submission payload and retry key, complete selected-JD snapshot, and the full AI result in SQLite. The requirement's existing `EvaluationCriteriaJson` and `RubricVersion` remain part of the job read because they define its evaluation criteria. The full result includes score justification, partially matched evidence, confidence and actual model/prompt/rubric versions. Actual PDF/DOCX files remain in private Azure Blob Storage; SQLite stores their references.

These existing F&O fields are deliberately unused by the lean source: candidate `LinkedInProfileJson`, `ConsentVersion`, `ConsentDateTime`; application `ApplicationDate`, `ResumeFileName`, `ResumeBlobKey`, `ResumeSha256`, `ConsentVersion`, `JDHash`; evaluation `ModelDeployment`, `PromptVersion`, `RubricVersion`, `EvaluatedDateTime`; requirement `PublishedDateTime`, `ClosingDateTime`. Do not delete them or add replacements. F&O may still maintain its own system fields or table defaults.

## Important checks before build

The original screenshots omitted many properties; the AXPP now supplies the declared EDT/enum/index metadata listed in the review. Target database contents, assignments and live constraints still require inspection. Make these checks on the existing definitions:

- `CandidateId`, `ApplicationId`, `EvaluationId`: at least **40 characters**, with compatible reference fields. The portal sends `APP-` plus a 36-character GUID; evaluation IDs are stable `EVA-` IDs derived from that application. Existing requirement IDs keep their actual values.
- `IdentityKey`: **64 characters** for the SHA-256 digest of the validated issuer/subject pair. Never identify candidates solely by email or name.
- JD, summary and JSON fields: enough space for complete values. Bounded text fields can truncate evidence; inspect their existing EDTs rather than guessing types from the screenshot.
- `RequirementId` must select one requirement in the configured company. Verify the existing `CandidateId` index (`UBCandidateIdx`), `IdentityKey` index (`UBIdentityKeyIdx`), application-ID index and `EvaluationIdIdx` enforce uniqueness (`AllowDuplicates=No`). The application screenshot does not show indexes, so their existence/properties need inspection. Clean pre-existing duplicate/blank identifiers before applying a unique index.
- Check existing `initValue()` / `validateWrite()` rules and provide valid existing defaults for any required screenshot business fields outside the selected payload (for example `CandidateSource` or `RecruiterId`). The service invokes table validation; it does not bypass these rules.
- Verify existing company scoping, relationships and table validation. Required links are `UBJobApplicationTable.CandidateId` → `UBCandidateTable.CandidateId`, application `RequirementId` → requirement `RequirementId`, and evaluation `ApplicationId` / `RequirementId` → the corresponding application/job. The service checks those links even when metadata relations are already present.
- Inspect `Mandatory` on the existing SQLite-only metadata fields listed above. They must permit absence in F&O for this lean write contract; set `Mandatory=No` on those existing fields where needed, then build/synchronize that metadata change. Existing mandatory business fields need valid table defaults. This changes existing properties rather than adding columns.
- For old candidate records with a blank identity digest, use a reviewed migration/backfill before enabling the portal identity index; do not match a LinkedIn account to an existing candidate only by contact email.

**No essential new column is missing** for jobs → application submission → AI-result persistence. `ScoreJustification`, `PartiallyMatchedJson` and `EvaluationConfidence` are absent from the evaluation screenshot, but the complete result is stored in SQLite, so they do not need new F&O columns.

## Existing status and date mapping

The service reads actual metadata through `DictField`/`DictEnum`; it handles an existing string or enum without assuming numeric enum values. In `UBHRPortalService`, inspect these private methods and change their return values only if your existing symbols differ:

| Method | Default |
| --- | --- |
| `requirementStatusFieldId()` | `fieldNum(UBJobRequirementTable, RequirementStatus)` |
| `openRequirementStatus()` | `Open` |
| `pendingAIStatus()` | `Pending` |
| `completedAIStatus()` | `Completed` |

Use the enum element **name**, not a translated label or guessed ordinal. Missing symbols fail explicitly. Eligible job responses use canonical `RequirementStatus="Open"` regardless of the configured local active symbol. `WorkMode` and `EmploymentType` are read as their existing string/enum symbols. `MeetsMinimumCriteria` supports the existing `NoYes` enum or integer 0/1. The date fields used are `OpenDate` and `CloseDate`, inclusive UTC calendar dates; zero dates mean no bound. The separate `JobStatus` and `ClosingDateTime` fields are not used for eligibility in this simplified flow. Numeric job values must represent the actual advertised experience/openings; zero means zero.

The machine-readable [table-specification.json](table-specification.json) repeats the screenshot inventory and required checks. It is documentation, not importable AOT metadata.
