"""Validate the three screenshot-table contracts and SQLite metadata boundary.

From backend: uv run --frozen python ../d365/validate_bundle.py
This runs mocked HTTP calls, not an X++ compiler or a live F&O deployment test.
"""

import asyncio
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import httpx  # noqa: E402

from app.config import Settings  # noqa: E402
from app.metadata import SQLiteMetadata, encode  # noqa: E402
from app.models import Evaluation, Job, User  # noqa: E402
from app.services.d365 import PATHS, normalize  # noqa: E402
from app.services.sqlite_d365 import LEAN_REQUEST_FIELDS, SQLiteD365Service  # noqa: E402

BUNDLE = ROOT / "d365"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def check(condition, message):
    if not condition:
        raise ValueError(message)


def validate_dto(value, name, contracts):
    check(isinstance(value, dict), f"{name}: expected an object")
    members = contracts[name]["members"]
    optional = set(contracts[name].get("optional_members", []))
    check(set(members) - optional <= set(value) <= set(members), f"{name}: example fields differ from the DTO")
    for field, kind in members.items():
        if field not in value:
            continue
        item = value[field]
        if kind.startswith("List<"):
            check(isinstance(item, list), f"{name}.{field}: expected an array")
            for row in item:
                validate_dto(row, kind[5:-1], contracts)
        else:
            valid = {
                "str": isinstance(item, str), "int": type(item) is int,
                "real": type(item) in (int, float), "boolean": type(item) is bool,
            }[kind]
            check(valid, f"{name}.{field}: wrong JSON type for {kind}")


def validate_xpp_syntax_regressions(source):
    """Catch the specific grammar errors reported by the user's F&O compiler.

    These targeted guards do not replace the X++ compiler. Comments and string
    literals are removed so examples in explanatory text do not become errors.
    """
    code = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
                  " ", source)
    check(not re.search(r"\b(?:int|int64|str|real|boolean|date|utcDateTime)\s+index\b", code, re.IGNORECASE),
          "X++ reserves 'index'; use a different local variable name")
    lock_hints = {"forupdate", "optimisticlock", "pessimisticlock", "repeatableread"}
    for statement in re.finditer(r"\bselect\b([^;]*);", code, re.IGNORECASE):
        header = re.split(r"\bwhere\b", statement.group(1), maxsplit=1, flags=re.IGNORECASE)[0]
        hints = set(re.findall(r"\b\w+\b", header.lower())) & lock_hints
        check(len(hints) <= 1, f"X++ select locking hints are mutually exclusive: {sorted(hints)}")


def validate_sources(manifest, schema):
    contracts = manifest["contracts"]
    for name, contract in contracts.items():
        source = (ROOT / contract["source"]).read_text(encoding="utf-8")
        check(f"class {name}" in source, f"Wrong class declaration: {name}")
        found = re.findall(r'DataMemberAttribute\("([^"]+)"\)', source)
        check(len(found) == len(set(found)), f"Duplicate DTO member in {name}")
        check(set(found) == set(contract["members"]), f"Manifest differs from {name}")
        accessors = re.findall(
            r'DataMemberAttribute\("([^"]+)"\).*?public\s+(\w+)\s+(parm\w+)\(',
            source, flags=re.DOTALL,
        )
        for member, return_type, _accessor in accessors:
            check(return_type == contract["members"][member].split("<", 1)[0],
                  f"Wrong X++ return type for {name}.{member}")
        for target in contract["members"].values():
            if target.startswith("List<"):
                check(f"classStr({target[5:-1]})" in source, f"Untyped list in {name}")

    request_members = set()
    for info in manifest["operations"].values():
        if info["request"]:
            contract = contracts[info["request"]]
            request_members.update(set(contract["members"]) - set(contract.get("optional_members", [])))
    check(set(LEAN_REQUEST_FIELDS.values()) == request_members,
          "Lean backend aliases differ from the two request DTOs")
    check(schema["existing_tables"] is True, "The bundle must use existing tables")
    check(schema["extended_data_types"] == {} and schema["enums"] == {},
          "No new EDTs or enums are authorized")

    service = (BUNDLE / "xpp/UBHRPortalService.xpp").read_text(encoding="utf-8")
    validate_xpp_syntax_regressions(service)
    check(service.count("[SysEntryPointAttribute(true)]") == 3, "Expected three entry points")
    for operation, info in manifest["operations"].items():
        parameter = "" if info["request"] is None else f'{info["request"]} _request'
        check(f'public {info["response"]} {operation}({parameter})' in service,
              f"Operation signature mismatch: {operation}")

    tables = schema["tables"]
    table_types = "|".join(tables)
    declarations = re.findall(rf"\b({table_types})\s+(\w+)", service)
    for table, variable in set(declarations):
        fields = {field["name"] for field in tables[table]["fields"]} | {"RecId", "DataAreaId", "RecVersion"}
        used = set(re.findall(rf"\b{variable}\.([A-Za-z]\w*)\b(?!\s*\()", service))
        check(used <= fields, f"Unknown {table} fields: {used - fields}")
    for table, field in re.findall(r"fieldNum\(\s*(\w+)\s*,\s*(\w+)\s*\)", service):
        check(table in tables, f"Unprovided screenshot table: {table}")
        spec = next((item for item in tables[table]["fields"] if item["name"] == field), None)
        check(spec is not None, f"Unprovided screenshot column: {table}.{field}")
        check(spec["used_by_lean_service"], f"SQLite-only/unused field referenced: {table}.{field}")
    all_fields = {field["name"] for table in tables.values() for field in table["fields"]}
    for contract in contracts.values():
        check(set(contract["members"]) <= all_fields | {"Company", "Success", "Message", "Requirements"},
              "DTO contains an unprovided business column")


async def validate_adapter(manifest, database):
    requests, responses = {}, {}
    for name, info in manifest["operations"].items():
        requests[name] = read_json(BUNDLE / f"examples/{name}.request.json")
        responses[name] = read_json(BUNDLE / f"examples/{name}.response.json")
        validate_dto(responses[name], info["response"], manifest["contracts"])
        if info["request"] is None:
            check(requests[name] == {}, f"{name} must be parameterless")
        else:
            check(set(requests[name]) == {"_request"}, f"Wrong wrapper for {name}")
            validate_dto(requests[name]["_request"], info["request"], manifest["contracts"])

    submit = requests["submitApplication"]["_request"]
    settings = Settings(_env_file=None, demo_mode=False, d365_contract_mode="sqlite",
                        d365_company=submit["Company"], d365_base_url="https://fo.example.invalid",
                        sqlite_database_path=database)
    for operation, info in manifest["operations"].items():
        setattr(settings, PATHS[info["backend_operation"]],
                f'/api/services/{manifest["service_group"]}/{manifest["aot_service"]}/{operation}')

    seen = []

    def respond(request):
        name = request.url.path.rsplit("/", 1)[-1]
        check(request.method == "POST", f"{name}: wrong method")
        check(json.loads(request.content) == requests[name], f"{name}: actual adapter body differs")
        seen.append(name)
        return httpx.Response(200, json=responses[name])

    class FixtureService(SQLiteD365Service):
        async def get_access_token(self, force_refresh=False):
            return "local-mocked-token"

    metadata = SQLiteMetadata(database)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        service = FixtureService(settings, metadata=metadata, client=client)
        await service.get_open_requirements()
        row = normalize(responses["getOpenRequirements"])["requirements"][0]
        row["status"] = row["requirement_status"]
        row["published_date"] = row["open_date"]
        row["deadline"] = row["close_date"]
        for field in ("required_skills", "preferred_skills", "required_certifications"):
            row[field] = [part.strip() for part in re.split(r"[,;\n]", row[field]) if part.strip()]
        # Validate historical example shape separately from current eligibility.
        job = Job.model_validate(row)
        profile = {"sub": submit["LinkedInSub"], "issuer": submit["LinkedInIssuer"],
                   "name": submit["CandidateName"]}
        payload = {
            "application_id": submit["ApplicationId"], "requirement_id": submit["RequirementId"],
            "candidate_name": profile["name"], "email": submit["Email"], "phone": submit["Phone"],
            "linkedin_url": submit["LinkedInProfileUrl"], "owner_sub": submit["LinkedInSub"],
            "owner_issuer": submit["LinkedInIssuer"], "linkedin_profile": profile,
            "resume_ref": "azure:0123456789abcdef0123456789abcdef.pdf", "resume_file_name": "example.pdf",
            "resume_sha256": "a" * 64, "job_description_hash": hashlib.sha256(job.model_dump_json().encode()).hexdigest(),
            "consent_accepted": True, "consent_version": "v1", "consent_date": datetime.now(timezone.utc).isoformat(),
            "idempotency_key": "fictional-local-request",
        }
        record = {
            "application_id": submit["ApplicationId"], "company": submit["Company"], "payload": payload,
            "requirement_id": submit["RequirementId"], "fingerprint": hashlib.sha256(b"fixture").hexdigest(),
            "payload_sha256": hashlib.sha256(encode(payload).encode()).hexdigest(),
            "stage": "ready", "job_snapshot": job.model_dump(mode="json"), "resume_ref": payload["resume_ref"],
            "resume_file_name": payload["resume_file_name"], "created_at": payload["consent_date"],
        }
        key = "fixture-sqlite-submission"
        await metadata.reserve(key, record)
        acknowledgement = await service.submit_application(payload)
        record.update(stage="accepted", candidate_id=acknowledgement["candidate_id"])
        await metadata.update_submission(key, record)

        evaluation_request = requests["saveCandidateEvaluation"]["_request"]
        fields = {key: value for key, value in normalize(evaluation_request).items()
                  if key in Evaluation.model_fields}
        fields.update(score_justification="Fictional full explanation stored only in SQLite.",
                      partially_matched_requirements=[], evaluation_confidence="Medium",
                      model_deployment="fixture-model", prompt_version="v1", rubric_version=job.rubric_version)
        evaluation = Evaluation.model_validate(fields)
        await service.save_candidate_evaluation({"application_id": submit["ApplicationId"],
                                                "requirement_id": submit["RequirementId"],
                                                "evaluation": evaluation.model_dump(mode="json")})

        # The three read capabilities below use acknowledged SQLite data and
        # must not introduce additional F&O operations or lose private metadata.
        user = User(sub=submit["LinkedInSub"], issuer=submit["LinkedInIssuer"],
                    name=profile["name"], email=submit["Email"], role="candidate")
        history = await service.get_my_applications(user)
        check(len(history) == 1 and history[0].owner_sub == user.sub, "History ownership mismatch")
        ranking = await service.get_top_candidates(submit["RequirementId"])
        check(ranking[0].evaluation.score == evaluation.score, "Score was changed")
        # Reopen the same file to prove the complete state is not process memory.
        reopened = FixtureService(settings, metadata=SQLiteMetadata(database), client=client)
        detail = await reopened.get_application(submit["ApplicationId"])
        check(detail.resume_ref == payload["resume_ref"], "Private resume reference lost")
        check(detail.evaluation.score_justification == fields["score_justification"], "Full explanation lost")
        retained = await reopened.metadata.get_application(submit["ApplicationId"], submit["Company"])
        check(retained["payload"]["linkedin_profile"] == profile, "Profile snapshot lost")
    check(set(seen) == set(manifest["operations"]) and len(seen) == 3,
          "Expected exactly three actual F&O adapter calls")


def validate_postman(manifest):
    collection = read_json(BUNDLE / "UBHRPortal.postman_collection.json")
    check(len(collection["item"]) == 3, "Postman must expose exactly three operations")
    variables = {item["key"]: item["value"] for item in collection["variable"]}
    check(not variables.get("accessToken"), "Postman must not include a token")
    for item in collection["item"]:
        request = item["request"]
        check(request["method"] == "POST", "Postman request must use POST")
        raw = request["url"] if isinstance(request["url"], str) else request["url"]["raw"]
        operation = raw.rsplit("/", 1)[-1]
        check(operation in manifest["operations"], "Unknown Postman operation")
        check(f'/{manifest["service_group"]}/{manifest["aot_service"]}/' in raw,
              "Postman endpoint differs from the source")
        body = request["body"]["raw"]
        for key, value in variables.items():
            body = body.replace("{{" + key + "}}", str(value))
        parsed = json.loads(body)
        info = manifest["operations"][operation]
        if info["request"]:
            validate_dto(parsed["_request"], info["request"], manifest["contracts"])
        else:
            check(parsed == {}, "Postman jobs must be parameterless")


def main():
    manifest = read_json(BUNDLE / "contracts-manifest.json")
    schema = read_json(BUNDLE / "table-specification.json")
    validate_sources(manifest, schema)
    with TemporaryDirectory(prefix="ubhr-contract-") as directory:
        asyncio.run(validate_adapter(manifest, Path(directory) / "portal.sqlite3"))
    validate_postman(manifest)
    print("PASS: screenshot field references, five DTOs, six fixtures, three actual adapter calls, SQLite restart, Postman")
    print("X++ compilation, existing EDT/index properties and live F&O permissions remain unverified.")


if __name__ == "__main__":
    main()
