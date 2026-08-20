"""Contract-bound Feishu HTTP client with explicit uncertainty semantics."""

from __future__ import annotations

import time
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

from .contracts import EndpointContract, TenantContract
from .credentials import WindowsCredentialManager
from .rate_limit import RateLimiter


class ProviderOutcome(StrEnum):
    CONFIRMED = "confirmed"
    RETRYABLE = "retryable"
    PERMANENT = "permanent"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProviderResult:
    outcome: ProviderOutcome
    code: str
    message_id: str | None = None
    thread_id: str | None = None
    chat_id: str | None = None
    image_key: str | None = None
    file_key: str | None = None
    retry_after_seconds: float | None = None
    response: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ResourceDownloadResult:
    outcome: ProviderOutcome
    code: str
    content: bytes | None = None
    content_type: str | None = None
    retry_after_seconds: float | None = None


class FeishuClient:
    def __init__(
        self,
        *,
        contract: TenantContract,
        app_id: str,
        credential_target: str,
        base_url: str = "https://open.feishu.cn",
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if contract.app_id != app_id:
            raise ValueError("configured app id does not match tenant contract")
        self.contract = contract
        self.app_id = app_id
        self.credential_target = credential_target
        self.rate_limiter = RateLimiter()
        self.http = httpx.Client(
            base_url=base_url,
            transport=transport,
            timeout=httpx.Timeout(timeout_seconds),
            headers={"User-Agent": "codex-feishu-bridge/0.1"},
        )
        self._tenant_token: str | None = None
        self._token_expires_at = 0.0

    def close(self) -> None:
        self.http.close()

    def _token(self) -> str:
        if self._tenant_token and time.monotonic() < self._token_expires_at - 60:
            return self._tenant_token
        endpoint = self.contract.endpoint("tenant_token")
        credential = WindowsCredentialManager().read(self.credential_target)
        result = self._raw_request(
            endpoint,
            json_body={"app_id": self.app_id, "app_secret": credential.app_secret},
            authenticated=False,
        )
        if result.outcome is not ProviderOutcome.CONFIRMED or not result.response:
            raise RuntimeError(f"tenant token request failed: {result.code}")
        token = result.response.get("tenant_access_token")
        expires = result.response.get("expire")
        if not isinstance(token, str) or not token or not isinstance(expires, int):
            raise RuntimeError("tenant token response violates endpoint contract")
        self._tenant_token = token
        self._token_expires_at = time.monotonic() + max(0, expires)
        return token

    def _raw_request(
        self,
        endpoint: EndpointContract,
        *,
        path_parameters: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        form_data: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        chat_id: str | None = None,
        authenticated: bool = True,
    ) -> ProviderResult:
        self.rate_limiter.wait(endpoint, chat_id)
        if json_body is not None and (form_data is not None or files is not None):
            return ProviderResult(ProviderOutcome.PERMANENT, "conflicting_request_encodings")
        path = endpoint.path
        for name, value in (path_parameters or {}).items():
            if not value or "/" in value or "\\" in value:
                return ProviderResult(ProviderOutcome.PERMANENT, "invalid_path_parameter")
            path = path.replace("{" + name + "}", value)
        if "{" in path or "}" in path:
            return ProviderResult(ProviderOutcome.PERMANENT, "unbound_path_parameter")
        headers = {"Authorization": f"Bearer {self._token()}"} if authenticated else {}
        try:
            response = self.http.request(
                endpoint.method,
                path,
                params=query,
                json=json_body,
                data=form_data,
                files=files,
                headers=headers,
            )
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteError, httpx.RemoteProtocolError):
            # Bytes may have reached the provider; callers must reconcile before retry.
            return ProviderResult(ProviderOutcome.UNKNOWN, "transport_unknown")
        retry_after = response.headers.get("Retry-After")
        retry_seconds = float(retry_after) if retry_after and retry_after.isdigit() else None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code == 429:
            return ProviderResult(ProviderOutcome.RETRYABLE, "http_429", retry_after_seconds=retry_seconds)
        if response.status_code in {401, 403}:
            self._tenant_token = None
            self._token_expires_at = 0
            return ProviderResult(ProviderOutcome.PERMANENT, f"http_{response.status_code}", response=payload)
        if 500 <= response.status_code:
            return ProviderResult(ProviderOutcome.RETRYABLE, f"http_{response.status_code}", response=payload)
        if not isinstance(payload, dict):
            return ProviderResult(ProviderOutcome.PERMANENT, "invalid_json_response")
        code = payload.get("code")
        if response.status_code >= 400 or code not in {0, "0", None}:
            provider_code = str(code if code is not None else response.status_code)
            if provider_code in {"230020"}:
                return ProviderResult(ProviderOutcome.RETRYABLE, provider_code, response=payload)
            return ProviderResult(ProviderOutcome.PERMANENT, provider_code, response=payload)
        message_id = _json_pointer(payload, endpoint.response_message_id_pointer)
        if endpoint.response_message_id_pointer and message_id is None:
            return ProviderResult(ProviderOutcome.PERMANENT, "missing_message_id_response")
        if message_id is not None and not isinstance(message_id, str):
            return ProviderResult(ProviderOutcome.PERMANENT, "invalid_message_id_response")
        thread_id = _json_pointer(payload, endpoint.response_thread_id_pointer)
        if thread_id is not None and not isinstance(thread_id, str):
            return ProviderResult(ProviderOutcome.PERMANENT, "invalid_thread_id_response")
        response_chat_id = _json_pointer(payload, endpoint.response_chat_id_pointer)
        if endpoint.response_chat_id_pointer and response_chat_id is None:
            return ProviderResult(ProviderOutcome.PERMANENT, "missing_chat_id_response")
        if response_chat_id is not None and not isinstance(response_chat_id, str):
            return ProviderResult(ProviderOutcome.PERMANENT, "invalid_chat_id_response")
        image_key = _json_pointer(payload, endpoint.response_image_key_pointer)
        if endpoint.response_image_key_pointer and image_key is None:
            return ProviderResult(ProviderOutcome.PERMANENT, "missing_image_key_response")
        if image_key is not None and (not isinstance(image_key, str) or not image_key):
            return ProviderResult(ProviderOutcome.PERMANENT, "invalid_image_key_response")
        file_key = _json_pointer(payload, endpoint.response_file_key_pointer)
        if endpoint.response_file_key_pointer and file_key is None:
            return ProviderResult(ProviderOutcome.PERMANENT, "missing_file_key_response")
        if file_key is not None and (not isinstance(file_key, str) or not file_key):
            return ProviderResult(ProviderOutcome.PERMANENT, "invalid_file_key_response")
        return ProviderResult(
            ProviderOutcome.CONFIRMED,
            "0",
            message_id=message_id,
            thread_id=thread_id,
            chat_id=response_chat_id,
            image_key=image_key,
            file_key=file_key,
            response=payload,
        )

    def call(
        self,
        endpoint_name: str,
        *,
        path_parameters: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        chat_id: str | None = None,
    ) -> ProviderResult:
        endpoint = self.contract.endpoint(endpoint_name)
        if endpoint.uuid_location == "json_body":
            if query is not None and "uuid" in query:
                return ProviderResult(ProviderOutcome.PERMANENT, "invalid_uuid_location")
            uuid_value = json_body.get("uuid") if json_body is not None else None
            if not isinstance(uuid_value, str) or not uuid_value:
                return ProviderResult(ProviderOutcome.PERMANENT, "missing_uuid")
        elif endpoint.uuid_location == "query":
            if json_body is not None and "uuid" in json_body:
                return ProviderResult(ProviderOutcome.PERMANENT, "invalid_uuid_location")
            uuid_value = query.get("uuid") if query is not None else None
            if not isinstance(uuid_value, str) or not uuid_value:
                return ProviderResult(ProviderOutcome.PERMANENT, "missing_uuid")
        return self._raw_request(
            endpoint,
            path_parameters=path_parameters,
            query=query,
            json_body=json_body,
            chat_id=chat_id,
        )

    def upload_image(
        self,
        *,
        file_name: str,
        mime_type: str,
        content: bytes,
    ) -> ProviderResult:
        """Upload immutable image bytes for a later image message."""
        endpoint = self.contract.endpoint("upload_image")
        if not file_name or not mime_type or not content:
            return ProviderResult(ProviderOutcome.PERMANENT, "invalid_image_upload")
        if len(content) > 10 * 1024 * 1024:
            return ProviderResult(ProviderOutcome.PERMANENT, "image_too_large")
        return self._raw_request(
            endpoint,
            form_data={"image_type": "message"},
            files={"image": (file_name, content, mime_type)},
        )

    def upload_file(
        self,
        *,
        file_name: str,
        file_type: str,
        mime_type: str,
        content: bytes,
    ) -> ProviderResult:
        """Upload immutable bytes for a later native Feishu file message."""
        endpoint = self.contract.endpoint("upload_file")
        if not file_name or not file_type or not mime_type or not content:
            return ProviderResult(ProviderOutcome.PERMANENT, "invalid_file_upload")
        if len(content) > 20 * 1024 * 1024:
            return ProviderResult(ProviderOutcome.PERMANENT, "file_too_large")
        return self._raw_request(
            endpoint,
            form_data={"file_type": file_type, "file_name": file_name},
            files={"file": (file_name, content, mime_type)},
        )

    def download_message_resource(
        self,
        *,
        message_id: str,
        file_key: str,
        resource_type: str,
        chat_id: str,
        max_bytes: int,
    ) -> ResourceDownloadResult:
        """Download one resource that is cryptographically bound to a message.

        The event callback only persists provider identifiers.  Binary network
        I/O happens later on the service worker and is bounded while streaming,
        so a forged Content-Length or oversized body cannot fill memory.
        """

        endpoint = self.contract.endpoint("download_message_resource")
        if resource_type not in {"image", "file"} or max_bytes <= 0:
            return ResourceDownloadResult(ProviderOutcome.PERMANENT, "invalid_resource_request")
        for value in (message_id, file_key):
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,512}", value or ""):
                return ResourceDownloadResult(
                    ProviderOutcome.PERMANENT, "invalid_resource_path_parameter"
                )
        path = endpoint.path.replace("{message_id}", message_id).replace(
            "{file_key}", file_key
        )
        self.rate_limiter.wait(endpoint, chat_id)
        headers = {"Authorization": f"Bearer {self._token()}"}
        try:
            with self.http.stream(
                endpoint.method,
                path,
                params={"type": resource_type},
                headers=headers,
            ) as response:
                retry_after = response.headers.get("Retry-After")
                retry_seconds = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else None
                )
                if response.status_code == 429:
                    return ResourceDownloadResult(
                        ProviderOutcome.RETRYABLE,
                        "http_429",
                        retry_after_seconds=retry_seconds,
                    )
                if response.status_code in {401, 403}:
                    self._tenant_token = None
                    self._token_expires_at = 0
                    return ResourceDownloadResult(
                        ProviderOutcome.RETRYABLE
                        if response.status_code == 401
                        else ProviderOutcome.PERMANENT,
                        f"http_{response.status_code}",
                    )
                if 300 <= response.status_code < 400:
                    return ResourceDownloadResult(
                        ProviderOutcome.PERMANENT, "unexpected_redirect"
                    )
                if response.status_code >= 500:
                    return ResourceDownloadResult(
                        ProviderOutcome.RETRYABLE, f"http_{response.status_code}"
                    )
                if response.status_code >= 400:
                    return ResourceDownloadResult(
                        ProviderOutcome.PERMANENT, f"http_{response.status_code}"
                    )
                declared = response.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    return ResourceDownloadResult(
                        ProviderOutcome.PERMANENT, "resource_too_large"
                    )
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        return ResourceDownloadResult(
                            ProviderOutcome.PERMANENT, "resource_too_large"
                        )
                if not body:
                    return ResourceDownloadResult(
                        ProviderOutcome.PERMANENT, "resource_empty"
                    )
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
                return ResourceDownloadResult(
                    ProviderOutcome.CONFIRMED,
                    "0",
                    bytes(body),
                    content_type.casefold() or "application/octet-stream",
                )
        except httpx.TransportError:
            # GET has no provider-side effect and can be retried safely.
            return ResourceDownloadResult(ProviderOutcome.RETRYABLE, "transport_retryable")


def _json_pointer(payload: dict[str, Any], pointer: str | None) -> Any:
    if not pointer:
        return None
    if not pointer.startswith("/"):
        raise ValueError("response pointer must be an RFC 6901 absolute pointer")
    current: Any = payload
    for raw in pointer[1:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current
