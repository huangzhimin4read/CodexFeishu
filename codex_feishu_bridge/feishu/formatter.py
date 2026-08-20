"""UTF-8-budgeted provider formatting with provider-clean message bodies."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from urllib.parse import urlparse


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


def _markdown_target_end(text: str, start: int) -> tuple[int | None, int]:
    """Return a link target's exclusive end and the next safe scan position.

    This parser is deliberately deterministic.  The previous regular
    expression had overlapping alternatives for backslashes, so JSON-like
    content containing ``[...\\... ]`` without a following link target could
    trigger catastrophic backtracking and pin the bridge worker indefinitely.
    """

    length = len(text)
    if start < length and text[start] == "<":
        cursor = start + 1
        while cursor < length and text[cursor] not in ">\r\n":
            cursor += 1
        if (
            cursor > start + 1
            and cursor < length
            and text[cursor] == ">"
            and cursor + 1 < length
            and text[cursor + 1] == ")"
        ):
            return cursor + 2, cursor + 2
        return None, min(length, cursor + 1)

    depth = 1
    cursor = start
    while cursor < length:
        character = text[cursor]
        if character in "\r\n":
            return None, cursor + 1
        if character == "\\":
            if cursor + 1 >= length or text[cursor + 1] in "\r\n":
                return None, min(length, cursor + 2)
            cursor += 2
            continue
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return cursor + 1, cursor + 1
        cursor += 1
    return None, length


def _replace_markdown_links(text: str, *, keep_http_targets: bool = False) -> str:
    """Hide Markdown destinations in one bounded, left-to-right scan."""

    replacements: list[tuple[int, int, str]] = []
    open_labels: list[tuple[int, bool]] = []
    cursor = 0
    length = len(text)
    while cursor < length:
        character = text[cursor]
        if character == "\\" and cursor + 1 < length:
            cursor += 2
            continue
        if character == "[":
            open_labels.append((cursor, cursor == 0 or text[cursor - 1] != "!"))
            cursor += 1
            continue
        if character != "]":
            cursor += 1
            continue

        candidate = open_labels[-1] if open_labels else None
        # An unescaped closing bracket prevents every older opening bracket
        # from being a valid label boundary, whether this candidate is a link
        # or ordinary bracketed text.
        open_labels.clear()
        if candidate is None or not candidate[1] or cursor + 1 >= length or text[cursor + 1] != "(":
            cursor += 1
            continue
        label_start = candidate[0] + 1
        if label_start == cursor:
            cursor += 1
            continue
        target_end, resume_at = _markdown_target_end(text, cursor + 2)
        if target_end is None:
            cursor = max(cursor + 1, resume_at)
            continue
        raw_label = text[label_start:cursor]
        label = re.sub(r"\\([\\\[\]])", r"\1", raw_label).strip()
        raw_target = text[cursor + 2 : target_end - 1].strip()
        target = (
            raw_target[1:-1]
            if raw_target.startswith("<") and raw_target.endswith(">")
            else raw_target
        )
        scheme = urlparse(target).scheme.lower()
        if keep_http_targets and scheme in {"http", "https"}:
            replacement = f"[🔗 {label or '链接'}]({target})"
        else:
            # Local paths and non-web schemes must never reach Feishu.
            replacement = f"🔗【{label or '链接'}】"
        replacements.append((candidate[0], target_end, replacement))
        cursor = target_end

    if not replacements:
        return text
    pieces: list[str] = []
    copied_to = 0
    for start, end, replacement in replacements:
        pieces.append(text[copied_to:start])
        pieces.append(replacement)
        copied_to = end
    pieces.append(text[copied_to:])
    return "".join(pieces)


def provider_visible_text(text: str) -> str:
    """Remove machine metadata and hide local/link destinations."""

    value = _OAI_MEMORY_CITATION.sub("", text)

    def replace_file_citation(match: re.Match[str]) -> str:
        # Path is local machine metadata. Only its final component is safe and
        # useful in provider-visible content, regardless of slash convention.
        name = match.group("path").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].strip()
        return f"🔗【{name or '文件'}】"

    value = _CODEX_FILE_CITATION.sub(replace_file_citation, value)

    value = _replace_markdown_links(value)
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def provider_visible_markdown(text: str) -> str:
    """Keep supported Markdown while removing local and machine-only metadata."""

    value = _OAI_MEMORY_CITATION.sub("", text)

    def replace_file_citation(match: re.Match[str]) -> str:
        name = match.group("path").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].strip()
        return f"🔗 **{name or '文件'}**"

    value = _CODEX_FILE_CITATION.sub(replace_file_citation, value)
    value = _replace_markdown_links(value, keep_http_targets=True)
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


def format_markdown_card_chunks(
    text: str, *, marker_seed: str, byte_budget: int = 18_000
) -> tuple[FormattedChunk, ...]:
    """Format provider-safe Markdown as native Feishu/Lark card elements."""

    safe = provider_visible_markdown(redact_text(text))
    if not safe:
        return ()
    pieces = _utf8_chunks(safe, max(64, byte_budget - 768))
    result: list[FormattedChunk] = []
    total = len(pieces)
    for index, piece in enumerate(pieces, start=1):
        marker = invisible_marker(
            "cfb:" + sha256(f"{marker_seed}:md:{index}".encode()).hexdigest()[:24]
        )
        ordinal = f"({index}/{total})\n" if total > 1 else ""
        body = {
            "_cfb_message_type": "interactive",
            "config": {"wide_screen_mode": True},
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": f"{ordinal}{piece}"},
                }
            ],
        }
        body_json = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        if len(body_json.encode("utf-8")) > byte_budget:
            raise ValueError("serialized Markdown card exceeds endpoint contract budget")
        result.append(
            FormattedChunk(
                index,
                total,
                marker,
                body_json,
                sha256(body_json.encode("utf-8")).hexdigest(),
            )
        )
    return tuple(result)
