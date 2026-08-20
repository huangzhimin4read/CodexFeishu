"""Safe extraction of small project-local files referenced by Codex output."""

from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from ..security.windows_paths import PathValidationError, capture_path_identity, revalidate
from .formatter import _markdown_target_end
from .images import _local_target


MAX_OUTBOUND_FILE_BYTES = 20 * 1024 * 1024

# Keep this deliberately finite: an arbitrary local link must never become an
# implicit file exfiltration primitive. CAD and ordinary office handoff formats
# are included; images remain owned by the image pipeline.
_ALLOWED_SUFFIXES = {
    ".pdf", ".step", ".stp", ".iges", ".igs", ".dxf", ".dwg", ".stl",
    ".fcstd", ".zip", ".7z", ".rar", ".tar", ".gz", ".bz2",
    ".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
}
_NATIVE_FILE_TYPES = {
    ".pdf": "pdf", ".doc": "doc", ".docx": "docx", ".xls": "xls",
    ".xlsx": "xlsx", ".ppt": "ppt", ".pptx": "pptx", ".zip": "zip",
    ".7z": "7z", ".rar": "rar", ".tar": "tar", ".gz": "gz", ".bz2": "bz2",
}
_CODEX_FILE_CITATION = re.compile(
    r':codex-file-citation\{\s*path="(?P<path>[^"\r\n]+)"'
    r'(?:\s+purpose="[^"\r\n]*")?\s*\}',
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class LocalFile:
    label: str
    source_path: str
    file_name: str
    mime_type: str
    provider_file_type: str
    content: bytes
    content_hash: str


@dataclass(frozen=True, slots=True)
class FileExtraction:
    text: str
    files: tuple[LocalFile, ...]
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Reference:
    start: int
    end: int
    label: str
    target: str


def _references(text: str) -> tuple[_Reference, ...]:
    found = [
        _Reference(match.start(), match.end(), Path(match.group("path")).name, match.group("path"))
        for match in _CODEX_FILE_CITATION.finditer(text)
    ]
    cursor = 0
    while cursor < len(text):
        start = text.find("[", cursor)
        if start < 0:
            break
        if start > 0 and text[start - 1] == "!":
            cursor = start + 1
            continue
        close = text.find("]", start + 1)
        if close < 0 or close + 1 >= len(text) or text[close + 1] != "(":
            cursor = start + 1
            continue
        end, resume_at = _markdown_target_end(text, close + 2)
        if end is None:
            cursor = max(close + 1, resume_at)
            continue
        # Codex citations and Markdown links cannot validly overlap. Ignore a
        # Markdown candidate that sits inside an already-recognized citation.
        if not any(item.start <= start < item.end for item in found):
            label = re.sub(r"\\([\\\[\]])", r"\1", text[start + 1 : close]).strip()
            found.append(_Reference(start, end, label, text[close + 2 : end - 1]))
        cursor = end
    return tuple(sorted(found, key=lambda item: item.start))


def _read_local_file(candidate: Path, project_root: Path, label: str) -> LocalFile:
    suffix = candidate.suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
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
    if size > MAX_OUTBOUND_FILE_BYTES:
        raise ValueError("file_too_large")
    content = identity.canonical_path.read_bytes()
    revalidate(identity, (project_root,))
    if len(content) != size:
        raise ValueError("file_changed_during_read")
    file_name = identity.canonical_path.name
    return LocalFile(
        label=label or file_name,
        source_path=str(identity.canonical_path),
        file_name=file_name,
        mime_type=mimetypes.guess_type(file_name)[0] or "application/octet-stream",
        provider_file_type=_NATIVE_FILE_TYPES.get(suffix, "stream"),
        content=content,
        content_hash=sha256(content).hexdigest(),
    )


def extract_local_files(text: str, *, project_root: Path) -> FileExtraction:
    """Capture allowed project-local references and replace paths with labels."""

    references = _references(text)
    if not references:
        return FileExtraction(text, (), ())
    files: list[LocalFile] = []
    failures: list[str] = []
    pieces: list[str] = []
    position = 0
    resolved_root = project_root.resolve(strict=True)
    for reference in references:
        pieces.append(text[position : reference.start])
        try:
            candidate = _local_target(reference.target, resolved_root)
            if candidate is None:
                pieces.append(text[reference.start : reference.end])
            elif candidate.suffix.lower() not in _ALLOWED_SUFFIXES:
                pieces.append(text[reference.start : reference.end])
            else:
                local_file = _read_local_file(candidate, resolved_root, reference.label)
                files.append(local_file)
                pieces.append(f"📎 **{local_file.label}**")
        except (OSError, ValueError, PathValidationError) as exc:
            reason = str(exc) or type(exc).__name__
            failures.append(reason)
            pieces.append(f"[本地文件未转发：{reference.label or '未命名文件'}（{reason}）]")
        position = reference.end
    pieces.append(text[position:])
    return FileExtraction("".join(pieces), tuple(files), tuple(failures))
