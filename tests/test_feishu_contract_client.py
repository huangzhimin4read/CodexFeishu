import json
from pathlib import Path

import httpx
import pytest

from codex_feishu_bridge.feishu.client import FeishuClient, ProviderOutcome
from codex_feishu_bridge.feishu.contracts import ContractError, load_tenant_contract


def contract_value() -> dict:
    endpoints = []
    for name, method, path, pointer in (
        ("tenant_token", "POST", "/open-apis/auth/v3/tenant_access_token/internal", None),
        ("send_message", "POST", "/open-apis/im/v1/messages", "/data/message_id"),
        ("reply_message", "POST", "/open-apis/im/v1/messages/{message_id}/reply", "/data/message_id"),
        ("preflight_chat", "GET", "/open-apis/im/v1/chats/{chat_id}", None),
        ("list_messages", "GET", "/open-apis/im/v1/messages", None),
        ("update_message", "PUT", "/open-apis/im/v1/messages/{message_id}", None),
        ("delete_message", "DELETE", "/open-apis/im/v1/messages/{message_id}", None),
    ):
        endpoints.append(
            {
                "name": name,
                "method": method,
                "path": path,
                "exact_scopes": [] if name == "tenant_token" else ["im:fixture"],
                "administrator_approved": True,
                "enabled": True,
                "rate_limit": {"capacity": 100, "refill_per_second": 100},
                **(
                    {
                        "response_message_id_pointer": pointer,
                        "uuid_window_seconds": 3600,
                        "uuid_location": "json_body",
                    }
                    if pointer
                    else {}
                ),
            }
        )
    return {
        "schema_version": 1,
        "tenant_key": "tenant",
        "app_id": "app",
        "published_version": "1.0.0",
        "bot_enabled": True,
        "p2p_only": True,
        "source_export_sha256": "a" * 64,
        "endpoints": endpoints,
    }


def load_contract(tmp_path: Path):
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract_value()), encoding="utf-8")
    return load_tenant_contract(path)


def test_contract_is_exact_and_hash_stable(tmp_path: Path) -> None:
    contract = load_contract(tmp_path)
    assert contract.endpoint("send_message").response_message_id_pointer == "/data/message_id"
    assert len(contract.contract_hash) == 64
    value = contract_value()
    value["endpoints"][1]["unexpected"] = True
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ContractError, match="unknown endpoint"):
        load_tenant_contract(path)


def test_client_uses_only_contract_path_and_classifies_result(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "m-1"}})

    client = FeishuClient(
        contract=load_contract(tmp_path),
        app_id="app",
        credential_target="unused",
        transport=httpx.MockTransport(handler),
    )
    client._tenant_token = "fixture"
    client._token_expires_at = 10**20
    result = client.call(
        "reply_message",
        path_parameters={"message_id": "anchor"},
        json_body={"msg_type": "text", "content": "{}", "reply_in_thread": False, "uuid": "u"},
        chat_id="chat",
    )
    client.close()
    assert result.outcome is ProviderOutcome.CONFIRMED and result.message_id == "m-1"
    assert requests[0].url.path == "/open-apis/im/v1/messages/anchor/reply"
    assert "uuid" not in requests[0].url.params
    assert json.loads(requests[0].content) == {
        "content": "{}",
        "msg_type": "text",
        "reply_in_thread": False,
        "uuid": "u",
    }


def test_client_rejects_uuid_outside_contract_location(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    client = FeishuClient(
        contract=load_contract(tmp_path),
        app_id="app",
        credential_target="unused",
        transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(200, json={"code": 0})
        ),
    )
    client._tenant_token = "fixture"
    client._token_expires_at = 10**20
    result = client.call(
        "reply_message",
        path_parameters={"message_id": "anchor"},
        query={"uuid": "wrong-place"},
        json_body={"msg_type": "text", "content": "{}"},
    )
    client.close()
    assert result.outcome is ProviderOutcome.PERMANENT
    assert result.code == "invalid_uuid_location"
    assert requests == []


def test_client_transport_failure_is_explicitly_unknown(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("fixture", request=request)

    client = FeishuClient(
        contract=load_contract(tmp_path),
        app_id="app",
        credential_target="unused",
        transport=httpx.MockTransport(handler),
    )
    client._tenant_token = "fixture"
    client._token_expires_at = 10**20
    result = client.call("preflight_chat", path_parameters={"chat_id": "chat"})
    client.close()
    assert result.outcome is ProviderOutcome.UNKNOWN


def test_topic_group_contract_and_response_thread_are_explicit(tmp_path: Path) -> None:
    value = contract_value()
    value["p2p_only"] = False
    value["group_topic_enabled"] = True
    for endpoint in value["endpoints"]:
        if endpoint["name"] in {"send_message", "reply_message"}:
            endpoint["response_thread_id_pointer"] = "/data/thread_id"
    path = tmp_path / "topic-contract.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    contract = load_tenant_contract(path)
    assert contract.supports_conversation_mode("topic_group")
    assert not contract.supports_conversation_mode("p2p")

    client = FeishuClient(
        contract=contract,
        app_id="app",
        credential_target="unused",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"code": 0, "data": {"message_id": "m", "thread_id": "th"}},
            )
        ),
    )
    client._tenant_token = "fixture"
    client._token_expires_at = 10**20
    result = client.call(
        "reply_message",
        path_parameters={"message_id": "root"},
        json_body={
            "msg_type": "text",
            "content": "{}",
            "reply_in_thread": True,
            "uuid": "u",
        },
    )
    client.close()
    assert result.message_id == "m" and result.thread_id == "th"


def test_create_chat_binds_uuid_to_query_and_chat_id_to_response(tmp_path: Path) -> None:
    value = contract_value()
    value["endpoints"].append(
        {
            "name": "create_chat",
            "method": "POST",
            "path": "/open-apis/im/v1/chats",
            "exact_scopes": ["im:chat:create"],
            "administrator_approved": True,
            "enabled": True,
            "uuid_window_seconds": 36000,
            "uuid_location": "query",
            "response_chat_id_pointer": "/data/chat_id",
            "rate_limit": {"capacity": 1, "refill_per_second": 1},
        }
    )
    path = tmp_path / "create-contract.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    requests: list[httpx.Request] = []
    client = FeishuClient(
        contract=load_tenant_contract(path),
        app_id="app",
        credential_target="unused",
        transport=httpx.MockTransport(
            lambda request: requests.append(request)
            or httpx.Response(200, json={"code": 0, "data": {"chat_id": "chat-created"}})
        ),
    )
    client._tenant_token = "fixture"
    client._token_expires_at = 10**20
    result = client.call(
        "create_chat",
        query={"uuid": "create-uuid", "user_id_type": "open_id"},
        json_body={"name": "项目"},
    )
    client.close()
    assert result.outcome is ProviderOutcome.CONFIRMED
    assert result.chat_id == "chat-created"
    assert requests[0].url.params["uuid"] == "create-uuid"
    assert "uuid" not in json.loads(requests[0].content)


def test_image_upload_uses_contract_bound_multipart_and_returns_image_key(tmp_path: Path) -> None:
    value = contract_value()
    value["endpoints"].append(
        {
            "name": "upload_image",
            "method": "POST",
            "path": "/open-apis/im/v1/images",
            "exact_scopes": ["im:resource"],
            "administrator_approved": True,
            "enabled": True,
            "response_image_key_pointer": "/data/image_key",
            "rate_limit": {"capacity": 1, "refill_per_second": 1},
        }
    )
    path = tmp_path / "image-contract.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": 0, "data": {"image_key": "img-key"}})

    client = FeishuClient(
        contract=load_tenant_contract(path),
        app_id="app",
        credential_target="unused",
        transport=httpx.MockTransport(handler),
    )
    client._tenant_token = "fixture"
    client._token_expires_at = 10**20
    result = client.upload_image(
        file_name="chart.png",
        mime_type="image/png",
        content=b"\x89PNG\r\n\x1a\nfixture",
    )
    client.close()
    assert result.outcome is ProviderOutcome.CONFIRMED and result.image_key == "img-key"
    assert requests[0].url.path == "/open-apis/im/v1/images"
    assert requests[0].headers["content-type"].startswith("multipart/form-data;")
    assert b'name="image_type"' in requests[0].content
    assert b'name="image"; filename="chart.png"' in requests[0].content


def test_message_resource_download_is_contract_bound_and_stream_limited(tmp_path: Path) -> None:
    value = contract_value()
    value.update(
        {
            "schema_version": 2,
            "event_subscriptions": ["im.message.receive_v1"],
            "event_scopes": ["im:message.group_msg"],
            "callback_subscriptions": ["card.action.trigger"],
        }
    )
    value["endpoints"].append(
        {
            "name": "download_message_resource",
            "method": "GET",
            "path": "/open-apis/im/v1/messages/{message_id}/resources/{file_key}",
            "exact_scopes": ["im:message"],
            "administrator_approved": True,
            "enabled": True,
            "response_binary": True,
            "rate_limit": {"capacity": 50, "refill_per_second": 50},
        }
    )
    path = tmp_path / "resource-contract.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    requests: list[httpx.Request] = []
    client = FeishuClient(
        contract=load_tenant_contract(path),
        app_id="app",
        credential_target="unused",
        transport=httpx.MockTransport(
            lambda request: requests.append(request)
            or httpx.Response(200, content=b"fixed-bytes", headers={"Content-Type": "application/pdf"})
        ),
    )
    client._tenant_token = "fixture"
    client._token_expires_at = 10**20
    result = client.download_message_resource(
        message_id="message-1",
        file_key="file-key",
        resource_type="file",
        chat_id="chat",
        max_bytes=100,
    )
    client.close()
    assert result.outcome is ProviderOutcome.CONFIRMED
    assert result.content == b"fixed-bytes" and result.content_type == "application/pdf"
    assert requests[0].url.path.endswith("/messages/message-1/resources/file-key")
    assert requests[0].url.params["type"] == "file"


def test_message_resource_download_rejects_oversized_stream(tmp_path: Path) -> None:
    value = contract_value()
    value["endpoints"].append(
        {
            "name": "download_message_resource",
            "method": "GET",
            "path": "/open-apis/im/v1/messages/{message_id}/resources/{file_key}",
            "exact_scopes": ["im:message:readonly"],
            "administrator_approved": True,
            "enabled": True,
            "response_binary": True,
            "rate_limit": {"capacity": 50, "refill_per_second": 50},
        }
    )
    path = tmp_path / "resource-contract.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    client = FeishuClient(
        contract=load_tenant_contract(path),
        app_id="app",
        credential_target="unused",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"123456")),
    )
    client._tenant_token = "fixture"
    client._token_expires_at = 10**20
    result = client.download_message_resource(
        message_id="message-1",
        file_key="file-key",
        resource_type="file",
        chat_id="chat",
        max_bytes=5,
    )
    client.close()
    assert result.outcome is ProviderOutcome.PERMANENT and result.code == "resource_too_large"
