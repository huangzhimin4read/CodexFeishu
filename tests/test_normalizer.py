import base64
from pathlib import Path

import pytest

from codex_feishu_bridge.codex.normalizer import (
    RolloutNormalizer,
    RolloutRecordError,
    UnsupportedRolloutVersion,
)
from codex_feishu_bridge.models import EventKind


def test_response_item_allowlist_and_hidden_role_filter() -> None:
    normalizer = RolloutNormalizer()
    commentary = normalizer.normalize(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "thread_id": "t",
                "turn_id": "u",
                "item_id": "i",
                "content": [{"type": "output_text", "text": "hello"}],
            },
        }
    )
    assert commentary is not None
    assert commentary.kind is EventKind.COMMENTARY
    assert commentary.text == "hello"
    assert normalizer.normalize(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "hidden"}],
            },
        }
    ) is None


def test_ambiguous_agent_message_is_not_guessed() -> None:
    normalizer = RolloutNormalizer()
    assert normalizer.normalize(
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "x"}}
    ) is None
    with pytest.raises(RolloutRecordError, match="unknown assistant message phase"):
        normalizer.normalize(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "unknown",
                    "content": [{"type": "output_text", "text": "x"}],
                },
            }
        )


def test_unknown_rollout_version_fails_closed() -> None:
    with pytest.raises(UnsupportedRolloutVersion):
        RolloutNormalizer().normalize(
            {"type": "session_meta", "payload": {"rollout_version": "2"}}
        )


def test_unknown_record_type_is_ignored() -> None:
    assert RolloutNormalizer().normalize(
        {"type": "internal_telemetry", "payload": {"secret": "not emitted"}}
    ) is None


def test_visible_tool_output_image_is_normalized_as_commentary() -> None:
    content = b"\x89PNG\r\n\x1a\nvisible-fixture"
    data_url = "data:image/png;base64," + base64.b64encode(content).decode("ascii")
    event = RolloutNormalizer().normalize(
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "id": "tool-output",
                "thread_id": "thread",
                "turn_id": "turn",
                "output": [
                    {"type": "input_text", "text": "已查看图像"},
                    {"type": "input_image", "image_url": data_url, "detail": "original"},
                ],
            },
        }
    )
    assert event is not None
    assert event.kind is EventKind.COMMENTARY
    assert event.source_type == "visible_tool_output" and event.text == ""
    assert len(event.images) == 1
    assert event.images[0].mime_type == "image/png"
    assert event.images[0].content == content


def test_embedded_image_invalid_data_fails_closed() -> None:
    with pytest.raises(RolloutRecordError, match="base64"):
        RolloutNormalizer().normalize(
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "id": "tool-output",
                    "thread_id": "thread",
                    "turn_id": "turn",
                    "output": [
                        {"type": "input_image", "image_url": "data:image/png;base64,***"}
                    ],
                },
            }
        )


def test_nested_internal_metadata_supplies_authoritative_turn_identity() -> None:
    event = RolloutNormalizer().normalize(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "thread_id": "thread",
                "id": "item",
                "content": [{"type": "output_text", "text": "visible"}],
                "internal_chat_message_metadata_passthrough": {
                    "turn_id": "turn-explicit"
                },
            },
        }
    )
    assert event is not None
    assert event.turn_id == "turn-explicit"


def test_conflicting_turn_identities_fail_closed() -> None:
    with pytest.raises(RolloutRecordError, match="conflicting turn identities"):
        RolloutNormalizer().normalize(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "commentary",
                    "thread_id": "thread",
                    "turn_id": "turn-top",
                    "id": "item",
                    "content": [{"type": "output_text", "text": "visible"}],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": "turn-nested"
                    },
                },
            }
        )
