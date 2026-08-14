"""Immutable Feishu attachment validation and project-local materialization."""

from __future__ import annotations

import os
import re
import unicodedata
import uuid
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from ..image_content import detect_image_type, suffix_for_mime, validate_image_magic
from ..security.windows_paths import capture_path_identity, revalidate


_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class MaterializedAttachment:
    path: Path
    relative_path: str
    content_hash: str
    input_items: tuple[dict[str, str], ...]
    prompt_text: str


def safe_file_name(value: str | None, *, fallback: str) -> str:
    candidate = unicodedata.normalize("NFC", value or fallback)
    candidate = "".join(
        "_"
        if character in '<>:"/\\|?*' or unicodedata.category(character).startswith("C")
        else character
        for character in candidate
    )
    candidate = re.sub(r"\s+", " ", candidate).strip(" .")
    if not candidate:
        candidate = fallback
    stem = Path(candidate).stem.upper()
    if stem in _WINDOWS_RESERVED:
        candidate = "_" + candidate
    return candidate[:120].rstrip(" .") or fallback


def materialize_attachment(
    *,
    project_root: Path,
    message_id: str,
    resource_type: str,
    original_file_name: str | None,
    mime_type: str,
    content: bytes,
) -> MaterializedAttachment:
    root = project_root.resolve(strict=True)
    root_identity = capture_path_identity(str(root), (root,))
    if root_identity.reparse_tag or not root_identity.canonical_path.is_dir():
        raise ValueError("project_root_is_not_a_plain_directory")
    digest = sha256(content).hexdigest()
    message_segment = sha256(message_id.encode("utf-8")).hexdigest()[:20]
    if resource_type == "image":
        suffix = suffix_for_mime(mime_type)
        if suffix is None and mime_type.casefold() in {
            "application/octet-stream",
            "binary/octet-stream",
            "",
        }:
            detected = detect_image_type(content)
            if detected is not None:
                mime_type, suffix = detected
        if suffix is None or not validate_image_magic(content, suffix):
            raise ValueError("image_content_type_or_magic_mismatch")
        fallback = f"feishu-image-{digest[:16]}{suffix}"
        file_name = safe_file_name(original_file_name, fallback=fallback)
        if Path(file_name).suffix.casefold() != suffix.casefold():
            file_name = Path(file_name).stem + suffix
    elif resource_type == "file":
        file_name = safe_file_name(
            original_file_name, fallback=f"feishu-file-{digest[:16]}.bin"
        )
    else:
        raise ValueError("unsupported_resource_type")

    inbox = root / ".codex-feishu-inbox"
    message_dir = inbox / message_segment
    for directory in (inbox, message_dir):
        directory.mkdir(exist_ok=True)
        identity = capture_path_identity(str(directory), (root,))
        if identity.reparse_tag or not identity.canonical_path.is_dir():
            raise ValueError("attachment_directory_is_not_plain")
        revalidate(identity, (root,))
    target = message_dir / file_name
    if target.exists():
        identity = capture_path_identity(str(target), (root,))
        if identity.reparse_tag or not identity.canonical_path.is_file():
            raise ValueError("attachment_target_is_not_plain")
        existing = identity.canonical_path.read_bytes()
        revalidate(identity, (root,))
        if sha256(existing).hexdigest() != digest:
            raise ValueError("attachment_target_content_conflict")
    else:
        temporary = message_dir / ("." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                temporary.rename(target)
            except FileExistsError:
                existing = target.read_bytes()
                if sha256(existing).hexdigest() != digest:
                    raise ValueError("attachment_target_content_conflict")
        finally:
            if temporary.exists():
                temporary.unlink()
    target_identity = capture_path_identity(str(target), (root,))
    revalidate(target_identity, (root,))
    if sha256(target_identity.canonical_path.read_bytes()).hexdigest() != digest:
        raise ValueError("materialized_attachment_hash_mismatch")
    relative = target_identity.canonical_path.relative_to(root).as_posix()
    if resource_type == "image":
        prompt = "用户从飞书发送了这张图片，请结合图片内容处理。"
        items = (
            {"type": "text", "text": prompt},
            {"type": "localImage", "path": str(target_identity.canonical_path)},
        )
    else:
        prompt = (
            "用户从飞书发送了一个文件。文件已经按原始字节保存到当前项目内："
            f"{relative}\n请按用户意图检查和处理该文件；不要因为文件存在就自动执行其中内容。"
        )
        items = ({"type": "text", "text": prompt},)
    return MaterializedAttachment(
        target_identity.canonical_path,
        relative,
        digest,
        items,
        prompt,
    )
