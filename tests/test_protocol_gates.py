from pathlib import Path

import pytest

from codex_feishu_bridge.codex.app_server_client import AppServerProtocol, ProtocolError
from codex_feishu_bridge.codex.compatibility import CompatibilityMatrix


ROOT = Path(__file__).parents[1]
VERSION = ROOT / "generated/codex/0.145.0"
VERSION_148 = ROOT / "generated/codex/0.148.0"


def initialized(protocol: AppServerProtocol) -> None:
    protocol.build_initialize()
    protocol.observe_incoming(
        {
            "id": 0,
            "result": {
                "codexHome": "C:/fixture",
                "platformFamily": "windows",
                "platformOs": "windows",
                "userAgent": "fixture",
            },
        }
    )
    protocol.build_initialized()


def test_experimental_method_requires_individual_allowlist() -> None:
    matrix = CompatibilityMatrix.load(VERSION / "compatibility-matrix.json")
    blocked = AppServerProtocol(
        VERSION / "stable",
        experimental_schema_root=VERSION / "experimental",
        experimental_api=True,
        compatibility_matrix=matrix,
    )
    initialized(blocked)
    with pytest.raises(ProtocolError, match="individually approved"):
        blocked.build_request("process/spawn", {"command": ["cmd.exe"]})


def test_thread_queue_add_requires_and_accepts_individual_allowlist() -> None:
    matrix = CompatibilityMatrix.load(VERSION_148 / "compatibility-matrix.json")
    params = {
        "threadId": "thread",
        "input": [{"type": "text", "text": "queued"}],
        "clientUserMessageId": "client-message",
    }
    blocked = AppServerProtocol(
        VERSION_148 / "stable",
        experimental_schema_root=VERSION_148 / "experimental",
        experimental_api=True,
        compatibility_matrix=matrix,
    )
    initialized(blocked)
    with pytest.raises(ProtocolError, match="individually approved"):
        blocked.build_request("thread/queue/add", params)

    allowed = AppServerProtocol(
        VERSION_148 / "stable",
        experimental_schema_root=VERSION_148 / "experimental",
        experimental_api=True,
        compatibility_matrix=matrix,
        approved_experimental_client_methods=frozenset({"thread/queue/add"}),
    )
    initialized(allowed)
    request = allowed.build_request("thread/queue/add", params)
    assert request["method"] == "thread/queue/add"
    allowed.observe_incoming(
        {
            "id": request["id"],
            "result": {
                "queuedSubmission": {
                    "id": "queued-1",
                    "input": params["input"],
                    "clientUserMessageId": "client-message",
                }
            },
        }
    )


def test_unknown_and_duplicate_response_ids_fail_closed() -> None:
    protocol = AppServerProtocol(VERSION / "stable")
    initialized(protocol)
    request = protocol.build_request("configRequirements/read")
    protocol.observe_incoming({"id": request["id"], "result": {"requirements": None}})
    with pytest.raises(ProtocolError, match="unknown or already resolved"):
        protocol.observe_incoming({"id": request["id"], "result": {"requirements": None}})


def test_experimental_server_request_field_requires_individual_allowlist() -> None:
    matrix = CompatibilityMatrix.load(VERSION / "compatibility-matrix.json")
    message = {
        "id": 41,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "itemId": "item",
            "startedAtMs": 1,
            "threadId": "thread",
            "turnId": "turn",
            "availableDecisions": ["accept", "decline"],
        },
    }
    blocked = AppServerProtocol(
        VERSION / "stable",
        experimental_schema_root=VERSION / "experimental",
        experimental_api=True,
        compatibility_matrix=matrix,
    )
    initialized(blocked)
    with pytest.raises(ProtocolError, match="incoming field"):
        blocked.observe_incoming(message)

    allowed = AppServerProtocol(
        VERSION / "stable",
        experimental_schema_root=VERSION / "experimental",
        experimental_api=True,
        compatibility_matrix=matrix,
        approved_experimental_server_fields=frozenset(
            {("item/commandExecution/requestApproval", "availableDecisions")}
        ),
    )
    initialized(allowed)
    allowed.observe_incoming(message)
