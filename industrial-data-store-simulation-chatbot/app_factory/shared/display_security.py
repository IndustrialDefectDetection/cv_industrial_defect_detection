"""Safe presentation helpers for model-generated Markdown."""

from __future__ import annotations

import re
import unicodedata
from html import unescape as html_unescape


_INLINE_LINK_DESTINATION = re.compile(
    r"(?P<prefix>\]\(\s*)(?P<destination>[^)]{0,2048})",
    re.IGNORECASE,
)
_REFERENCE_LINK_DESTINATION = re.compile(
    r"(?m)^(?P<prefix>[ \t]{0,3}\[[^\]\r\n]+\]:[ \t]*)"
    r"(?P<destination>\S{1,2048})"
)
_UNSAFE_SCHEMES = ("javascript:", "data:", "vbscript:")
_ANSI_CSI = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]")
_ANSI_OSC = re.compile(
    r"(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c|$)"
)
_ANSI_STRING = re.compile(
    r"\x1b[PX^_](?:(?!\x1b\\)[\s\S])*(?:\x1b\\|$)"
)


def safe_terminal_text(value: object, *, max_chars: int = 100_000) -> str:
    """Remove terminal control sequences while preserving readable layout.

    Model, database, and user-controlled strings can otherwise use ANSI/OSC
    sequences, carriage returns, or backspaces to rewrite terminal output.
    Newlines and tabs are intentionally retained for trace and UI readability.
    """

    text = ("" if value is None else str(value))[: max_chars + 256]
    text = _ANSI_OSC.sub("", text)
    text = _ANSI_STRING.sub("", text)
    text = _ANSI_CSI.sub("", text)
    safe_characters: list[str] = []
    for character in text:
        if character in ("\u2028", "\u2029"):
            safe_characters.append("\n")
        elif (
            character in ("\n", "\t")
            or (
                ord(character) >= 32
                and not 127 <= ord(character) <= 159
                and (
                    unicodedata.category(character) != "Cf"
                    or character == "\u200d"
                )
            )
        ):
            safe_characters.append(character)
    text = "".join(safe_characters)
    return text[:max_chars]


def safe_log_text(value: object, *, max_chars: int = 2_000) -> str:
    """Return a bounded, single-line representation for untrusted log fields."""

    return (
        safe_terminal_text(value, max_chars=max_chars)
        .replace("\n", r"\n")
        .replace("\t", r"\t")
    )


def _normalized_link_target(destination: str) -> str:
    """Normalize parser/browser-obscuring characters before scheme checks."""
    decoded = html_unescape(destination).strip().lstrip("<")
    return "".join(
        character
        for character in decoded
        if not character.isspace() and 32 < ord(character) != 127
    ).lower()


def _neutralize_unsafe_destination(match: re.Match) -> str:
    destination = match.group("destination")
    if _normalized_link_target(destination).startswith(_UNSAFE_SCHEMES):
        return match.group("prefix") + "blocked:"
    return match.group(0)


def safe_model_markdown(value: object, *, max_chars: int = 100_000) -> str:
    """Bound model text and prevent passive image loads or active URL schemes."""

    text = safe_terminal_text(value, max_chars=max_chars + 1)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[Output truncated]"
    text = text.replace("\x00", "\N{REPLACEMENT CHARACTER}")
    # An HTML entity is decoded only after Markdown tokenization, so it cannot
    # become image syntax even when the source already contains backslashes.
    text = text.replace("![", "&#33;[")
    text = _INLINE_LINK_DESTINATION.sub(_neutralize_unsafe_destination, text)
    return _REFERENCE_LINK_DESTINATION.sub(
        _neutralize_unsafe_destination,
        text,
    )
