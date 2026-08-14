import os
from pathlib import Path

import pytest

from codex_feishu_bridge.codex.app_server_client import (
    AppServerProtocol,
    ProtocolError,
    ProtocolState,
    StdioAppServer,
)


ROOT = Path(__file__).parents[1]
STABLE = ROOT / "generated" / "codex" / "0.145.0" / "stable"
EXPERIMENTAL = ROOT / "generated" / "codex" / "0.145.0" / "experimental"


def test_handshake_order_and_wire_shape() -> None:
    protocol = AppServerProtocol(STABLE)
    with pytest.raises(ProtocolError, match="completed handshake"):
        protocol.build_request("thread/start", {"model": "gpt-5.6-terra"})
    initialize = protocol.build_initialize()
    assert initialize["method"] == "initialize"
    assert "jsonrpc" not in initialize
    with pytest.raises(ProtocolError, match="successful initialize"):
        protocol.build_initialized()
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
    initialized = protocol.build_initialized()
    assert initialized == {"method": "initialized", "params": {}}
    request = protocol.build_request("thread/start", {"model": "gpt-5.6-terra"})
    assert request["id"] == 1


def test_duplicate_initialize_and_invalid_wire_are_rejected() -> None:
    protocol = AppServerProtocol(STABLE)
    protocol.build_initialize()
    with pytest.raises(ProtocolError, match="exactly once"):
        protocol.build_initialize()
    with pytest.raises(ProtocolError, match="omit"):
        protocol.encode({"jsonrpc": "2.0", "method": "initialized"})


def test_unknown_incoming_method_fails_schema_validation() -> None:
    protocol = AppServerProtocol(STABLE)
    protocol.build_initialize()
    with pytest.raises(ProtocolError, match="schema validation"):
        protocol.observe_incoming({"method": "server/not-a-method", "params": {}})


def test_stable_schema_rejects_unknown_method() -> None:
    protocol = AppServerProtocol(STABLE)
    protocol.build_initialize()
    with pytest.raises(ProtocolError, match="typed response"):
        protocol.observe_incoming({"id": 0, "result": {}})
    protocol = AppServerProtocol(STABLE)
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
    with pytest.raises(ProtocolError, match="schema validation"):
        protocol.build_request("bridge/not-a-method", {})


def test_schema_valid_error_response_for_unsupported_server_request() -> None:
    protocol = AppServerProtocol(STABLE)

    assert protocol.build_error_response(
        91,
        code=-32601,
        message="Server request is not available in the remote bridge",
    ) == {
        "id": 91,
        "error": {
            "code": -32601,
            "message": "Server request is not available in the remote bridge",
        },
    }


def _codex_executable() -> Path:
    configured = os.environ.get("CODEX_TEST_EXECUTABLE")
    if configured:
        return Path(configured)
    return Path.home() / (
        "AppData/Roaming/npm/node_modules/@openai/codex/node_modules/"
        "@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe"
    )


@pytest.mark.integration
def test_disposable_app_server_initialize_handshake(tmp_path: Path) -> None:
    executable = _codex_executable()
    assert executable.is_file(), f"pinned Codex executable not found: {executable}"
    protocol = AppServerProtocol(STABLE)
    with StdioAppServer(executable, tmp_path / "codex-home", protocol) as server:
        result = server.handshake(timeout=15)
        assert "result" in result.initialize_response
        assert protocol.state is ProtocolState.READY
