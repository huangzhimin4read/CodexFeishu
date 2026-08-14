import json
import struct
import subprocess

import pytest

from codex_feishu_bridge.security.jcs import (
    CanonicalizationError,
    canonicalize_text,
    digest,
)


def test_rfc8785_section_3_2_2_vector() -> None:
    source = {
        "numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 1e-27],
        "string": "€$\u000f\nA'B\"\\\"/",
        "literals": [None, True, False],
    }
    expected = (
        '{"literals":[null,true,false],'
        '"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
        '"string":"€$\\u000f\\nA\'B\\\"\\\\\\\"/"}'
    )
    assert canonicalize_text(source) == expected


def test_utf16_property_sorting_and_hash_stability() -> None:
    source_a = {"😀": 1, "\ue000": 2, "a": {"z": 0, "b": 1}}
    source_b = {"a": {"b": 1, "z": 0}, "\ue000": 2, "😀": 1}
    canonical = canonicalize_text(source_a)
    assert canonical.index('"a"') < canonical.index('"😀"') < canonical.index('"\ue000"')
    assert digest(source_a) == digest(source_b)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_numbers_are_rejected(value: float) -> None:
    with pytest.raises(CanonicalizationError, match="non-finite"):
        canonicalize_text(value)


def test_unsafe_integer_and_lone_surrogate_are_rejected() -> None:
    with pytest.raises(CanonicalizationError, match="safe range"):
        canonicalize_text(9_007_199_254_740_992)
    with pytest.raises(CanonicalizationError, match="surrogate"):
        canonicalize_text("\ud800")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (-0.0, "0"),
        (1e-6, "0.000001"),
        (1e-7, "1e-7"),
        (1.0, "1"),
        (1e20, "100000000000000000000"),
        (1e21, "1e+21"),
    ],
)
def test_ecmascript_number_boundaries(value: float, expected: str) -> None:
    assert canonicalize_text(value) == expected


@pytest.mark.parametrize(
    ("bits", "expected"),
    [
        ("0000000000000000", "0"),
        ("8000000000000000", "0"),
        ("0000000000000001", "5e-324"),
        ("8000000000000001", "-5e-324"),
        ("7fefffffffffffff", "1.7976931348623157e+308"),
        ("ffefffffffffffff", "-1.7976931348623157e+308"),
        ("4340000000000000", "9007199254740992"),
        ("c340000000000000", "-9007199254740992"),
        ("4430000000000000", "295147905179352830000"),
        ("44b52d02c7e14af5", "9.999999999999997e+22"),
        ("44b52d02c7e14af6", "1e+23"),
        ("44b52d02c7e14af7", "1.0000000000000001e+23"),
        ("444b1ae4d6e2ef4e", "999999999999999700000"),
        ("444b1ae4d6e2ef4f", "999999999999999900000"),
        ("444b1ae4d6e2ef50", "1e+21"),
        ("3eb0c6f7a0b5ed8c", "9.999999999999997e-7"),
        ("3eb0c6f7a0b5ed8b", "9.999999999999995e-7"),
        ("43a33c44a1e1f000", "693029123697147900"),
    ],
)
def test_rfc8785_appendix_b_binary64_vectors(bits: str, expected: str) -> None:
    value = struct.unpack(">d", bytes.fromhex(bits))[0]
    assert canonicalize_text(value) == expected


def test_jcs_float_matches_node_ecmascript_oracle() -> None:
    values = [
        8.563216986513925e17,
        float.fromhex("0x1.72b22ed9c8d00p+63"),
        1.2345678901234567e-7,
        1.0000000000000002,
        9.999999999999999e20,
    ]
    script = "process.stdout.write(JSON.stringify(JSON.parse(process.argv[1])))"
    for value in values:
        result = subprocess.run(
            ["node", "-e", script, json.dumps(value)],
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        )
        assert canonicalize_text(value) == result.stdout
