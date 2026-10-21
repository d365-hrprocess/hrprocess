"""Bounded, value-free diagnostics for untrusted F&O error responses.

Provider text is never returned verbatim. Only messages emitted by our shipped
UBHR service are reconstructed, using known tables/fields and numeric lengths.
"""

from __future__ import annotations

import json
from hashlib import sha256
import re
import uuid
from typing import Any

import httpx

from app.diagnostic_templates import safe_message_template

MAX_ERROR_BYTES = 65536
MAX_MESSAGE_CHARS = 4096

_COMPANY_MESSAGE = "Company must match the integration user's configured current company."
_FIXED_MESSAGES = {
    _COMPANY_MESSAGE: ("company_mismatch", "Match D365_COMPANY to the F&O integration user's current legal entity."),
    "The _request parameter is required.": ("request_wrapper_missing", "Check D365_PARAMETER_NAME and the deployed contract; the default wrapper is _request."),
    "ApplicationId is already assigned to a different requirement or identity.": ("application_identity_conflict", "Check the frozen application identifier and requirement; retry only the original submission."),
    "The requirement is not accepting applications.": ("requirement_closed", "Check RequirementStatus, UTC OpenDate/CloseDate and legal entity in F&O."),
    "The candidate identity key does not match its stored identity.": ("candidate_identity_conflict", "Check existing candidate identity mapping and unique IdentityKey index."),
    "The candidate record could not be validated. Check the existing table's business defaults.": ("candidate_validation_failed", "Inspect UBCandidateTable.validateWrite(), mandatory fields and business defaults in F&O."),
    "The application record could not be validated. Check the existing table's business defaults.": ("application_validation_failed", "Inspect UBJobApplicationTable.validateWrite(), mandatory fields and business defaults in F&O."),
    "The evaluation record could not be validated. Check the existing table's business defaults.": ("evaluation_validation_failed", "Inspect UBHRMatchEvaluationTable.validateWrite(), mandatory fields and business defaults in F&O."),
    "The completed application record could not be validated.": ("completed_application_validation_failed", "Inspect UBJobApplicationTable.validateWrite() and AIStatus/OverallScore field mappings in F&O."),
    "Concurrent submission could not be resolved. Retry with the same ApplicationId.": ("submission_concurrency", "Retry the original submission with the same frozen ApplicationId."),
    "Concurrent submission update could not be resolved. Retry with the same ApplicationId.": ("submission_concurrency", "Retry the original submission with the same frozen ApplicationId."),
    "Concurrent evaluation could not be resolved. Retry with the same EvaluationId.": ("evaluation_concurrency", "Retry the saved evaluation with the same stable EvaluationId."),
    "Concurrent evaluation update could not be resolved. Retry with the same EvaluationId.": ("evaluation_concurrency", "Retry the saved evaluation with the same stable EvaluationId."),
    "Score must be between 0 and 100.": ("evaluation_score_invalid", "Check the validated evaluation Score range and deployed contract type."),
    "ApplicationId and RequirementId must identify the same existing application.": ("evaluation_application_missing", "Check that application creation was acknowledged in the same legal entity before evaluation."),
    "EvaluationId is already assigned to a different application or requirement.": ("evaluation_identity_conflict", "Check the frozen ApplicationId/RequirementId and stable EvaluationId mapping."),
    "Use the same stable EvaluationId when updating an application's current evaluation.": ("evaluation_identity_conflict", "Retry with the existing stable EvaluationId for this application."),
    "The existing evaluation has an inconsistent requirement reference.": ("evaluation_requirement_conflict", "Check the existing evaluation's application/requirement relationships in F&O."),
    "MeetsMinimumCriteria must be a NoYes enum or integer field.": ("boolean_mapping_invalid", "Use an existing NoYes enum or integer field for MeetsMinimumCriteria."),
    "MeetsMinimumCriteria must use the existing No/Yes enum symbols.": ("boolean_mapping_invalid", "Check the existing enum has the No and Yes symbols."),
    "MeetsMinimumCriteria integer value must be 0 or 1.": ("boolean_mapping_invalid", "Check existing MeetsMinimumCriteria integer data is 0 or 1."),
    "Email must be a contact email address.": ("contact_email_invalid", "Check the submitted contact email format; provider identity is not a contact email."),
    "Email must contain only a contact email address.": ("contact_email_invalid", "Submit a plain contact email without a display name."),
    "LinkedInProfileUrl must be a valid HTTPS LinkedIn profile URL.": ("linkedin_url_invalid", "Check the optional profile URL uses HTTPS and a linkedin.com hostname."),
}

# Values here are AOT metadata from the user's tables, never request values.
_TABLE_FIELDS = {
    'UBJobRequirementTable': {'CloseDate', 'ClosingDateTime', 'Department', 'DepartmentId', 'EmploymentType', 'EvaluationCriteriaJson', 'ExternalRequirementId', 'HiringManagerWorkerRecId', 'JobDescription', 'JobLocation', 'JobStatus', 'JobTitle', 'MaximumExperienceYears', 'MinimumExperienceYears', 'NumberOfOpenings', 'OpenDate', 'PositionId', 'PreferredSkills', 'PublishedDateTime', 'RecruiterId', 'Remarks', 'RequiredCertifications', 'RequiredQualifications', 'RequiredSkills', 'RequirementId', 'RequirementStatus', 'Responsibilities', 'RubricVersion', 'WorkMode', 'WorkflowStatus'},
    'UBCandidateTable': {'CandidateId', 'CandidateSource', 'CandidateStatus', 'ConsentDateTime', 'ConsentVersion', 'CurrentCompany', 'Designation', 'Email', 'ExperienceYears', 'FirstName', 'IdentityKey', 'LastName', 'LinkedInIssuer', 'LinkedInProfileJson', 'LinkedInProfileUrl', 'LinkedInSub', 'LocationName', 'NaukriProfileId', 'NoticePeriodDays', 'Phone', 'Qualification', 'RelevantExperienceYears', 'Remarks', 'SourceId'},
    'UBJobApplicationTable': {'AIStatus', 'ApplicationDate', 'ApplicationId', 'ApplicationStatus', 'CandidateId', 'ConsentVersion', 'JDHash', 'OverallScore', 'RecruiterId', 'Remarks', 'RequirementId', 'ResumeBlobKey', 'ResumeFileName', 'ResumeSha256'},
    'UBHRMatchEvaluationTable': {'ApplicationId', 'EvaluatedDateTime', 'EvaluationId', 'EvidenceJson', 'MatchLabel', 'MatchedSkillsJson', 'MeetsMinimumCriteria', 'MissingSkillsJson', 'ModelDeployment', 'PromptVersion', 'RequirementId', 'RubricVersion', 'Score', 'Summary'},
}

_EXCEPTION_TYPES = {
    "ErrorException", "Exception", "XppException", "ClrErrorException", "UpdateConflictException",
    "DuplicateKeyException", "DuplicateKeyExceptionNotRecovered", "UpdateConflictNotRecovered",
    "System.Exception", "System.InvalidOperationException", "System.ArgumentException",
    "System.NullReferenceException", "System.InvalidCastException", "System.FormatException",
    "System.ArgumentNullException", "System.Reflection.TargetInvocationException", "System.Data.SqlClient.SqlException",
    "Microsoft.Data.SqlClient.SqlException", "Microsoft.Dynamics.Ax.Xpp.ErrorException",
    "Microsoft.Dynamics.Ax.Xpp.XppException", "Microsoft.Dynamics.Ax.Xpp.UpdateConflictException",
    "Microsoft.Dynamics.Ax.Xpp.DuplicateKeyException", "System.UnauthorizedAccessException",
    "Microsoft.Dynamics.Ax.Xpp.InfoException", "Newtonsoft.Json.JsonSerializationException",
}


_SHIPPED_METHODS = ('getOpenRequirements', 'submitApplication', 'saveCandidateEvaluation', 'requirementStatusFieldId', 'openRequirementStatus', 'pendingAIStatus', 'completedAIStatus', 'acceptsApplications', 'requirementContract', 'applicationResult', 'sameEvaluation', 'requireCompany', 'requireExternalId', 'requireFieldText', 'writeText', 'readSymbol', 'writeSymbol', 'writeBoolean', 'readBoolean', 'requireEmail', 'requireLinkedInUrl', 'requireJsonArray', 'dateText', 'sameText', 'externalId', 'segment', 'identityHash')

def _uuid(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _known_message(message: str) -> dict[str, Any] | None:
    # The F&O runtime wraps custom-service errors in an operation failure prefix.
    # Reconstruct the recognized fragment, discarding ALL surrounding text.
    for canonical, (code, hint) in _FIXED_MESSAGES.items():
        if canonical in message:
            return {"issue_code": code, "hint": hint, "provider_message": canonical}
    for table, fields in _TABLE_FIELDS.items():
        for field in fields:
            prefix = f"{table}.{field}"
            length = re.search(re.escape(prefix) + r" is missing or exceeds its existing field length \(([0-9]{1,7})\)\.", message)
            if length:
                maximum = int(length.group(1))
                return {"issue_code": "field_length_or_missing", "table": table, "field": field,
                        "max_length": maximum, "provider_message": f"{prefix} is missing or exceeds its existing field length ({maximum}).",
                        "hint": "Inspect this existing field's EDT/StringSize and required value; IDs need 40 characters and IdentityKey needs 64."}
            for suffix, code, hint in (
                ("must be a string field for this service mapping.", "field_type_invalid", "Check the existing field is a string EDT for the deployed UBHR mapping."),
                ("contains an unknown enum value.", "enum_value_invalid", "Check existing enum data and the deployed status configuration symbols."),
                ("must be an enum or string field for the status mapping.", "status_field_type_invalid", "Check the existing status field type is Enum or String."),
            ):
                canonical = f"{prefix} {suffix}"
                if canonical in message:
                    return {"issue_code": code, "table": table, "field": field,
                            "provider_message": canonical, "hint": hint}
    for field, prefix in (("ApplicationId", "APP-"), ("EvaluationId", "EVA-")):
        canonical = f"{field} must contain the stable {prefix} identifier supplied by the backend."
        if canonical in message:
            return {"issue_code": "external_id_invalid", "field": field, "provider_message": canonical,
                    "hint": "Use the stable backend-generated identifier; check DTO field length and request mapping."}
    for field in ("MatchedSkillsJson", "MissingSkillsJson", "EvidenceJson"):
        for suffix, code in (("exceeds the supported JSON payload size.", "evaluation_json_too_large"),
                             ("must contain a valid JSON array; use [] for an empty collection.", "evaluation_json_invalid")):
            canonical = f"{field} {suffix}"
            if canonical in message:
                return {"issue_code": code, "field": field, "provider_message": canonical,
                        "hint": "Check evaluation JSON array serialization and the existing memo field mapping."}
    enum_error = re.search(r"Enum [A-Za-z_][A-Za-z0-9_]{0,63} has no symbol '(Open|Pending|Completed|No|Yes)'\. Set the service configuration method to your actual existing enum symbol\.", message)
    if enum_error:
        # Enum names are user-defined; suppress them rather than printing a
        # provider-controlled identifier that could be a credential or subject.
        symbol = enum_error.group(1)
        return {"issue_code": "enum_symbol_missing", "enum_symbol": symbol,
                "provider_message": f"The existing enum has no symbol '{symbol}'.",
                "hint": "Match UBHRPortalService status configuration methods to actual AOT enum symbols; AIStatus defaults to Pending/Completed."}
    return None


_STANDARD_FAILURES = (
    (("record already exists", "duplicate key", "unique constraint", "duplicate record"), "duplicate_record", "Inspect existing unique indexes and the frozen application identity; do not create a replacement ID."),
    (("must be filled in", "mandatory field", "required field", "field is required"), "mandatory_field_missing", "Inspect mandatory fields, initValue defaults and validateWrite in the existing F&O table."),
    (("string or binary data would be truncated", "string length", "value is too long", "exceeds the length", "maximum length"), "field_size_invalid", "Compare the logged contract field lengths with the existing AOT EDT/StringSize; do not truncate stable IDs."),
    (("invalid cast", "cannot convert", "unable to cast", "wrong argument type", "type mismatch"), "contract_type_mismatch", "Check deployed DTO parameter types, field mappings and enum/string assignment in the service."),
    (("object reference not set", "null reference", "nullreferenceexception"), "server_null_reference", "Inspect the reported service stack frame and missing table/contract initialization in F&O."),
    (("not authorized", "access denied", "permission denied", "unauthorized", "insufficient permissions"), "server_access_denied", "Verify the integration user's F&O service privileges and legal-entity access."),
    (("deadlock", "update conflict", "record has been updated", "concurrency conflict"), "server_concurrency", "Inspect concurrent operations and existing unique indexes; keep the same frozen identifiers."),
    (("sql timeout", "execution timeout", "operation timed out", "request timed out"), "server_timeout", "Inspect F&O database/service latency; an unconfirmed write must retain its original identity."),
    (("not a valid enum", "invalid enum", "enum value", "enum symbol"), "server_enum_invalid", "Verify the actual existing enum symbols and deployed status mappings; do not guess numeric values."),
    (("deserializ", "serializ", "invalid json", "jsonreaderexception"), "contract_serialization_failed", "Compare the request wrapper, JSON property types and deployed data contracts."),
)


def d365_error_diagnostics(response: httpx.Response, *, sensitive_values=()) -> dict[str, Any]:
    """Bounded scalar diagnostics, including unfamiliar inner errors, with no raw values."""
    diagnostics: dict[str, Any] = {"issue_code": "d365_unclassified",
        "hint": "Inspect the value-free message template and activity_id; the provider may omit its internal cause."}
    for header in ("x-ms-diagnostics-activity-id", "x-ms-activity-id", "activityid", "x-correlation-id", "request-id"):
        value = _uuid(response.headers.get(header))
        if value:
            diagnostics["activity_id"] = value
            break
    if len(response.content) > MAX_ERROR_BYTES:
        return {**diagnostics, "response_kind": "oversized", "diagnostic_truncated": True}
    try:
        body = json.loads(response.content)
        if isinstance(body, str) and len(body) <= MAX_MESSAGE_CHARS:
            body = json.loads(body)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return {**diagnostics, "response_kind": "non_json"}
    diagnostics["response_kind"] = "json"
    stack, messages, types, frames = [(body, 0, False)], [], [], []
    visited = 0
    while stack and visited < 128:
        node, depth, message_container = stack.pop()
        visited += 1
        if depth > 8:
            diagnostics["diagnostic_truncated"] = True
            continue
        if isinstance(node, str) and message_container:
            messages.append(node[:MAX_MESSAGE_CHARS])
            if len(node) > MAX_MESSAGE_CHARS:
                diagnostics["diagnostic_truncated"] = True
            continue
        if isinstance(node, dict):
            for key, value in list(node.items())[:32]:
                if not isinstance(key, str):
                    continue
                key = key.lower()
                if key in {"activityid", "activity_id", "correlationid", "correlation_id"}:
                    activity = _uuid(value)
                    if activity:
                        diagnostics.setdefault("activity_id", activity)
                elif key in {"exceptiontype", "exception_type", "type"} and isinstance(value, str):
                    if value in _EXCEPTION_TYPES:
                        types.append(value)
                        diagnostics.setdefault("exception_type", value)
                elif key in {"message", "errormessage", "exceptionmessage", "description", "text"} or (message_container and key == "value"):
                    if isinstance(value, str):
                        messages.append(value[:MAX_MESSAGE_CHARS])
                        if len(value) > MAX_MESSAGE_CHARS:
                            diagnostics["diagnostic_truncated"] = True
                    elif isinstance(value, (dict, list)):
                        stack.append((value, depth + 1, True))
                elif key in {"stacktrace", "stack_trace"} and isinstance(value, str):
                    # Keep only our known service entry points, never arguments,
                    # filesystem paths, source excerpts or arbitrary class names.
                    for method in _SHIPPED_METHODS:
                        if re.search(r"\bUBHRPortalService[.`:]" + re.escape(method) + r"\b", value):
                            frames.append("UBHRPortalService." + method)
                elif key in {"error", "innererror", "innerexception", "details", "infolog", "errors"} and isinstance(value, (dict, list)):
                    stack.append((value, depth + 1, key in {"infolog", "errors"}))
        elif isinstance(node, list):
            stack.extend((item, depth + 1, message_container) for item in node[:16] if isinstance(item, (dict, list, str)))
    diagnostics["message_count"] = len(messages)
    diagnostics["inner_exception_count"] = max(0, len(types) - 1)
    diagnostics["diagnostic_truncated"] = bool(diagnostics.get("diagnostic_truncated") or stack)
    for message in messages:
        known = _known_message(message)
        if known and diagnostics["issue_code"] == "d365_unclassified":
            diagnostics.update(known)
    if diagnostics["issue_code"] == "d365_unclassified":
        for phrases, code, hint in _STANDARD_FAILURES:
            if any(phrase in message.lower() for phrase in phrases for message in messages):
                diagnostics.update(issue_code=code, hint=hint)
                break
    if messages:
        # Identify only metadata names supplied in the repository's AOT inventory.
        # Unknown provider identifiers/values are never accepted as field names.
        text = " ".join(messages)
        tables = [name for name in _TABLE_FIELDS if re.search(r"\b" + re.escape(name) + r"\b", text)]
        if len(tables) == 1:
            diagnostics.setdefault("table", tables[0])
            allowed_fields = _TABLE_FIELDS[tables[0]]
        else:
            allowed_fields = set().union(*_TABLE_FIELDS.values())
        fields = [name for name in allowed_fields if re.search(r"\b" + re.escape(name) + r"\b", text)]
        if len(fields) == 1:
            diagnostics.setdefault("field", fields[0])
    if messages:
        templates = list(dict.fromkeys(safe_message_template(m, sensitive_values=sensitive_values) for m in messages))
        # Prefer the deepest returned message for a single-line troubleshooting clue.
        diagnostics["message_template"] = templates[-1]
        diagnostics["error_fingerprint"] = sha256("|".join(templates).encode()).hexdigest()[:24]
    if types:
        diagnostics["root_exception_type"] = types[-1]
    if frames:
        diagnostics["provider_stack"] = ":".join(dict.fromkeys(frames))
    diagnostics["cause_available"] = diagnostics["issue_code"] != "d365_unclassified"
    return diagnostics
