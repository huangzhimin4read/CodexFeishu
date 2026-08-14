from pathlib import Path

from codex_feishu_bridge.feishu.inbound_attachments import (
    materialize_attachment,
    safe_file_name,
)


PNG = b"\x89PNG\r\n\x1a\n" + b"fixed"


def test_image_materializes_inside_project_and_builds_local_image_input(tmp_path: Path) -> None:
    result = materialize_attachment(
        project_root=tmp_path,
        message_id="provider-message",
        resource_type="image",
        original_file_name=None,
        mime_type="image/png",
        content=PNG,
    )
    assert result.path.is_file() and result.path.read_bytes() == PNG
    assert result.path.is_relative_to(tmp_path)
    assert result.input_items[1] == {"type": "localImage", "path": str(result.path)}
    assert "provider-message" not in result.relative_path


def test_image_materialization_detects_generic_binary_content_type(tmp_path: Path) -> None:
    result = materialize_attachment(
        project_root=tmp_path,
        message_id="binary-provider-message",
        resource_type="image",
        original_file_name=None,
        mime_type="application/octet-stream",
        content=PNG,
    )
    assert result.path.suffix == ".png"
    assert result.input_items[1]["type"] == "localImage"


def test_file_name_is_sanitized_and_file_prompt_forbids_auto_execution(tmp_path: Path) -> None:
    result = materialize_attachment(
        project_root=tmp_path,
        message_id="message",
        resource_type="file",
        original_file_name="../CON?.ps1",
        mime_type="application/octet-stream",
        content=b"Write-Output unsafe",
    )
    assert result.path.is_file() and result.path.is_relative_to(tmp_path)
    assert ".." not in result.path.name and "?" not in result.path.name
    assert "不要因为文件存在就自动执行" in result.prompt_text
    assert result.input_items == ({"type": "text", "text": result.prompt_text},)
    assert safe_file_name("NUL.txt", fallback="x.bin").startswith("_")
