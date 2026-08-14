"""Shared validation for immutable image bytes captured from Codex rollouts."""

from __future__ import annotations

import base64
import binascii


MAX_IMAGE_BYTES = 10 * 1024 * 1024

_SUFFIX_TO_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".heic": "image/heic",
}

_MIME_TO_SUFFIX = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "image/tiff": ".tiff",
    "image/heic": ".heic",
}


def mime_for_suffix(suffix: str) -> str | None:
    return _SUFFIX_TO_MIME.get(suffix.casefold())


def suffix_for_mime(mime_type: str) -> str | None:
    return _MIME_TO_SUFFIX.get(mime_type.casefold())


def detect_image_type(content: bytes) -> tuple[str, str] | None:
    """Return a supported MIME/suffix pair from bytes without trusting headers."""

    for mime_type, suffix in _MIME_TO_SUFFIX.items():
        if validate_image_magic(content, suffix):
            return mime_type, suffix
    return None


def validate_image_magic(content: bytes, suffix: str) -> bool:
    suffix = suffix.casefold()
    if suffix in {".jpg", ".jpeg"}:
        return content.startswith(b"\xff\xd8\xff")
    if suffix == ".png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix == ".gif":
        return content.startswith((b"GIF87a", b"GIF89a"))
    if suffix == ".bmp":
        return content.startswith(b"BM")
    if suffix == ".ico":
        return content.startswith(b"\x00\x00\x01\x00")
    if suffix in {".tif", ".tiff"}:
        return content.startswith((b"II*\x00", b"MM\x00*"))
    if suffix == ".webp":
        return len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    if suffix == ".heic":
        return len(content) >= 12 and content[4:8] == b"ftyp" and content[8:12] in {
            b"heic",
            b"heix",
            b"hevc",
            b"hevx",
            b"mif1",
            b"msf1",
        }
    return False


def decode_image_data_url(value: str) -> tuple[str, str, bytes]:
    """Decode a strict base64 image data URL and verify size and file magic."""

    if not value.startswith("data:") or "," not in value:
        raise ValueError("embedded image is not a data URL")
    header, encoded = value.split(",", 1)
    parts = header[5:].split(";")
    mime_type = parts[0].casefold()
    if len(parts) != 2 or parts[1].casefold() != "base64":
        raise ValueError("embedded image must use strict base64 encoding")
    suffix = _MIME_TO_SUFFIX.get(mime_type)
    if suffix is None:
        raise ValueError("unsupported embedded image MIME type")
    if len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4 + 4:
        raise ValueError("embedded image is too large")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("embedded image base64 is invalid") from exc
    if not content:
        raise ValueError("embedded image is empty")
    if len(content) > MAX_IMAGE_BYTES:
        raise ValueError("embedded image is too large")
    if not validate_image_magic(content, suffix):
        raise ValueError("embedded image content does not match its MIME type")
    return mime_type, suffix, content
