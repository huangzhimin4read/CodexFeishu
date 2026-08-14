from codex_feishu_bridge.feishu.long_connection import FeishuLongConnection


class _Client:
    _conn = None


def test_long_connection_reports_pinned_sdk_transport_state() -> None:
    connection = FeishuLongConnection.__new__(FeishuLongConnection)
    connection.client = _Client()
    assert connection.connection_state() == "connecting"
    connection.client._conn = object()
    assert connection.connection_state() == "connected"
