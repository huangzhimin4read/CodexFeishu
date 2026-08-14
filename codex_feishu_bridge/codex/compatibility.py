"""Read-only access to the generated App Server compatibility matrix."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CompatibilityError(RuntimeError):
    """A method or field is unavailable under the negotiated capability set."""


@dataclass(frozen=True, slots=True)
class CompatibilityMatrix:
    data: dict[str, Any]

    @classmethod
    def load(cls, path: Path) -> CompatibilityMatrix:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict) or "methods" not in value:
            raise CompatibilityError("invalid compatibility matrix")
        return cls(value)

    def classify_method(self, direction: str, method: str) -> str | None:
        entry = self.data["methods"].get(direction)
        if not isinstance(entry, dict):
            raise CompatibilityError(f"unknown protocol direction: {direction}")
        if method in entry.get("stable", []):
            return "stable"
        if method in entry.get("experimentalOnly", []):
            return "experimental"
        return None

    def require_method(
        self, direction: str, method: str, *, experimental_api: bool = False
    ) -> None:
        classification = self.classify_method(direction, method)
        if classification is None:
            raise CompatibilityError(f"unsupported method: {method}")
        if classification == "experimental" and not experimental_api:
            raise CompatibilityError(f"experimentalApi is required for {method}")

    def experimental_request_fields(self, method: str) -> frozenset[str]:
        selected = self.data.get("selectedRequestFieldClassification", {})
        request = selected.get(method) if isinstance(selected, dict) else None
        if not isinstance(request, dict):
            return frozenset()
        return frozenset(
            field
            for field, classification in request.items()
            if isinstance(classification, dict)
            and classification.get("experimental") is True
            and classification.get("stable") is not True
        )
