"""Per-owner Windows mutex with an explicit DACL."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from .windows_identity import current_principal


class SingleInstanceError(RuntimeError):
    pass


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


class PrivateMutex:
    ERROR_ALREADY_EXISTS = 183

    def __init__(self, name: str) -> None:
        if os.name != "nt":
            raise SingleInstanceError("private mutex requires Windows")
        if not name or any(char in name for char in "\\/:"):
            raise SingleInstanceError("invalid private mutex name")
        sid = current_principal().sid
        sddl = f"D:P(A;;GA;;;SY)(A;;GA;;;{sid})"
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        descriptor = wintypes.LPVOID()
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None
        ):
            raise SingleInstanceError("failed to build mutex security descriptor")
        attributes = _SECURITY_ATTRIBUTES(
            ctypes.sizeof(_SECURITY_ATTRIBUTES), descriptor, False
        )
        try:
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            self.handle = kernel32.CreateMutexW(
                ctypes.byref(attributes), False, "Local\\CodexFeishu-" + name
            )
            error = ctypes.get_last_error()
        finally:
            kernel32.LocalFree(descriptor)
        if not self.handle:
            raise SingleInstanceError(f"CreateMutexW failed: {error}")
        if error == self.ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(self.handle)
            self.handle = None
            raise SingleInstanceError("another service instance owns the private mutex")
        self.kernel32 = kernel32

    def close(self) -> None:
        if getattr(self, "handle", None):
            self.kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self) -> "PrivateMutex":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
