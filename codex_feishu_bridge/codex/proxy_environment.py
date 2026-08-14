"""Strict propagation of credential-free loopback proxy settings."""

from __future__ import annotations

import socket
from collections.abc import Mapping
from urllib.parse import urlsplit


class ProxyEnvironmentError(ValueError):
    pass


_PROXY_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
_ALLOWED_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def loopback_proxy_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Return normalized proxy variables or reject unsafe inherited values.

    The launch ticket is readable by the separately-principalled worker.  It
    may therefore contain only credential-free proxy URLs whose endpoint is
    local to this machine; remote/userinfo proxy URLs are never propagated.
    """

    result: dict[str, str] = {}
    for name in _PROXY_NAMES:
        value = source.get(name) or source.get(name.lower())
        if not value:
            continue
        _proxy_endpoint(value)
        result[name] = value
        result[name.lower()] = value
    if result:
        no_proxy = "localhost,127.0.0.1,::1"
        result["NO_PROXY"] = no_proxy
        result["no_proxy"] = no_proxy
    return result


def validate_worker_proxy_environment(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in value.items()
    ):
        raise ProxyEnvironmentError("worker proxy environment must be a string map")
    allowed = {
        name for base in _PROXY_NAMES for name in (base, base.lower())
    } | {"NO_PROXY", "no_proxy"}
    if set(value) - allowed:
        raise ProxyEnvironmentError("worker proxy environment has unknown keys")
    normalized = loopback_proxy_environment(value)
    if normalized != value:
        raise ProxyEnvironmentError("worker proxy environment is not normalized")
    return normalized


def proxy_endpoint_reachable(environment: Mapping[str, str], timeout: float = 3.0) -> bool:
    if not environment:
        return True
    value = next(
        (environment.get(name) for name in _PROXY_NAMES if environment.get(name)),
        None,
    )
    if value is None:
        return False
    host, port = _proxy_endpoint(value)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _proxy_endpoint(value: str) -> tuple[str, int]:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ProxyEnvironmentError("proxy URL is malformed") from exc
    if (
        parsed.scheme.casefold() not in _ALLOWED_SCHEMES
        or parsed.hostname is None
        or parsed.hostname.casefold() not in _LOOPBACK_HOSTS
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ProxyEnvironmentError(
            "proxy URL must be credential-free and target a loopback endpoint"
        )
    return parsed.hostname, port
