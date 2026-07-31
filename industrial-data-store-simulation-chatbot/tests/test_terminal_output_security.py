"""Regression tests for terminal/control-sequence injection defenses."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app_factory.shared.display_security import (
    safe_log_text,
    safe_terminal_text,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


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


def test_log_text_prevents_multiline_log_forgery():
    rendered = safe_log_text(
        "first\nsecond\tthird\u2028\x1b]8;;https://evil.invalid\x07link"
    )

    assert rendered == r"first\nsecond\tthird\nlink"
    assert "\n" not in rendered
    assert "\t" not in rendered
    assert "\x1b" not in rendered


def test_terminal_text_removes_bidi_and_invisible_format_controls():
    rendered = safe_terminal_text(
        "start\u202eoverridden\u202c\u2066isolated\u2069\u200bfinish"
    )

    assert rendered == "startoverriddenisolatedfinish"


def test_terminal_text_preserves_emoji_joiners():
    assert safe_terminal_text("👩\u200d💻") == "👩\u200d💻"


@pytest.mark.parametrize(
    "relative_path",
    [
        "app_factory/mes_agents/mes_analysis_agent.py",
        "app_factory/production_meeting_agents/production_meeting_agent.py",
    ],
)
def test_every_strands_agent_explicitly_disables_stdout_callback(relative_path):
    tree = ast.parse((PROJECT_ROOT / relative_path).read_text(encoding="utf-8"))
    agent_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Agent"
    ]

    assert agent_calls
    for call in agent_calls:
        callback = next(
            (
                keyword.value
                for keyword in call.keywords
                if keyword.arg == "callback_handler"
            ),
            None,
        )
        assert isinstance(callback, ast.Constant)
        assert callback.value is None
