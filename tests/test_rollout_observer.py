import base64
import json
import os
from pathlib import Path

import pytest

from codex_feishu_bridge.codex.rollout_observer import (
    IncrementalRolloutReader,
    RolloutReadError,
    SourceReplacedError,
)


FIXTURES = Path(__file__).parent / "fixtures" / "rollout"
SESSION = b'{"type":"session_meta","payload":{"rollout_version":"1","id":"t"}}\n'


def test_complete_records_advance_but_partial_line_does_not(tmp_path: Path) -> None:
    source = tmp_path / "rollout.jsonl"
    complete = (
        b'{"type":"response_item","payload":{"type":"message","role":"assistant",'
        b'"phase":"commentary","thread_id":"t","turn_id":"u","item_id":"i",'
        b'"content":[{"type":"output_text","text":"one"}]}}\n'
    )
    partial = b'{"type":"response_item","payload":'
    source.write_bytes(SESSION + complete + partial)

    reader = IncrementalRolloutReader()
    first = reader.read(source)
    assert len(first.events) == 1
    assert first.cursor.committed_offset == len(SESSION + complete)

    suffix = (
        b'{"type":"message","role":"assistant","phase":"final_answer",'
        b'"thread_id":"t","turn_id":"u","item_id":"j",'
        b'"content":[{"type":"output_text","text":"two"}]}}\n'
    )
    source.write_bytes(SESSION + complete + partial + suffix)
    second = reader.read(source, first.cursor)
    assert len(second.events) == 1
    assert second.events[0].text == "two"
    assert second.cursor.committed_offset == len(SESSION + complete + partial + suffix)


def test_invalid_complete_record_does_not_advance_cursor(tmp_path: Path) -> None:
    source = tmp_path / "rollout.jsonl"
    source.write_bytes(SESSION + b'{"type":"noop","payload":{}}\n')
    reader = IncrementalRolloutReader()
    first = reader.read(source)
    source.write_bytes(source.read_bytes() + b'{not-json}\n')
    with pytest.raises(RolloutReadError, match="invalid complete JSONL"):
        reader.read(source, first.cursor)
    assert first.cursor.committed_offset == len(SESSION + b'{"type":"noop","payload":{}}\n')


def test_visible_fixture_filters_hidden_and_deduplicates_in_storage(tmp_path: Path) -> None:
    from codex_feishu_bridge.storage import BridgeStorage

    batch = IncrementalRolloutReader().read(FIXTURES / "visible.jsonl")
    assert len(batch.events) == 3
    assert all("must not leave" not in event.text for event in batch.events)
    with BridgeStorage(tmp_path / "m0.db") as storage:
        storage.initialize()
        assert storage.store_rollout_batch(batch) == 2
        assert storage.item_count() == 2
        assert storage.store_rollout_batch(batch) == 0
        assert storage.item_count() == 2


@pytest.mark.parametrize("fixture", ["unknown-version.jsonl", "unknown-phase.jsonl"])
def test_unsupported_control_relevant_fixture_fails_closed(fixture: str) -> None:
    with pytest.raises(RolloutReadError):
        IncrementalRolloutReader().read(FIXTURES / fixture)


def test_file_replacement_is_detected(tmp_path: Path) -> None:
    source = tmp_path / "rollout.jsonl"
    source.write_bytes(SESSION + b'{"type":"noop","payload":{}}\n')
    reader = IncrementalRolloutReader()
    first = reader.read(source)
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(SESSION + b'{"type":"noop2","payload":{}}\n')
    os.replace(replacement, source)
    with pytest.raises(SourceReplacedError, match="identity changed"):
        reader.read(source, first.cursor)


def test_in_place_change_to_last_committed_record_is_detected(tmp_path: Path) -> None:
    source = tmp_path / "rollout.jsonl"
    original = SESSION + b'{"type":"noop","payload":{"value":"a"}}\n'
    source.write_bytes(original)
    reader = IncrementalRolloutReader()
    first = reader.read(source)
    changed = original.replace(b'"a"', b'"b"')
    assert len(changed) == len(original)
    source.write_bytes(changed)
    with pytest.raises(SourceReplacedError, match="record changed"):
        reader.read(source, first.cursor)


def test_large_generated_fixture_advances_once(tmp_path: Path) -> None:
    source = tmp_path / "large.jsonl"
    noop = b'{"type":"internal_telemetry","payload":{"value":1}}\n'
    visible = (
        b'{"type":"response_item","payload":{"type":"message","role":"assistant",'
        b'"phase":"final_answer","thread_id":"t","turn_id":"u","item_id":"final",'
        b'"content":[{"type":"output_text","text":"done"}]}}\n'
    )
    source.write_bytes(SESSION + noop * 25_000 + visible)
    batch = IncrementalRolloutReader().read(source)
    assert len(batch.events) == 1
    assert batch.events[0].text == "done"
    assert batch.cursor.committed_offset == source.stat().st_size


def test_current_desktop_context_is_bound_and_recovers_after_restart(tmp_path: Path) -> None:
    source = tmp_path / "current.jsonl"
    context = b'{"type":"turn_context","payload":{"turn_id":"turn-current"}}\n'
    idless_event = (
        b'{"type":"event_msg","payload":{"type":"agent_message",'
        b'"phase":"commentary","message":"derived duplicate"}}\n'
    )
    first_message = (
        b'{"type":"response_item","payload":{"type":"message","role":"assistant",'
        b'"phase":"commentary","id":"item-current-1",'
        b'"content":[{"type":"output_text","text":"first"}]}}\n'
    )
    second_message = (
        b'{"type":"response_item","payload":{"type":"message","role":"assistant",'
        b'"phase":"final_answer","id":"item-current-2",'
        b'"content":[{"type":"output_text","text":"second"}]}}\n'
    )
    source.write_bytes(SESSION + context + idless_event + first_message)

    first = IncrementalRolloutReader().read(source, expected_thread_id="t")
    assert [(event.thread_id, event.turn_id, event.item_id, event.text) for event in first.events] == [
        ("t", "turn-current", "item-current-1", "first")
    ]

    with source.open("ab") as handle:
        handle.write(second_message)
    # A new reader models a service restart and must recover turn_context from
    # the committed prefix without relying on process-local sticky state.
    second = IncrementalRolloutReader().read(
        source,
        first.cursor,
        expected_thread_id="t",
    )
    assert [(event.turn_id, event.item_id, event.text) for event in second.events] == [
        ("turn-current", "item-current-2", "second")
    ]


def test_current_desktop_visible_tool_image_inherits_session_and_turn(tmp_path: Path) -> None:
    source = tmp_path / "image-output.jsonl"
    image = b"\x89PNG\r\n\x1a\nrollout-image"
    data_url = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
    context = b'{"type":"turn_context","payload":{"turn_id":"turn-image"}}\n'
    output = (
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "id": "image-output",
                    "call_id": "call-image",
                    "output": [{"type": "input_image", "image_url": data_url}],
                },
            },
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    source.write_bytes(SESSION + context + output)
    batch = IncrementalRolloutReader().read(source, expected_thread_id="t")
    assert len(batch.events) == 1
    event = batch.events[0]
    assert (event.thread_id, event.turn_id, event.item_id) == (
        "t",
        "turn-image",
        "image-output",
    )
    assert event.images[0].content == image


def test_nested_turn_identity_wins_over_interleaved_current_context(tmp_path: Path) -> None:
    source = tmp_path / "interleaved.jsonl"
    records = [
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "local"}},
        {"type": "turn_context", "payload": {"turn_id": "remote"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "remote"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "id": "local-item",
                "content": [{"type": "output_text", "text": "local response"}],
                "internal_chat_message_metadata_passthrough": {"turn_id": "local"},
            },
        },
    ]
    source.write_bytes(
        SESSION
        + b"".join(
            json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n"
            for record in records
        )
    )

    reader = IncrementalRolloutReader()
    batch = reader.read(source, expected_thread_id="t")
    assert [(event.turn_id, event.text) for event in batch.events] == [
        ("local", "local response")
    ]
    assert batch.active_turn_ids == frozenset({"local", "remote"})

    with source.open("ab") as handle:
        handle.write(
            b'{"type":"event_msg","payload":{"type":"task_complete",'
            b'"turn_id":"remote"}}\n'
        )
    restarted = IncrementalRolloutReader().read(
        source, batch.cursor, expected_thread_id="t"
    )
    assert restarted.active_turn_ids == frozenset({"local"})
