from pathlib import Path

import pytest

from codex_feishu_bridge.codex.compatibility import (
    CompatibilityError,
    CompatibilityMatrix,
)


ROOT = Path(__file__).parents[1]
MATRIX = ROOT / "generated" / "codex" / "0.145.0" / "compatibility-matrix.json"


def test_stable_user_input_and_experimental_process_are_distinct() -> None:
    matrix = CompatibilityMatrix.load(MATRIX)
    assert matrix.classify_method("ServerRequest", "item/tool/requestUserInput") == "stable"
    assert matrix.classify_method("ClientRequest", "process/spawn") == "experimental"
    matrix.require_method("ServerRequest", "item/tool/requestUserInput")
    with pytest.raises(CompatibilityError, match="experimentalApi"):
        matrix.require_method("ClientRequest", "process/spawn")
    matrix.require_method("ClientRequest", "process/spawn", experimental_api=True)


def test_unknown_method_fails_closed() -> None:
    matrix = CompatibilityMatrix.load(MATRIX)
    with pytest.raises(CompatibilityError, match="unsupported"):
        matrix.require_method("ServerRequest", "item/unknown")
