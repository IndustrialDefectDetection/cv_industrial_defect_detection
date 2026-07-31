"""The billed Anthropic client must not inherit credential-routing settings."""

from __future__ import annotations

import asyncio

import pytest

from strands_agent import _anthropic_client_args


def test_anthropic_transport_is_fixed_to_https_without_env_or_redirects(
    monkeypatch,
):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://attacker.invalid")
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.invalid:8080")
    monkeypatch.setenv("MES_API_TIMEOUT", "45")
    monkeypatch.setenv("MES_API_RETRIES", "1")

    arguments = _anthropic_client_args("sk-ant-test-value-not-real")
    client = arguments["http_client"]
    try:
        assert arguments["base_url"] == "https://api.anthropic.com"
        assert arguments["max_retries"] == 1
        assert client.follow_redirects is False
        assert client._trust_env is False
    finally:
        asyncio.run(client.aclose())


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MES_API_TIMEOUT", "nan"),
        ("MES_API_TIMEOUT", "301"),
        ("MES_API_RETRIES", "6"),
    ],
)
def test_anthropic_transport_bounds_retry_and_timeout_settings(
    monkeypatch,
    name,
    value,
):
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError):
        _anthropic_client_args("sk-ant-test-value-not-real")
