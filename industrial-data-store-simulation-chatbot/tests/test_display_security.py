"""Tests for rendering untrusted agent output without passive requests."""

import pytest

from app_factory.shared.display_security import safe_model_markdown


def test_model_markdown_neutralizes_images_and_active_link_schemes():
    rendered = safe_model_markdown(
        "![track](https://attacker.invalid/pixel) "
        "[click](javascript:alert(1))"
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
    assert safe_model_markdown("abcdef", max_chars=3) == (
        "abc\n\n[Output truncated]"
    )
