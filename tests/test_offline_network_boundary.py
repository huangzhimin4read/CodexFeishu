import socket
from pathlib import Path

from codex_feishu_bridge.codex.rollout_observer import IncrementalRolloutReader
from codex_feishu_bridge.runtime_storage import RuntimeStorage


def test_m0_offline_fixture_and_shadow_path_make_zero_network_calls(
    tmp_path: Path, monkeypatch
) -> None:
    calls = []

    def denied(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("offline test attempted network access")

    monkeypatch.setattr(socket.socket, "connect", denied)
    source = tmp_path / "rollout.jsonl"
    source.write_text(
        '{"type":"session_meta","payload":{"rollout_version":"1","id":"thread"}}\n',
        encoding="utf-8",
    )
    batch = IncrementalRolloutReader().read(source, expected_thread_id="thread")
    with RuntimeStorage(tmp_path / "shadow.db") as storage:
        storage.initialize_runtime(sink_mode="shadow_only")
        storage.store_rollout_batch(batch)
    assert calls == []
