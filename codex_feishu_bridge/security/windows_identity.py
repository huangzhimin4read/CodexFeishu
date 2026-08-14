"""Windows principal separation preflight."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass


class IdentityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PrincipalIdentity:
    account_name: str
    sid: str


def current_principal() -> PrincipalIdentity:
    if os.name != "nt":
        raise IdentityError("Windows identity inspection is unavailable")
    size = wintypes.DWORD(0)
    ctypes.windll.advapi32.GetUserNameW(None, ctypes.byref(size))
    buffer = ctypes.create_unicode_buffer(size.value)
    if not ctypes.windll.advapi32.GetUserNameW(buffer, ctypes.byref(size)):
        raise IdentityError(f"GetUserNameW failed: {ctypes.get_last_error()}")
    # Resolve the account SID through LookupAccountNameW and stringify it.
    sid_size = wintypes.DWORD(0)
    domain_size = wintypes.DWORD(0)
    sid_type = wintypes.DWORD(0)
    ctypes.windll.advapi32.LookupAccountNameW(
        None, buffer.value, None, ctypes.byref(sid_size), None, ctypes.byref(domain_size), ctypes.byref(sid_type)
    )
    sid_buffer = ctypes.create_string_buffer(sid_size.value)
    domain_buffer = ctypes.create_unicode_buffer(domain_size.value)
    if not ctypes.windll.advapi32.LookupAccountNameW(
        None,
        buffer.value,
        sid_buffer,
        ctypes.byref(sid_size),
        domain_buffer,
        ctypes.byref(domain_size),
        ctypes.byref(sid_type),
    ):
        raise IdentityError(f"LookupAccountNameW failed: {ctypes.get_last_error()}")
    string_sid = wintypes.LPWSTR()
    if not ctypes.windll.advapi32.ConvertSidToStringSidW(sid_buffer, ctypes.byref(string_sid)):
        raise IdentityError(f"ConvertSidToStringSidW failed: {ctypes.get_last_error()}")
    try:
        return PrincipalIdentity(buffer.value, string_sid.value)
    finally:
        ctypes.windll.kernel32.LocalFree(string_sid)


def require_distinct_principals(broker_sid: str, worker_sid: str) -> None:
    if not broker_sid or not worker_sid or broker_sid.casefold() == worker_sid.casefold():
        raise IdentityError("broker and full-access worker must use distinct Windows principals")


def current_token_is_administrator() -> bool:
    """Return effective token membership, respecting a filtered UAC token."""
    if os.name != "nt":
        raise IdentityError("Windows token inspection is unavailable")
    return bool(ctypes.windll.shell32.IsUserAnAdmin())
