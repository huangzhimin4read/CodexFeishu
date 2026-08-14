"""Conservative Windows path canonicalization and identity pinning."""

from __future__ import annotations

import ctypes
import os
import re
from ctypes import wintypes
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path


class PathValidationError(ValueError):
    pass


_SHORT_NAME = re.compile(r"(?i)(?:^|[\\/])[^\\/]*~\d+(?:[\\/]|$)")


@dataclass(frozen=True, slots=True)
class PathIdentity:
    requested_path: Path
    canonical_path: Path
    final_path: str
    volume_serial: int
    file_index: str
    reparse_tag: int
    security_hash: str
    nearest_existing_parent: Path
    missing_suffix: tuple[str, ...]


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


def _reject_ambiguous(raw: str) -> None:
    if not raw or raw != raw.strip():
        raise PathValidationError("path must not be empty or padded")
    if raw.startswith(("\\\\", "//", "\\?\\", "\\.\\", "\\??\\")):
        raise PathValidationError("UNC/device/NT paths require a separate gate")
    if not re.match(r"^[A-Za-z]:[\\/]", raw):
        raise PathValidationError("path must be drive-qualified and absolute")
    if any(char in raw for char in ("*", "?", "%", "\x00")):
        raise PathValidationError("path expansion/globs are forbidden")
    if ":" in raw[2:]:
        raise PathValidationError("alternate data streams are forbidden")
    if _SHORT_NAME.search(raw):
        raise PathValidationError("8.3 short-name paths are forbidden")
    if any(part.endswith((".", " ")) for part in re.split(r"[\\/]", raw)):
        raise PathValidationError("trailing-dot/space path components are forbidden")


def capture_path_identity(raw: str, allowed_roots: tuple[Path, ...]) -> PathIdentity:
    if os.name != "nt":
        raise PathValidationError("Windows path identity is only supported on Windows")
    _reject_ambiguous(raw)
    requested = Path(raw)
    missing: list[str] = []
    existing = requested
    while not existing.exists():
        if existing.parent == existing:
            raise PathValidationError("path has no existing ancestor")
        missing.insert(0, existing.name)
        existing = existing.parent
    canonical_existing = existing.resolve(strict=True)
    canonical = canonical_existing.joinpath(*missing)
    if not any(canonical.is_relative_to(root.resolve(strict=True)) for root in allowed_roots):
        raise PathValidationError("path is outside the project allowlist")
    identity = _identity_for_existing(canonical_existing)
    return PathIdentity(
        requested,
        canonical,
        identity[0],
        identity[1],
        identity[2],
        identity[3],
        identity[4],
        canonical_existing,
        tuple(missing),
    )


def _identity_for_existing(path: Path) -> tuple[str, int, str, int, str]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(
        str(path),
        0x80,
        0x1 | 0x2 | 0x4,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    if handle in (0, -1):
        raise PathValidationError(f"CreateFileW failed: {ctypes.get_last_error()}")
    try:
        size = kernel32.GetFinalPathNameByHandleW(handle, None, 0, 0)
        if not size:
            raise PathValidationError("GetFinalPathNameByHandleW failed")
        buffer = ctypes.create_unicode_buffer(size + 1)
        kernel32.GetFinalPathNameByHandleW(handle, buffer, size + 1, 0)
        info = _BY_HANDLE_FILE_INFORMATION()
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise PathValidationError("GetFileInformationByHandle failed")
        file_index = f"{info.nFileIndexHigh:08x}{info.nFileIndexLow:08x}"
        attributes = kernel32.GetFileAttributesW(str(path))
        reparse_tag = 0
        if attributes != 0xFFFFFFFF and attributes & 0x400:
            reparse_tag = attributes
        security_needed = wintypes.DWORD(0)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi32.GetFileSecurityW(str(path), 0x00000007, None, 0, ctypes.byref(security_needed))
        security = ctypes.create_string_buffer(security_needed.value)
        if not advapi32.GetFileSecurityW(
            str(path), 0x00000007, security, security_needed.value, ctypes.byref(security_needed)
        ):
            raise PathValidationError("GetFileSecurityW failed")
        security_hash = sha256(security.raw[: security_needed.value]).hexdigest()
        return (
            buffer.value,
            int(info.dwVolumeSerialNumber),
            file_index,
            reparse_tag,
            security_hash,
        )
    finally:
        kernel32.CloseHandle(handle)


def revalidate(identity: PathIdentity, allowed_roots: tuple[Path, ...]) -> PathIdentity:
    current = capture_path_identity(str(identity.canonical_path), allowed_roots)
    pinned = (
        identity.final_path,
        identity.volume_serial,
        identity.file_index,
        identity.reparse_tag,
        identity.security_hash,
        identity.nearest_existing_parent,
        identity.missing_suffix,
    )
    observed = (
        current.final_path,
        current.volume_serial,
        current.file_index,
        current.reparse_tag,
        current.security_hash,
        current.nearest_existing_parent,
        current.missing_suffix,
    )
    if observed != pinned:
        raise PathValidationError("path identity changed after approval")
    return current
