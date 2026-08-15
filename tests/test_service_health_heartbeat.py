from codex_feishu_bridge.service import BridgeService


class _BusyOutbox:
    def __init__(self, successful_runs: int) -> None:
        self.successful_runs = successful_runs
        self.calls = 0

    def run_once(self) -> bool:
        self.calls += 1
        return self.calls <= self.successful_runs


def test_busy_outbox_refreshes_health_every_five_deliveries() -> None:
    service = BridgeService.__new__(BridgeService)
    service.outbox_worker = _BusyOutbox(20)
    heartbeats: list[int] = []
    service._write_status = lambda: heartbeats.append(service.outbox_worker.calls)

    service._drain_outbox()

    assert service.outbox_worker.calls == 20
    assert heartbeats == [5, 10, 15, 20]


def test_short_outbox_batch_does_not_emit_an_extra_mid_batch_heartbeat() -> None:
    service = BridgeService.__new__(BridgeService)
    service.outbox_worker = _BusyOutbox(3)
    heartbeats: list[int] = []
    service._write_status = lambda: heartbeats.append(service.outbox_worker.calls)

    service._drain_outbox()

    assert service.outbox_worker.calls == 4
    assert heartbeats == []
