"""Local Markdown-image extraction with project-boundary and file-identity checks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import unquote, urlparse

from ..image_content import (
    MAX_IMAGE_BYTES,
    detect_image_type,
    mime_for_suffix,
    validate_image_magic,
)
from ..security.windows_paths import PathValidationError, capture_path_identity, revalidate
from .formatter import _markdown_target_end


@dataclass(frozen=True, slots=True)
class LocalImage:
    label: str
    source_path: str
    file_name: str
    mime_type: str
    content: bytes
    content_hash: str


@dataclass(frozen=True, slots=True)
class ImageExtraction:
    text: str
    images: tuple[LocalImage, ...]
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _MarkdownImage:
    start: int
    end: int
    alt: str
    target: str
    raw: str


def _markdown_images(text: str) -> tuple[_MarkdownImage, ...]:
    """Parse image references without regex backtracking on JSON-like text."""

    found: list[_MarkdownImage] = []
    cursor = 0
    length = len(text)
    while cursor < length:
        start = text.find("![", cursor)
        if start < 0:
            break
        label_start = start + 2
        close = label_start
        while close < length:
            if text[close] == "\\" and close + 1 < length:
                close += 2
                continue
            if text[close] == "]":
                break
            close += 1
        if close >= length:
            break
        if close + 1 >= length or text[close + 1] != "(":
            cursor = close + 1
            continue
        end, resume_at = _markdown_target_end(text, close + 2)
        if end is None:
            cursor = max(close + 1, resume_at)
            continue
        raw_alt = text[label_start:close]
        alt = re.sub(r"\\([\\\[\]])", r"\1", raw_alt).strip()
        found.append(_MarkdownImage(start, end, alt, text[close + 2 : end - 1], text[start:end]))
        cursor = end
    return tuple(found)


def _local_target(target: str, project_root: Path) -> Path | None:
    value = target.strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1]
    parsed = urlparse(value)
    if parsed.scheme.lower() in {"http", "https", "data"}:
        return None
    if parsed.scheme.lower() == "file":
        if parsed.netloc not in {"", "localhost"}:
            raise PathValidationError("remote file URI is forbidden")
        value = unquote(parsed.path)
    else:
        value = unquote(value)
    if re.match(r"^/[A-Za-z]:[\\/]", value):
        value = value[1:]
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = project_root / candidate
    return candidate


def _read_local_image(candidate: Path, project_root: Path, label: str) -> LocalImage:
    suffix = candidate.suffix.lower()
    declared_mime_type = mime_for_suffix(suffix)
    if declared_mime_type is None:
        raise ValueError("unsupported_format")
    identity = capture_path_identity(str(candidate), (project_root,))
    if identity.missing_suffix or not identity.canonical_path.is_file():
        raise ValueError("not_a_file")
    if identity.reparse_tag:
        raise ValueError("reparse_point_forbidden")
    revalidate(identity, (project_root,))
    size = identity.canonical_path.stat().st_size
    if size <= 0:
        raise ValueError("empty_file")
    if size > MAX_IMAGE_BYTES:
        raise ValueError("file_too_large")
    content = identity.canonical_path.read_bytes()
    revalidate(identity, (project_root,))
    if len(content) != size:
        raise ValueError("file_changed_during_read")
    if validate_image_magic(content, suffix):
        mime_type = declared_mime_type
        file_name = identity.canonical_path.name
    else:
        detected = detect_image_type(content)
        if detected is None:
            raise ValueError("content_format_mismatch")
        mime_type, detected_suffix = detected
        file_name = identity.canonical_path.stem + detected_suffix
    return LocalImage(
        label=label,
        source_path=str(identity.canonical_path),
        file_name=file_name,
        mime_type=mime_type,
        content=content,
        content_hash=sha256(content).hexdigest(),
    )


def extract_local_images(text: str, *, project_root: Path) -> ImageExtraction:
    """Replace local Markdown image URLs with labels and capture immutable bytes.

    Arbitrary HTTP(S) and data URLs remain in the text and are never fetched.
    Local files must resolve inside the task's current Codex project root.
    """

    images: list[LocalImage] = []
    failures: list[str] = []
    pieces: list[str] = []
    position = 0
    for match in _markdown_images(text):
        pieces.append(text[position : match.start])
        alt = match.alt
        target = match.target
        try:
            candidate = _local_target(target, project_root)
            if candidate is None:
                pieces.append(match.raw)
            else:
                image = _read_local_image(candidate, project_root.resolve(strict=True), alt)
                images.append(image)
                pieces.append(f"[图片：{alt or image.file_name}]")
        except (OSError, ValueError, PathValidationError) as exc:
            reason = str(exc) or type(exc).__name__
            failures.append(reason)
            pieces.append(f"[本地图片未转发：{alt or '未命名图片'}（{reason}）]")
        position = match.end
    pieces.append(text[position:])
    return ImageExtraction("".join(pieces), tuple(images), tuple(failures))
