"""No-network regression tests for hardened AWS transports."""

from __future__ import annotations

from types import SimpleNamespace

import boto3
import pytest

from app_factory.shared.aws_security import (
    _SecureBotoSession,
    _credential_isolated_session,
    create_bedrock_model,
    create_secure_aws_client,
    validate_aws_region,
)


def _capture_clients(monkeypatch):
    calls = []

    def fake_client(_session, service_name, **kwargs):
        calls.append((service_name, kwargs))
        return SimpleNamespace(
            meta=SimpleNamespace(region_name=kwargs["region_name"])
        )

    monkeypatch.setattr(boto3.Session, "client", fake_client)
    return calls


def _assert_hardened_call(service_name, kwargs, *, read_timeout):
    assert kwargs["region_name"] == "us-east-1"
    assert kwargs["endpoint_url"] == (
        f"https://{service_name}.us-east-1.amazonaws.com"
    )
    assert kwargs["verify"] is True

    config = kwargs["config"]
    assert config.ignore_configured_endpoint_urls is True
    assert config.proxies == {}
    assert config.connect_timeout == 5
    assert config.read_timeout == read_timeout
    assert config.retries == {
        "total_max_attempts": 3,
        "mode": "standard",
    }


def test_bedrock_clients_ignore_ambient_endpoint_proxy_and_ca(monkeypatch):
    monkeypatch.setenv(
        "AWS_ENDPOINT_URL_BEDROCK",
        "http://attacker.invalid",
    )
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.invalid:8080")
    monkeypatch.setenv("AWS_CA_BUNDLE", "/tmp/attacker-ca.pem")
    calls = _capture_clients(monkeypatch)

    client = create_secure_aws_client(
        "bedrock",
        region_name="us-east-1",
        read_timeout_seconds=45,
    )

    assert client.meta.region_name == "us-east-1"
    assert len(calls) == 1
    service_name, kwargs = calls[0]
    assert service_name == "bedrock"
    _assert_hardened_call(service_name, kwargs, read_timeout=45)


def test_strands_model_uses_the_hardened_runtime_client(monkeypatch):
    monkeypatch.setenv(
        "AWS_ENDPOINT_URL_BEDROCK_RUNTIME",
        "http://attacker.invalid",
    )
    monkeypatch.setenv("ALL_PROXY", "http://attacker.invalid:8080")
    calls = _capture_clients(monkeypatch)

    model = create_bedrock_model(
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        region_name="us-east-1",
        read_timeout_seconds=120,
    )

    assert model.client is not None
    assert len(calls) == 1
    service_name, kwargs = calls[0]
    assert service_name == "bedrock-runtime"
    _assert_hardened_call(service_name, kwargs, read_timeout=120)
    assert "strands-agents" in kwargs["config"].user_agent_extra


@pytest.mark.parametrize(
    "region_name",
    [
        "us-east-1.attacker.invalid",
        "us-east-1 ",
        "http://us-east-1",
        "cn-north-1",
        "",
    ],
)
def test_invalid_or_noncommercial_regions_are_rejected(region_name):
    with pytest.raises(ValueError, match="AWS region"):
        validate_aws_region(
            region_name,
            service_name="bedrock-runtime",
        )


@pytest.mark.parametrize("timeout", [0, 301, float("inf"), "not-a-number"])
def test_unbounded_or_invalid_aws_timeouts_are_rejected(timeout):
    with pytest.raises(ValueError, match="timeout"):
        create_secure_aws_client(
            "bedrock",
            region_name="us-east-1",
            read_timeout_seconds=timeout,
        )


def test_role_profile_and_ambient_sts_endpoint_are_not_credential_providers(
    tmp_path, monkeypatch
):
    credentials_file = tmp_path / "credentials"
    credentials_file.write_text(
        "[source]\n"
        "aws_access_key_id = TESTACCESSKEY12345678\n"
        "aws_secret_access_key = abcdefghijklmnopqrstuvwxyz1234567890AB\n",
        encoding="utf-8",
    )
    config_file = tmp_path / "config"
    config_file.write_text(
        "[profile role]\n"
        "role_arn = arn:aws:iam::123456789012:role/test\n"
        "source_profile = source\n",
        encoding="utf-8",
    )
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_PROFILE", "role")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
    monkeypatch.setenv(
        "AWS_SHARED_CREDENTIALS_FILE",
        str(credentials_file),
    )
    monkeypatch.setenv("AWS_ENDPOINT_URL_STS", "http://attacker.invalid")
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.invalid:8080")

    session = _credential_isolated_session("us-east-1")
    hardened_session = _SecureBotoSession(
        service_name="bedrock-runtime",
        region_name="us-east-1",
        read_timeout_seconds=30,
    )

    # No deferred role credentials exist to make a signed STS request.
    assert session.get_credentials() is None
    assert hardened_session.get_credentials() is None


def test_static_environment_credentials_are_passed_explicitly(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "TESTACCESSKEY12345678")
    monkeypatch.setenv(
        "AWS_SECRET_ACCESS_KEY",
        "abcdefghijklmnopqrstuvwxyz1234567890AB",
    )
    monkeypatch.setenv("AWS_SESSION_TOKEN", "temporary-session-token")

    credentials = _credential_isolated_session(
        "us-east-1"
    ).get_credentials()

    assert credentials is not None
    assert credentials.method == "explicit"
    assert credentials.access_key == "TESTACCESSKEY12345678"
    assert credentials.token == "temporary-session-token"


@pytest.mark.parametrize(
    "credential_values",
    [
        {"AWS_ACCESS_KEY_ID": "TESTACCESSKEY12345678"},
        {
            "AWS_ACCESS_KEY_ID": "TESTACCESSKEY12345678",
            "AWS_SECRET_ACCESS_KEY": "",
        },
        {
            "AWS_ACCESS_KEY_ID": "TESTACCESSKEY12345678",
            "AWS_SECRET_ACCESS_KEY": "do-not-log-this-secret",
            "AWS_SESSION_TOKEN": " whitespace-token ",
        },
    ],
)
def test_partial_blank_or_whitespace_credentials_fail_without_secret_logging(
    credential_values, monkeypatch, caplog
):
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in credential_values.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match="AWS_"):
        _credential_isolated_session("us-east-1")

    assert "do-not-log-this-secret" not in caplog.text
    assert "whitespace-token" not in caplog.text
