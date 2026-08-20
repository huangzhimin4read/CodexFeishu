from pathlib import Path

from codex_feishu_bridge.feishu.files import MAX_OUTBOUND_FILE_BYTES, extract_local_files


def test_pdf_and_step_references_are_captured_without_leaking_paths(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    pdf = project / "drawing.pdf"
    step = project / "part.step"
    pdf.write_bytes(b"%PDF-1.7\nfixture")
    step.write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;")

    result = extract_local_files(
        f'[加工图]({pdf})\n:codex-file-citation{{path="{step}" purpose="output"}}',
        project_root=project,
    )

    assert result.failures == ()
    assert [item.file_name for item in result.files] == ["drawing.pdf", "part.step"]
    assert [item.provider_file_type for item in result.files] == ["pdf", "stream"]
    assert str(pdf) not in result.text and str(step) not in result.text
    assert result.text == "📎 **加工图**\n📎 **part.step**"


def test_outside_project_file_is_rejected_without_path_leak(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "private.pdf"
    outside.write_bytes(b"%PDF-secret")

    result = extract_local_files(f"[资料]({outside})", project_root=project)

    assert result.files == () and result.failures
    assert str(outside) not in result.text
    assert "本地文件未转发" in result.text


def test_remote_and_unsupported_links_are_not_read(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = "[网页](https://example.invalid/a.pdf) [脚本](tool.py)"
    result = extract_local_files(source, project_root=project)
    assert result.text == source
    assert result.files == () and result.failures == ()


def test_oversized_file_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    large = project / "large.step"
    with large.open("wb") as handle:
        handle.truncate(MAX_OUTBOUND_FILE_BYTES + 1)
    result = extract_local_files(f"[大模型]({large})", project_root=project)
    assert result.files == ()
    assert result.failures == ("file_too_large",)
