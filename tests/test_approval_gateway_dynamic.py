import pytest

from codex_feishu_bridge.codex.approval_gateway import (
    ApprovalGatewayError,
    _approval_card,
    _decision_map,
    _resolve_dynamic_response,
)


def test_request_user_input_dynamic_response_validates_exact_fields() -> None:
    params = {
        "questions": [
            {
                "id": "choice",
                "header": "Choice",
                "question": "Pick",
                "options": [{"label": "A"}, {"label": "B"}],
            }
        ]
    }
    special = _decision_map("item/tool/requestUserInput", params)["submit"]
    response = _resolve_dynamic_response(special, {"form_value": {"choice": "A"}})
    assert response == {"answers": {"choice": {"answers": ["A"]}}}
    with pytest.raises(ApprovalGatewayError, match="outside"):
        _resolve_dynamic_response(special, {"form_value": {"choice": "C"}})


def test_mcp_form_dynamic_response_enforces_schema_field_set() -> None:
    params = {
        "mode": "form",
        "requestedSchema": {
            "type": "object",
            "required": ["count"],
            "properties": {"count": {"type": "integer"}, "name": {"type": "string"}},
        },
    }
    special = _decision_map("mcpServer/elicitation/request", params)["submit"]
    assert _resolve_dynamic_response(
        special, {"form_value": {"count": "2", "name": "x"}}
    ) == {"action": "accept", "content": {"count": 2, "name": "x"}}


def test_approval_card_shows_redacted_action_summary_without_patch_body() -> None:
    card = _approval_card(
        "applyPatchApproval",
        "opaque",
        ("approved", "deny"),
        {
            "reason": "update access_token=do-not-send",
            "grantRoot": "D:/project",
            "fileChanges": {
                "D:/project/a.py": {"type": "update", "unified_diff": "PRIVATE PATCH BODY"}
            },
        },
    )
    summary = card["elements"][0]["text"]["content"]
    assert "D:/project/a.py (update)" in summary
    assert "access_token=<redacted>" in summary
    assert "PRIVATE PATCH BODY" not in summary
