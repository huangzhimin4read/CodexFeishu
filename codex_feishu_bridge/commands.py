"""Strict full-message command grammar."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


class CommandError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ControlCommand:
    name: str
    argument: str | None = None


_NO_ARGUMENT = frozenset({"tasks", "status", "help", "stop", "hard-stop"})
_ONE_TOKEN = {
    "use": re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"),
    "sandbox": re.compile(r"readOnly|workspaceWrite|dangerFullAccess"),
    "network": re.compile(r"on|off"),
    "approval-policy": re.compile(r"untrusted|on-request|never"),
}
_REMAINDER = frozenset({"cwd", "writable", "append"})


def parse_command(message_type: str, text: str) -> ControlCommand | None:
    if message_type != "text" or not isinstance(text, str):
        return None
    normalized = unicodedata.normalize("NFC", text)
    if normalized != text or not normalized.startswith("/"):
        return None
    if normalized != normalized.strip() or "\n" in normalized or "\r" in normalized:
        return None
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in normalized):
        return None
    if "```" in normalized or "`" in normalized:
        return None
    match = re.fullmatch(r"/([a-z][a-z-]*)(?: ([^\r\n]+))?", normalized)
    if match is None:
        return None
    name, argument = match.group(1), match.group(2)
    if name in _NO_ARGUMENT:
        return ControlCommand(name) if argument is None else None
    if name in _ONE_TOKEN:
        if argument is not None and _ONE_TOKEN[name].fullmatch(argument):
            return ControlCommand(name, argument)
        return None
    if name in _REMAINDER:
        if argument is None or not argument.strip() or argument != argument.strip():
            return None
        if name != "append" and any(char in argument for char in ('"', "'", "*", "?", "%")):
            return None
        return ControlCommand(name, argument)
    return None
