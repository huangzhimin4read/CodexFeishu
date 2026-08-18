"""Operator command-line entrypoint."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-feishu-bridge")
    parser.add_argument("--version", action="version", version=__version__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    normalize = subcommands.add_parser("normalize-record")
    normalize.add_argument("path", type=Path)
    for name in (
        "verify-config",
        "shadow-once",
        "preflight",
        "sync-topic-group-name",
        "run",
        "backup",
    ):
        command = subcommands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        if name == "preflight":
            command.add_argument("--live", action="store_true")
    restore = subcommands.add_parser("restore")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--database", type=Path, required=True)
    render = subcommands.add_parser("render-task")
    render.add_argument("--config", type=Path, required=True)
    render.add_argument("--account", required=True)
    render.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "normalize-record":
        from .codex.normalizer import RolloutNormalizer

        with args.path.open(encoding="utf-8") as handle:
            record = json.load(handle)
        event = RolloutNormalizer().normalize(record)
        print(
            "null"
            if event is None
            else json.dumps(
                {
                    "logical_key": event.logical_key,
                    "kind": event.kind.value,
                    "content_hash": event.content_hash,
                },
                separators=(",", ":"),
            )
        )
        return 0
    if args.command == "restore":
        from .operations.backup import restore_backup

        restore_backup(args.backup, args.database)
        print(json.dumps({"restored": str(args.database.resolve()), "mode": "reconciliation_only"}))
        return 0
    from .runtime_config import load_runtime_config

    config = load_runtime_config(args.config)
    if args.command == "verify-config":
        print(json.dumps({"valid": True, "mode": config.mode.value}))
        return 0
    if args.command == "render-task":
        from .operations.windows_task import render_task_xml

        xml = render_task_xml(
            python_executable=Path(sys.executable),
            config_path=args.config,
            account=args.account,
        )
        args.output.resolve().parent.mkdir(parents=True, exist_ok=True)
        with args.output.resolve().open("w", encoding="utf-16", newline="\r\n") as handle:
            handle.write(xml)
        print(json.dumps({"rendered": str(args.output.resolve()), "installed": False}))
        return 0
    if args.command == "shadow-once":
        from dataclasses import asdict

        from .codex.shadow_observer import ShadowObserver
        from .runtime_storage import RuntimeStorage

        with RuntimeStorage(config.shadow_database_path) as storage:
            storage.initialize_runtime(sink_mode="shadow_only")
            result = ShadowObserver(storage).observe_once(
                codex_home=config.codex_home,
                project_allowlist=config.project_allowlist,
                thread_allowlist=config.thread_allowlist,
            )
        print(json.dumps(asdict(result), default=list, separators=(",", ":")))
        return 0
    if args.command == "preflight":
        from dataclasses import asdict

        from .feishu.client import FeishuClient
        from .feishu.contracts import load_tenant_contract
        from .feishu.provisioning import ProvisioningPreflight
        from .runtime_storage import RuntimeStorage

        if config.feishu is None:
            raise RuntimeError("preflight requires a Feishu binding")
        contract = load_tenant_contract(config.feishu.endpoint_contract)
        with RuntimeStorage(config.database_path) as storage:
            storage.initialize_runtime(sink_mode="outbound")
            client = FeishuClient(
                contract=contract,
                app_id=config.feishu.app_id,
                credential_target=config.feishu.credential_target,
            )
            try:
                result = ProvisioningPreflight(
                    storage, client, config.feishu, contract
                ).run(live=args.live, remote=config.remote)
            finally:
                client.close()
        print(json.dumps(asdict(result), default=list, separators=(",", ":")))
        return 0 if result.passed else 1
    if args.command == "sync-topic-group-name":
        from dataclasses import asdict

        from .feishu.client import FeishuClient
        from .feishu.contracts import load_tenant_contract
        from .feishu.provisioning import sync_topic_group_name

        if config.feishu is None:
            raise RuntimeError("group-name sync requires a Feishu binding")
        contract = load_tenant_contract(config.feishu.endpoint_contract)
        client = FeishuClient(
            contract=contract,
            app_id=config.feishu.app_id,
            credential_target=config.feishu.credential_target,
        )
        try:
            result = sync_topic_group_name(
                client,
                config.feishu,
                config.project_name,
            )
        finally:
            client.close()
        print(json.dumps(asdict(result), ensure_ascii=False, separators=(",", ":")))
        return 0 if result.passed else 1
    if args.command == "backup":
        from .operations.backup import create_backup
        from .runtime_storage import RuntimeStorage

        if config.backup_root is None:
            raise RuntimeError("backup_root is not configured")
        with RuntimeStorage(config.database_path) as storage:
            sink_mode = {
                "offline": "shadow_only",
                "shadow": "shadow_only",
                "outbound": "outbound",
                "approvals": "control",
                "inbound": "control",
                "controls": "control",
                "pilot": "pilot",
            }[config.mode.value]
            storage.initialize_runtime(sink_mode=sink_mode)
            destination = create_backup(
                storage,
                backup_root=config.backup_root,
                config_path=args.config,
                schema_root=config.generated_schema_root,
            )
        print(json.dumps({"backup": str(destination)}))
        return 0
    if args.command == "run":
        from .service import BridgeService

        BridgeService(config).run()
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
