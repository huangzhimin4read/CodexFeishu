"""Current-user DPAPI protection for broker-only local key material."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


class DpapiError(RuntimeError):
    pass


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes) -> tuple[_DATA_BLOB, object]:
    buffer = ctypes.create_string_buffer(data)
    return _DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def protect_current_user(data: bytes, *, entropy: bytes = b"CodexFeishu-v1") -> bytes:
    if os.name != "nt":
        raise DpapiError("DPAPI requires Windows")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    source, source_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(entropy)
    target = _DATA_BLOB()
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        "CodexFeishu broker key",
        ctypes.byref(entropy_blob),
        None,
        None,
        0x1,
        ctypes.byref(target),
    ):
        raise DpapiError(f"CryptProtectData failed: {ctypes.get_last_error()}")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def unprotect_current_user(data: bytes, *, entropy: bytes = b"CodexFeishu-v1") -> bytes:
    if os.name != "nt":
        raise DpapiError("DPAPI requires Windows")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    source, source_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(entropy)
    target = _DATA_BLOB()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        0x1,
        ctypes.byref(target),
    ):
        raise DpapiError(f"CryptUnprotectData failed: {ctypes.get_last_error()}")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)
