"""Secret retrieval through Windows Credential Manager."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass


class CredentialError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AppCredential:
    app_secret: str

    def __repr__(self) -> str:
        return "AppCredential(app_secret=<redacted>)"


if os.name == "nt":
    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", wintypes.LPVOID),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]


class WindowsCredentialManager:
    GENERIC = 1

    def read(self, target: str) -> AppCredential:
        if os.name != "nt":
            raise CredentialError("Windows Credential Manager is unavailable")
        credential = ctypes.POINTER(_CREDENTIALW)()
        advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        advapi32.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
        ]
        advapi32.CredReadW.restype = wintypes.BOOL
        advapi32.CredFree.argtypes = [wintypes.LPVOID]
        if not advapi32.CredReadW(target, self.GENERIC, 0, ctypes.byref(credential)):
            raise CredentialError(f"credential target is unavailable (winerror={ctypes.get_last_error()})")
        try:
            value = credential.contents
            blob = ctypes.string_at(value.CredentialBlob, value.CredentialBlobSize)
            try:
                secret = blob.decode("utf-16-le")
            except UnicodeDecodeError:
                secret = blob.decode("utf-8")
            if not secret:
                raise CredentialError("credential is empty")
            return AppCredential(secret)
        finally:
            advapi32.CredFree(credential)
