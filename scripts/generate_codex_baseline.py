"""Regenerate and summarize the pinned Codex App Server schema baseline."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXECUTABLE = Path.home() / (
    "AppData/Roaming/npm/node_modules/@openai/codex/node_modules/"
    "@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe"
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def methods(path: Path) -> list[str]:
    result: set[str] = set()
    for alternative in load(path).get("oneOf", []):
        method = alternative.get("properties", {}).get("method", {})
        result.update(method.get("enum", []))
    return sorted(result)


def definition(path: Path, name: str) -> dict[str, Any]:
    return load(path)["definitions"][name]


def contains_property(value: Any, property_name: str) -> bool:
    if isinstance(value, dict):
        properties = value.get("properties")
        if isinstance(properties, dict) and property_name in properties:
            return True
        return any(contains_property(child, property_name) for child in value.values())
    if isinstance(value, list):
        return any(contains_property(child, property_name) for child in value)
    return False


def request_field_evidence(
    stable: Path,
    experimental: Path,
    params_file: str,
    field: str,
) -> dict[str, Any]:
    stable_path = stable / params_file
    experimental_path = experimental / params_file
    return {
        "field": field,
        "stableSchemaPath": params_file,
        "stableSchemaSha256": sha256_file(stable_path),
        "stable": contains_property(load(stable_path), field),
        "experimentalSchemaPath": params_file,
        "experimentalSchemaSha256": sha256_file(experimental_path),
        "experimental": contains_property(load(experimental_path), field),
    }


def sandbox_variants(schema_root: Path) -> dict[str, dict[str, Any]]:
    sandbox = definition(schema_root / "v2" / "TurnStartParams.json", "SandboxPolicy")
    variants: dict[str, dict[str, Any]] = {}
    for alternative in sandbox["oneOf"]:
        properties = alternative["properties"]
        policy_type = properties["type"]["enum"][0]
        variants[policy_type] = {
            "required": sorted(alternative.get("required", [])),
            "properties": sorted(properties),
            "additionalProperties": alternative.get("additionalProperties", True),
            "networkAccess": properties.get("networkAccess"),
        }
    return variants


def build_matrix(stable: Path, experimental: Path, version: str) -> dict[str, Any]:
    stable_turn = load(stable / "v2" / "TurnStartParams.json")
    experimental_turn = load(experimental / "v2" / "TurnStartParams.json")
    stable_methods = {
        name: methods(stable / name)
        for name in (
            "ClientRequest.json",
            "ClientNotification.json",
            "ServerRequest.json",
            "ServerNotification.json",
        )
    }
    experimental_methods = {
        name: methods(experimental / name) for name in stable_methods
    }
    return {
        "codexVersion": version,
        "stableProtocolSchemaSha256": sha256_file(
            stable / "codex_app_server_protocol.schemas.json"
        ),
        "experimentalProtocolSchemaSha256": sha256_file(
            experimental / "codex_app_server_protocol.schemas.json"
        ),
        "methods": {
            name.removesuffix(".json"): {
                "stable": stable_methods[name],
                "experimentalOnly": sorted(
                    set(experimental_methods[name]) - set(stable_methods[name])
                ),
            }
            for name in stable_methods
        },
        "initializeCapabilities": {
            "stable": sorted(
                definition(
                    stable / "v1" / "InitializeParams.json", "InitializeCapabilities"
                )["properties"]
            ),
            "experimental": sorted(
                definition(
                    experimental / "v1" / "InitializeParams.json",
                    "InitializeCapabilities",
                )["properties"]
            ),
        },
        "approvalPolicy": definition(
            stable / "v2" / "TurnStartParams.json", "AskForApproval"
        ),
        "approvalsReviewer": definition(
            stable / "v2" / "TurnStartParams.json", "ApprovalsReviewer"
        ),
        "sandboxPolicy": {
            "stable": sandbox_variants(stable),
            "experimental": sandbox_variants(experimental),
        },
        "turnStartFields": {
            "stable": sorted(stable_turn["properties"]),
            "experimentalOnly": sorted(
                set(experimental_turn["properties"]) - set(stable_turn["properties"])
            ),
            "permissionsDescription": experimental_turn["properties"]
            .get("permissions", {})
            .get("description"),
        },
        "managedRequirementFields": sorted(
            definition(
                stable / "v2" / "ConfigRequirementsReadResponse.json",
                "ConfigRequirements",
            )["properties"]
        ),
        "selectedRequestFieldClassification": {
            "item/tool/requestUserInput": {
                "methodStable": "item/tool/requestUserInput"
                in stable_methods["ServerRequest.json"],
            },
            "item/commandExecution/requestApproval": {
                "availableDecisions": request_field_evidence(
                    stable,
                    experimental,
                    "CommandExecutionRequestApprovalParams.json",
                    "availableDecisions",
                ),
                "additionalPermissions": request_field_evidence(
                    stable,
                    experimental,
                    "CommandExecutionRequestApprovalParams.json",
                    "additionalPermissions",
                ),
            },
        },
    }


def replace_tree(source: Path, target: Path) -> None:
    allowed_root = (ROOT / "generated" / "codex").resolve()
    resolved = target.resolve()
    if not resolved.is_relative_to(allowed_root) or target.name not in {
        "stable",
        "experimental",
    }:
        raise RuntimeError(f"refusing to replace unexpected schema target: {target}")
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-executable", type=Path, default=DEFAULT_EXECUTABLE)
    args = parser.parse_args()
    executable = args.codex_executable.resolve()
    if not executable.is_file():
        raise SystemExit(f"Codex executable not found: {executable}")
    version_output = subprocess.check_output(
        [str(executable), "--version"], text=True, encoding="utf-8"
    ).strip()
    version = version_output.rsplit(" ", 1)[-1]
    version_root = ROOT / "generated" / "codex" / version
    version_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="codex-schema-") as raw_temp:
        temp = Path(raw_temp)
        stable_temp = temp / "stable"
        experimental_temp = temp / "experimental"
        subprocess.run(
            [
                str(executable),
                "app-server",
                "generate-json-schema",
                "--out",
                str(stable_temp),
            ],
            check=True,
        )
        subprocess.run(
            [
                str(executable),
                "app-server",
                "generate-json-schema",
                "--experimental",
                "--out",
                str(experimental_temp),
            ],
            check=True,
        )
        replace_tree(stable_temp, version_root / "stable")
        replace_tree(experimental_temp, version_root / "experimental")

    stable = version_root / "stable"
    experimental = version_root / "experimental"
    matrix = build_matrix(stable, experimental, version)
    (version_root / "compatibility-matrix.json").write_text(
        json.dumps(matrix, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "codexVersionOutput": version_output,
        "codexExecutable": str(executable),
        "codexExecutableSha256": sha256_file(executable),
        "stableProtocolSchemaSha256": matrix["stableProtocolSchemaSha256"],
        "experimentalProtocolSchemaSha256": matrix[
            "experimentalProtocolSchemaSha256"
        ],
        "generationCommands": [
            "codex.exe app-server generate-json-schema --out <stable>",
            "codex.exe app-server generate-json-schema --experimental --out <experimental>",
        ],
    }
    (version_root / "baseline.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    hash_lines = []
    for directory in (stable, experimental):
        for path in sorted(directory.rglob("*.json")):
            relative = path.relative_to(version_root).as_posix()
            hash_lines.append(f"{sha256_file(path)}  {relative}")
    (version_root / "schema-files.sha256").write_text(
        "\n".join(hash_lines) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
