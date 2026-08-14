from pathlib import Path

from codex_feishu_bridge.feishu.images import extract_local_images


def test_extracts_drive_absolute_local_image_without_forwarding_path(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    image = project / "chart.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    slash_absolute = "/" + image.as_posix()
    result = extract_local_images(
        f"图如下：![血压曲线](<{slash_absolute}>)", project_root=project
    )
    assert result.failures == ()
    assert len(result.images) == 1
    assert result.images[0].content == image.read_bytes()
    assert str(image) not in result.text and slash_absolute not in result.text
    assert result.text == "图如下：[图片：血压曲线]"


def test_outside_project_image_is_not_read_or_leaked(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\nsecret")
    result = extract_local_images(
        f"![外部图片](<{outside}>)", project_root=project
    )
    assert result.images == ()
    assert result.failures
    assert str(outside) not in result.text
    assert "本地图片未转发" in result.text


def test_remote_image_url_is_left_clickable_and_never_fetched(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    text = "![远程图](https://example.invalid/image.png)"
    result = extract_local_images(text, project_root=project)
    assert result.text == text
    assert result.images == () and result.failures == ()


def test_two_adjacent_local_images_are_captured_independently(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    front = project / "TransparentGuard_REV-U_front.png"
    rear = project / "TransparentGuard_REV-U_rear.png"
    front.write_bytes(b"\x89PNG\r\n\x1a\nfront")
    rear.write_bytes(b"\x89PNG\r\n\x1a\nrear")
    text = (
        "正面与对面侧板：\n\n"
        f"![正面侧板]({front.as_posix()})\n\n"
        f"![对面侧板]({rear.as_posix()})"
    )

    result = extract_local_images(text, project_root=project)

    assert result.failures == ()
    assert [image.label for image in result.images] == ["正面侧板", "对面侧板"]
    assert [image.file_name for image in result.images] == [front.name, rear.name]
    assert result.text == "正面与对面侧板：\n\n[图片：正面侧板]\n\n[图片：对面侧板]"
    assert front.as_posix() not in result.text and rear.as_posix() not in result.text
