"""Connector import contract: Swagger 2.0, business-only tools and safe inputs."""
import copy
import json
from pathlib import Path

from jsonschema import Draft4Validator, RefResolver
from swagger_spec_validator.validator20 import validate_spec

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "connector" / "swagger2.json"
SPEC = json.loads(SPEC_PATH.read_text())
HOST = "ubtihrprocess-egg3f7awbsbjdjd5.centralindia-01.azurewebsites.net"
OPERATIONS = {
    "ListFinanceCustomers",
    "GetFinanceCustomer",
    "GetFinanceCustomerTransactions",
    "CreateFinanceCustomer",
    "UpdateFinanceCustomer",
    "DeleteFinanceCustomer",
}


def operations():
    for path, methods in SPEC["paths"].items():
        for method, operation in methods.items():
            if method in {"get", "post", "patch", "delete"}:
                yield path, method, operation


def validate_payload(name, payload):
    resolver = RefResolver.from_schema(SPEC)
    Draft4Validator(SPEC["definitions"][name], resolver=resolver).validate(payload)


def test_document_is_valid_swagger_2():
    assert SPEC["swagger"] == "2.0"
    assert "openapi" not in SPEC
    validate_spec(copy.deepcopy(SPEC))


def test_actual_target_and_single_prefix():
    assert SPEC["host"] == HOST
    assert SPEC["schemes"] == ["https"]
    assert SPEC["basePath"] == "/finance"
    assert all(not path.startswith("/finance") for path in SPEC["paths"])


def test_only_six_named_business_operations():
    ids = [operation["operationId"] for _, _, operation in operations()]
    assert len(ids) == len(set(ids)) == 6
    assert set(ids) == OPERATIONS
    assert set(SPEC["paths"]) == {
        "/customers", "/customers/{account}", "/customers/{account}/transactions"
    }
    assert not any(word in path for path in SPEC["paths"] for word in ("health", "ready", "metadata", "swagger", "key"))


def test_key_is_connection_auth_not_ai_tool_input():
    definition = SPEC["securityDefinitions"]["FinanceDemoKey"]
    assert definition["type"] == "apiKey"
    assert definition["in"] == "header"
    assert definition["name"] == "X-Finance-Demo-Key"
    assert SPEC["security"] == [{"FinanceDemoKey": []}]
    for _, _, operation in operations():
        assert "security" not in operation or operation["security"]
        assert all(parameter["name"] not in {"X-Finance-Demo-Key", "api_key", "token", "client_secret"}
                   for parameter in operation.get("parameters", []))


def test_limits_are_bounded_and_no_arbitrary_query_inputs():
    for _, _, operation in operations():
        assert all(p["name"] in {"account", "limit", "body"} for p in operation["parameters"])
        for parameter in operation["parameters"]:
            if parameter["name"] == "limit":
                assert parameter["minimum"] == 1
                assert parameter["maximum"] == 20
                assert 1 <= parameter["default"] <= 20


def test_mutation_inputs_are_small_and_fixed_company():
    create = SPEC["definitions"]["CreateCustomerRequest"]
    update = SPEC["definitions"]["UpdateCustomerRequest"]
    assert create["additionalProperties"] is False
    assert update["additionalProperties"] is False
    assert set(create["properties"]) == {"customer_account", "organization_name"}
    assert set(update["properties"]) == {"organization_name"}
    assert create["properties"]["customer_account"]["pattern"].startswith("^TEST-COPILOT-")


def test_financial_values_are_strings_and_no_balance_schema():
    transaction = SPEC["definitions"]["CustomerTransaction"]["properties"]
    assert transaction["amount"]["type"] == "string"
    assert "balance" not in transaction
    assert "total" not in transaction
    assert SPEC["definitions"]["ResultMetadata"]["properties"]["limited"]["type"] == "boolean"


def test_success_and_uncertain_write_examples_validate():
    validate_payload("CustomerResponse", {
        "success": True,
        "data": {"customer_account": "TEST-COPILOT-001", "organization_name": "Copilot Demo Customer", "company": "usmf"},
        "meta": {"company": "usmf", "verified": True},
    })
    validate_payload("TransactionListResponse", {
        "success": True,
        "data": [{"customer_account": "AST-001", "company": "usmf", "amount": "1250.00"}],
        "meta": {"company": "usmf", "limited": True, "limit": 10, "returned": 1},
    })
    validate_payload("DeleteCustomerResponse", {
        "success": True,
        "data": {"customer_account": "TEST-COPILOT-001", "company": "usmf", "deleted": True, "verified": True},
        "meta": {"company": "usmf", "verified": True},
    })
    validate_payload("ErrorResponse", {
        "success": False,
        "error": {"code": "write_outcome_uncertain", "message": "Verify the exact account before retrying.",
                  "retryable": False, "outcome_uncertain": True, "verification_required": True},
    })


def test_explicit_json_success_and_error_responses():
    assert SPEC["consumes"] == ["application/json"]
    assert SPEC["produces"] == ["application/json"]
    for _, method, operation in operations():
        success_status = "201" if method == "post" else "200"
        assert success_status in operation["responses"]
        for status in ["401", "403", "404", "409", "422", "503", "504", "default"]:
            assert operation["responses"][status]["schema"] == {"$ref": "#/definitions/ErrorResponse"}


def test_import_size_and_no_embedded_credential_values():
    assert SPEC_PATH.stat().st_size < 1_000_000
    # API-key credentials belong to a connection, never a Swagger default/example.
    for definition in SPEC["securityDefinitions"].values():
        assert not any(k in definition for k in ("default", "example", "value"))
    assert "FINANCE_DEMO_CLIENT_SECRET=" not in SPEC_PATH.read_text()
