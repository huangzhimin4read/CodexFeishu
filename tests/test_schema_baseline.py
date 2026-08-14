import json
from pathlib import Path

from jsonschema import Draft7Validator


ROOT = Path(__file__).parents[1]
VERSION_ROOT = ROOT / "generated" / "codex" / "0.145.0"


def _matrix() -> dict:
    return json.loads(
        (VERSION_ROOT / "compatibility-matrix.json").read_text(encoding="utf-8")
    )


def test_reviewed_schema_hashes_are_reproduced() -> None:
    matrix = _matrix()
    assert matrix["stableProtocolSchemaSha256"] == (
        "7B0AB1679BE705644A1B6C3F486F15E2F90E5F900486D90073B1DC5F0CF4C62C"
    )
    assert matrix["experimentalProtocolSchemaSha256"] == (
        "1F66700D1CC3DE4A5004E5614A6098878B405C7E7C5F8C9BE97FC900D0AD6C68"
    )


def test_wire_enum_and_sandbox_union_facts() -> None:
    matrix = _matrix()
    approval = matrix["approvalPolicy"]["oneOf"][0]["enum"]
    assert approval == ["untrusted", "on-request", "never"]
    assert matrix["approvalsReviewer"]["enum"] == [
        "user",
        "auto_review",
        "guardian_subagent",
    ]
    sandbox = matrix["sandboxPolicy"]["stable"]
    assert set(sandbox) == {
        "dangerFullAccess",
        "readOnly",
        "externalSandbox",
        "workspaceWrite",
    }
    assert sandbox["dangerFullAccess"]["properties"] == ["type"]
    assert sandbox["readOnly"]["networkAccess"]["type"] == "boolean"
    assert sandbox["workspaceWrite"]["networkAccess"]["type"] == "boolean"
    assert "writableRoots" in sandbox["workspaceWrite"]["properties"]


def test_request_field_classification_is_schema_derived() -> None:
    selected = _matrix()["selectedRequestFieldClassification"]
    assert selected["item/tool/requestUserInput"] == {
        "methodStable": True,
    }
    command = selected["item/commandExecution/requestApproval"]
    assert command["availableDecisions"]["stable"] is False
    assert command["availableDecisions"]["experimental"] is True
    assert command["availableDecisions"]["stableSchemaPath"] == (
        "CommandExecutionRequestApprovalParams.json"
    )
    assert command["additionalPermissions"]["stable"] is False
    assert command["additionalPermissions"]["experimental"] is True
    assert command["additionalPermissions"]["experimentalSchemaPath"] == (
        "CommandExecutionRequestApprovalParams.json"
    )


def test_generated_root_schemas_are_valid_draft7_schemas() -> None:
    for mode in ("stable", "experimental"):
        for name in (
            "ClientRequest.json",
            "ClientNotification.json",
            "ServerRequest.json",
            "ServerNotification.json",
        ):
            schema = json.loads((VERSION_ROOT / mode / name).read_text(encoding="utf-8"))
            Draft7Validator.check_schema(schema)
