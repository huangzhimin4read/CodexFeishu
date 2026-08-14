"""Pinned executable/schema compatibility update gate."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path


class UpdateGateError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    value = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest().upper()


@dataclass(frozen=True, slots=True)
class UpdateGate:
    expected_version: str
    expected_executable_sha256: str
    expected_stable_schema_sha256: str
    expected_experimental_schema_sha256: str

    @classmethod
    def load(cls, baseline: Path) -> "UpdateGate":
        with baseline.resolve().open(encoding="utf-8") as handle:
            raw = json.load(handle)
        return cls(
            expected_version=str(raw["codexVersionOutput"]),
            expected_executable_sha256=str(raw["codexExecutableSha256"]).upper(),
            expected_stable_schema_sha256=str(raw["stableProtocolSchemaSha256"]).upper(),
            expected_experimental_schema_sha256=str(raw["experimentalProtocolSchemaSha256"]).upper(),
        )

    def require_executable(self, executable: Path) -> None:
        actual = sha256_file(executable.resolve())
        if actual != self.expected_executable_sha256:
            raise UpdateGateError("Codex executable changed; regenerate both schema sets before ingress")

    def require_schemas(self, schema_root: Path) -> None:
        stable = sha256_file(
            schema_root.resolve() / "stable" / "codex_app_server_protocol.schemas.json"
        )
        experimental = sha256_file(
            schema_root.resolve() / "experimental" / "codex_app_server_protocol.schemas.json"
        )
        if stable != self.expected_stable_schema_sha256 or experimental != self.expected_experimental_schema_sha256:
            raise UpdateGateError("generated App Server schemas changed; compatibility Gate is required")
