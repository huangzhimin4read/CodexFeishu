"""Windows Job Object wrapper that kills all attached workers on close."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


class JobObjectError(RuntimeError):
    pass


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class KillOnCloseJob:
    KILL_ON_JOB_CLOSE = 0x00002000
    EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self, name: str | None = None) -> None:
        if os.name != "nt":
            raise JobObjectError("Job Objects require Windows")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self.handle = self.kernel32.CreateJobObjectW(None, name)
        if not self.handle:
            raise JobObjectError(f"CreateJobObjectW failed: {ctypes.get_last_error()}")
        limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = self.KILL_ON_JOB_CLOSE
        ok = self.kernel32.SetInformationJobObject(
            self.handle,
            self.EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        )
        if not ok:
            self.close()
            raise JobObjectError(f"SetInformationJobObject failed: {ctypes.get_last_error()}")

    def assign(self, process_handle: int) -> None:
        if not self.kernel32.AssignProcessToJobObject(self.handle, process_handle):
            raise JobObjectError(f"AssignProcessToJobObject failed: {ctypes.get_last_error()}")

    def close(self) -> None:
        if getattr(self, "handle", None):
            self.kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self) -> "KillOnCloseJob":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
