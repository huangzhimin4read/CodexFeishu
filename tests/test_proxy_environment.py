import pytest

from codex_feishu_bridge.codex.proxy_environment import (
    ProxyEnvironmentError,
    loopback_proxy_environment,
    validate_worker_proxy_environment,
)


def test_loopback_proxy_environment_is_normalized_without_credentials() -> None:
    environment = loopback_proxy_environment(
        {
            "HTTPS_PROXY": "http://127.0.0.1:10808",
            "all_proxy": "socks5://localhost:10808",
            "NO_PROXY": "untrusted.example",
        }
    )
    assert environment == {
        "HTTPS_PROXY": "http://127.0.0.1:10808",
        "https_proxy": "http://127.0.0.1:10808",
        "ALL_PROXY": "socks5://localhost:10808",
        "all_proxy": "socks5://localhost:10808",
        "NO_PROXY": "localhost,127.0.0.1,::1",
        "no_proxy": "localhost,127.0.0.1,::1",
    }
    assert validate_worker_proxy_environment(environment) == environment


@pytest.mark.parametrize(
    "value",
    [
        "http://example.com:8080",
        "http://user:password@127.0.0.1:8080",
        "file://127.0.0.1:8080",
        "http://127.0.0.1",
        "http://127.0.0.1:8080/path",
    ],
)
def test_non_loopback_or_credentialed_proxy_is_rejected(value: str) -> None:
    with pytest.raises(ProxyEnvironmentError):
        loopback_proxy_environment({"HTTPS_PROXY": value})


def test_worker_proxy_map_rejects_unknown_or_non_normalized_keys() -> None:
    with pytest.raises(ProxyEnvironmentError):
        validate_worker_proxy_environment({"SECRET": "value"})
    with pytest.raises(ProxyEnvironmentError):
        validate_worker_proxy_environment(
            {"HTTPS_PROXY": "http://127.0.0.1:10808"}
        )
