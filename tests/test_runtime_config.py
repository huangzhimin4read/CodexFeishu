import json
from pathlib import Path

import pytest

from codex_feishu_bridge.runtime_config import (
    ConversationMode,
    RemoteDeliveryMode,
    RuntimeConfigurationError,
    RuntimeMode,
    load_runtime_config,
)


def _write_contract(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tenant_key": "tenant",
                "app_id": "app",
                "published_version": "1.0.0",
                "bot_enabled": True,
                "p2p_only": True,
                "source_export_sha256": "a" * 64,
                "endpoints": [
                    {
                        "name": "tenant_token",
                        "method": "POST",
                        "path": "/open-apis/auth/v3/tenant_access_token/internal",
                        "exact_scopes": [],
                        "administrator_approved": True,
                        "enabled": True,
                        "rate_limit": {"capacity": 1, "refill_per_second": 1},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _write_config(tmp_path: Path, executable: Path, *, mode: str = "outbound") -> Path:
    contract = tmp_path / "contract.json"
    _write_contract(contract)
    project = tmp_path / "project"
    project.mkdir()
    config = tmp_path / "runtime.toml"
    config.write_text(
        f'''schema_version=1
mode="{mode}"
[paths]
workspace_root="{tmp_path.as_posix()}"
database="{(tmp_path / 'runtime.db').as_posix()}"
shadow_database="{(tmp_path / 'shadow.db').as_posix()}"
codex_home="{(tmp_path / 'codex-home').as_posix()}"
codex_executable="{executable.as_posix()}"
schema_root="{tmp_path.as_posix()}"
[allowlist]
projects=["{project.as_posix()}"]
threads=["thread-1"]
[feishu]
tenant_key="tenant"
app_id="app"
owner_open_id="owner"
p2p_chat_id="chat"
credential_target="CodexFeishu/app"
endpoint_contract="{contract.as_posix()}"
''',
        encoding="utf-8",
    )
    return config


def test_runtime_config_is_versioned_and_has_distinct_databases(tmp_path: Path) -> None:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"fixture")
    config = load_runtime_config(_write_config(tmp_path, executable))
    assert config.mode is RuntimeMode.OUTBOUND
    assert config.database_path != config.shadow_database_path
    assert config.feishu is not None and config.feishu.credential_target == "CodexFeishu/app"
    assert config.feishu.owner_display_name == "用户"


def test_runtime_config_accepts_owner_display_name(tmp_path: Path) -> None:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"fixture")
    path = _write_config(tmp_path, executable)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'owner_open_id="owner"',
            'owner_open_id="owner"\nowner_display_name="项目所有者"',
        ),
        encoding="utf-8",
    )
    config = load_runtime_config(path)
    assert config.feishu is not None and config.feishu.owner_display_name == "项目所有者"


def test_runtime_config_rejects_unknown_and_plaintext_secret_fields(tmp_path: Path) -> None:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"fixture")
    path = _write_config(tmp_path, executable)
    path.write_text(path.read_text(encoding="utf-8") + '\napp_secret="forbidden"\n', encoding="utf-8")
    with pytest.raises(RuntimeConfigurationError, match="unknown .*fields"):
        load_runtime_config(path)


def test_runtime_config_supports_parallel_topic_group_surface(tmp_path: Path) -> None:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"fixture")
    path = _write_config(tmp_path, executable)
    text = path.read_text(encoding="utf-8").replace(
        'p2p_chat_id="chat"',
        'p2p_chat_id="chat"\nconversation_mode="topic_group"\ntopic_chat_id="topic-chat"',
    )
    path.write_text(text, encoding="utf-8")
    config = load_runtime_config(path)
    assert config.feishu is not None
    assert config.feishu.conversation_mode is ConversationMode.TOPIC_GROUP
    assert config.feishu.target_chat_id == "topic-chat"
    assert config.feishu.target_chat_type == "group"
    assert config.feishu.reply_in_thread


def test_runtime_config_accepts_explicit_project_display_name(tmp_path: Path) -> None:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"fixture")
    path = _write_config(tmp_path, executable)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'mode="outbound"', 'mode="outbound"\nproject_name="CODEX飞书接口"'
        ),
        encoding="utf-8",
    )
    assert load_runtime_config(path).project_name == "CODEX飞书接口"


def test_topic_group_config_requires_a_separate_target_chat(tmp_path: Path) -> None:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"fixture")
    path = _write_config(tmp_path, executable)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'p2p_chat_id="chat"', 'p2p_chat_id="chat"\nconversation_mode="topic_group"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeConfigurationError, match="topic_chat_id"):
        load_runtime_config(path)


def test_activity_triggered_projects_require_outbound_topic_group(tmp_path: Path) -> None:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"fixture")
    path = _write_config(tmp_path, executable)
    text = path.read_text(encoding="utf-8").replace(
        'p2p_chat_id="chat"',
        'p2p_chat_id="chat"\nconversation_mode="topic_group"\ntopic_chat_id="topic-chat"',
    )
    text += (
        '\n[codex_projects]\nenabled=true\nactivity_after_ms=1234567890\n'
        'primary_project_id="project-1"\n'
    )
    path.write_text(text, encoding="utf-8")
    config = load_runtime_config(path)
    assert config.codex_projects is not None and config.codex_projects.enabled
    assert config.codex_projects.primary_project_id == "project-1"

    path.write_text(text.replace('mode="outbound"', 'mode="approvals"'), encoding="utf-8")
    with pytest.raises(RuntimeConfigurationError, match="restricted to outbound"):
        load_runtime_config(path)


def test_full_remote_capabilities_require_topic_project_authority(tmp_path: Path) -> None:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"fixture")
    path = _write_config(tmp_path, executable)
    text = path.read_text(encoding="utf-8").replace(
        'p2p_chat_id="chat"',
        'p2p_chat_id="chat"\nconversation_mode="topic_group"\ntopic_chat_id="topic-chat"',
    )
    text += (
        '\n[codex_projects]\nenabled=true\nactivity_after_ms=1234567890\n'
        'primary_project_id="project-1"\n'
        '\n[worker_isolation]\nworker_sid="S-1-5-21-1-2-3-1001"\n'
        'scheduled_task_name="CodexFeishu-Worker-Test"\n'
        'launch_file=".runtime/isolated-worker-launch.json"\n'
        'worker_codex_home="../worker-codex-home"\n'
        '\n[remote]\nenabled=true\ntext=true\nimages=true\nfiles=true\n'
        'approvals=true\ncontrols=true\nmax_image_bytes=10485760\nmax_file_bytes=26214400\n'
    )
    path.write_text(text, encoding="utf-8")
    config = load_runtime_config(path)
    assert config.remote.needs_control_plane
    assert config.remote.receives_messages

    path.write_text(text.replace("approvals=true", "approvals=true\nauto_approve=true"), encoding="utf-8")
    assert load_runtime_config(path).remote.auto_approve

    path.write_text(
        text.replace("approvals=true", "approvals=false\nauto_approve=true"),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeConfigurationError, match="auto_approve requires"):
        load_runtime_config(path)

    path.write_text(text.replace("files=true", "files=false").replace("images=true", "images=false")
                    .replace("text=true", "text=false").replace("approvals=true", "approvals=false")
                    .replace("controls=true", "controls=false"), encoding="utf-8")
    with pytest.raises(RuntimeConfigurationError, match="remote.enabled"):
        load_runtime_config(path)


def test_remote_capabilities_require_isolated_worker(tmp_path: Path) -> None:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"fixture")
    path = _write_config(tmp_path, executable)
    text = path.read_text(encoding="utf-8").replace(
        'p2p_chat_id="chat"',
        'p2p_chat_id="chat"\nconversation_mode="topic_group"\ntopic_chat_id="topic-chat"',
    )
    text += (
        '\n[codex_projects]\nenabled=true\nactivity_after_ms=1234567890\n'
        'primary_project_id="project-1"\n'
        '\n[remote]\nenabled=true\ntext=true\n'
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(RuntimeConfigurationError, match="separately-principalled"):
        load_runtime_config(path)


def test_desktop_remote_delivery_uses_the_codex_app_writer_without_worker(tmp_path: Path) -> None:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"fixture")
    path = _write_config(tmp_path, executable)
    text = path.read_text(encoding="utf-8").replace(
        'p2p_chat_id="chat"',
        'p2p_chat_id="chat"\nconversation_mode="topic_group"\ntopic_chat_id="topic-chat"',
    )
    text += (
        '\n[codex_projects]\nenabled=true\nactivity_after_ms=1234567890\n'
        'primary_project_id="project-1"\n'
        '\n[remote]\nenabled=true\ntext=true\nimages=true\nfiles=true\n'
        'approvals=true\ncontrols=true\nauto_approve=true\ndelivery="desktop"\n'
    )
    path.write_text(text, encoding="utf-8")
    config = load_runtime_config(path)
    assert config.remote.delivery is RemoteDeliveryMode.DESKTOP
    assert config.remote.uses_desktop
    assert config.worker_isolation is None


def test_desktop_relay_delivery_requires_one_private_relay_task(tmp_path: Path) -> None:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"fixture")
    path = _write_config(tmp_path, executable)
    text = path.read_text(encoding="utf-8").replace(
        'p2p_chat_id="chat"',
        'p2p_chat_id="chat"\nconversation_mode="topic_group"\ntopic_chat_id="topic-chat"',
    )
    text += (
        '\n[codex_projects]\nenabled=true\nactivity_after_ms=1234567890\n'
        'primary_project_id="project-1"\n'
        '\n[remote]\nenabled=true\ntext=true\nimages=true\nfiles=true\n'
        'approvals=true\ncontrols=true\nauto_approve=true\ndelivery="desktop_relay"\n'
        'desktop_relay_thread_id="01a00efd-3472-70c2-8a71-fb86b7d8a85c"\n'
        'desktop_relay_thread_title="飞书桥接专用中转"\n'
    )
    path.write_text(text, encoding="utf-8")
    config = load_runtime_config(path)
    assert config.remote.delivery is RemoteDeliveryMode.DESKTOP_RELAY
    assert config.remote.uses_desktop_relay
    assert config.remote.desktop_relay_thread_title == "飞书桥接专用中转"
    assert config.remote.uses_host_writer
    assert config.internal_thread_ids == frozenset(
        {"01a00efd-3472-70c2-8a71-fb86b7d8a85c"}
    )
    assert config.worker_isolation is None

    path.write_text(
        text.replace(
            'desktop_relay_thread_id="01a00efd-3472-70c2-8a71-fb86b7d8a85c"\n',
            "",
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeConfigurationError, match="desktop_relay_thread_id"):
        load_runtime_config(path)

    path.write_text(
        text.replace('desktop_relay_thread_title="飞书桥接专用中转"\n', ""),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeConfigurationError, match="desktop_relay_thread_title"):
        load_runtime_config(path)


def test_cli_remote_delivery_uses_persisted_codex_writer_without_worker(tmp_path: Path) -> None:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"fixture")
    path = _write_config(tmp_path, executable)
    text = path.read_text(encoding="utf-8").replace(
        'p2p_chat_id="chat"',
        'p2p_chat_id="chat"\nconversation_mode="topic_group"\ntopic_chat_id="topic-chat"',
    )
    text += (
        '\n[codex_projects]\nenabled=true\nactivity_after_ms=1234567890\n'
        'primary_project_id="project-1"\n'
        '\n[remote]\nenabled=true\ntext=true\nimages=true\nfiles=true\n'
        'approvals=true\ncontrols=true\nauto_approve=true\ndelivery="cli"\n'
    )
    path.write_text(text, encoding="utf-8")
    config = load_runtime_config(path)
    assert config.remote.delivery is RemoteDeliveryMode.CLI
    assert config.remote.uses_cli
    assert config.remote.uses_host_writer
    assert not config.remote.uses_desktop
    assert config.worker_isolation is None
