"""UTF-8-budgeted provider formatting with provider-clean message bodies."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256


_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)((?:app_)?secret|access_token|refresh_token)(\s*[=:]\s*)[^\s,;]+"),
)
_OAI_MEMORY_CITATION = re.compile(
    r"<oai-mem-citation\b[^>]*>.*?</oai-mem-citation\s*>",
    re.IGNORECASE | re.DOTALL,
)
_CODEX_FILE_CITATION = re.compile(
    r':codex-file-citation\{\s*path="(?P<path>[^"\r\n]+)"'
    r'(?:\s+purpose="[^"\r\n]*")?\s*\}',
    re.IGNORECASE,
)
_MARKDOWN_LINK = re.compile(
    r"(?<!!)\[(?P<label>(?:\\.|[^\]])+)\]\("
    r"(?P<target><[^>\r\n]+>|(?:\\.|[^()\r\n])*(?:\((?:\\.|[^()\r\n])*\)(?:\\.|[^()\r\n])*)*)"
    r"\)"
)
_TAG_BASE = 0xE0000
_TAG_CANCEL = chr(0xE007F)


def invisible_marker(value: str) -> str:
    """Encode a printable ASCII reconciliation token for local persistence only.

    Some Feishu mobile builds render Unicode tag characters as visible garbage.
    The encoded value therefore remains an internal database key and must never
    be appended to provider-visible content.
    """

    if not value or any(ord(character) < 0x20 or ord(character) > 0x7E for character in value):
        raise ValueError("marker source must be printable ASCII")
    return "\u2063" + "".join(chr(_TAG_BASE + ord(character)) for character in value) + _TAG_CANCEL


def redact_text(text: str) -> str:
    value = text
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(
            lambda match: "".join(part or "" for part in match.groups()) + "<redacted>",
            value,
        )
    return value


def provider_visible_text(text: str) -> str:
    """Remove machine metadata and hide local/link destinations."""

    value = _OAI_MEMORY_CITATION.sub("", text)

    def replace_file_citation(match: re.Match[str]) -> str:
        # Path is local machine metadata. Only its final component is safe and
        # useful in provider-visible content, regardless of slash convention.
        name = match.group("path").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].strip()
        return f"🔗【{name or '文件'}】"

    value = _CODEX_FILE_CITATION.sub(replace_file_citation, value)

    def replace_link(match: re.Match[str]) -> str:
        label = re.sub(r"\\([\\\[\]])", r"\1", match.group("label")).strip()
        return f"🔗【{label or '链接'}】"

    value = _MARKDOWN_LINK.sub(replace_link, value)
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _utf8_chunks(text: str, budget: int) -> list[str]:
    if budget <= 0:
        raise ValueError("budget must be positive")
    chunks: list[str] = []
    current: list[str] = []
    used = 0
    for character in text:
        size = len(character.encode("utf-8"))
        if current and used + size > budget:
            chunks.append("".join(current))
            current = []
            used = 0
        if size > budget:
            raise ValueError("budget cannot fit a single Unicode scalar")
        current.append(character)
        used += size
    if current or not chunks:
        chunks.append("".join(current))
    return chunks


@dataclass(frozen=True, slots=True)
class FormattedChunk:
    index: int
    total: int
    marker: str
    body_json: str
    body_hash: str


def format_text_chunks(text: str, *, marker_seed: str, byte_budget: int = 18_000) -> tuple[FormattedChunk, ...]:
    safe = provider_visible_text(redact_text(text))
    if not safe:
        return ()
    # Reserve envelope and ordinal bytes conservatively.
    pieces = _utf8_chunks(safe, max(64, byte_budget - 512))
    result: list[FormattedChunk] = []
    total = len(pieces)
    for index, piece in enumerate(pieces, start=1):
        marker = invisible_marker(
            "cfb:" + sha256(f"{marker_seed}:{index}".encode()).hexdigest()[:24]
        )
        ordinal = f"({index}/{total})\n" if total > 1 else ""
        content = f"{ordinal}{piece}"
        body = json.dumps({"text": content}, ensure_ascii=False, separators=(",", ":"))
        if len(body.encode("utf-8")) > byte_budget:
            raise ValueError("serialized message exceeds endpoint contract budget")
        result.append(
            FormattedChunk(index, total, marker, body, sha256(body.encode("utf-8")).hexdigest())
        )
    return tuple(result)
