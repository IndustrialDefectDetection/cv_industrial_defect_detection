"""Tests for rendering untrusted agent output without passive requests."""

import pytest

from display_security import safe_log_text, safe_model_markdown, safe_terminal_text


def test_model_markdown_neutralizes_images_and_active_link_schemes():
    rendered = safe_model_markdown(
        "![track](https://attacker.invalid/pixel) "
        "[click](data:text/html,bad)"
    )

    assert "&#33;[track]" in rendered
    assert "](blocked:)" in rendered


@pytest.mark.parametrize(
    "unsafe_markdown",
    [
        "[click](java&#x73;cript:alert(1))",
        "[click](java\tscript:alert(1))",
        "[click][target]\n[target]: vbscript:msgbox(1)",
    ],
)
def test_model_markdown_blocks_obfuscated_active_schemes(unsafe_markdown):
    rendered = safe_model_markdown(unsafe_markdown)

    assert "blocked:" in rendered
    assert "javascript:" not in rendered.lower()
    assert "vbscript:" not in rendered.lower()


def test_model_markdown_is_bounded():
    assert safe_model_markdown("abcdef", max_chars=3).endswith(
        "[Output truncated]"
    )


@pytest.mark.parametrize(
    ("untrusted", "expected"),
    [
        ("\x1b[31mred\x1b[0m", "red"),
        ("before\x1b]0;spoofed title\x07after", "beforeafter"),
        ("before\x9d0;spoofed title\x9cafter", "beforeafter"),
        ("\x9b31mred\x9b0m", "red"),
        ("a\rb\b\x00\x7f\x85c", "abc"),
    ],
)
def test_terminal_text_removes_ansi_osc_and_control_characters(
    untrusted, expected
):
    rendered = safe_terminal_text(untrusted)

    assert rendered == expected
    assert "\x1b" not in rendered


def test_terminal_text_preserves_safe_layout_characters():
    assert safe_terminal_text("first\nsecond\tvalue") == "first\nsecond\tvalue"


def test_terminal_text_removes_bidi_and_invisible_format_controls():
    rendered = safe_terminal_text(
        "start\u202eoverridden\u202c\u2066isolated\u2069\u200bfinish"
    )

    assert rendered == "startoverriddenisolatedfinish"


def test_terminal_text_preserves_emoji_joiners():
    assert safe_terminal_text("👩\u200d💻") == "👩\u200d💻"


def test_log_text_is_single_line_and_control_free():
    rendered = safe_log_text(
        "first\nsecond\tthird\u2028\x1b]8;;https://evil.invalid\x07link"
    )

    assert rendered == r"first\nsecond\tthird\nlink"
    assert "\n" not in rendered
    assert "\t" not in rendered
    assert "\x1b" not in rendered
