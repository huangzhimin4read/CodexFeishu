from pathlib import Path

import pytest

from codex_feishu_bridge.security.dpapi import protect_current_user, unprotect_current_user
from codex_feishu_bridge.security.single_instance import PrivateMutex, SingleInstanceError
from codex_feishu_bridge.security.windows_paths import (
    PathValidationError,
    capture_path_identity,
    revalidate,
)


def test_dpapi_current_user_round_trip() -> None:
    protected = protect_current_user(b"x" * 32)
    assert protected != b"x" * 32
    assert unprotect_current_user(protected) == b"x" * 32


def test_private_mutex_rejects_second_owner() -> None:
    with PrivateMutex("test-exclusive"):
        with pytest.raises(SingleInstanceError, match="another service"):
            PrivateMutex("test-exclusive")


def test_windows_path_identity_and_ambiguous_rejections(tmp_path: Path) -> None:
    identity = capture_path_identity(str(tmp_path), (tmp_path,))
    assert identity.canonical_path == tmp_path.resolve()
    assert revalidate(identity, (tmp_path,)).file_index == identity.file_index
    for value in (
        "C:relative",
        "\\\\server\\share",
        "\\\\?\\C:\\device",
        "C:\\file:stream",
        "C:\\PROGRA~1",
        "C:\\tail. ",
        "C:\\%TEMP%",
    ):
        with pytest.raises(PathValidationError):
            capture_path_identity(value, (tmp_path,))
