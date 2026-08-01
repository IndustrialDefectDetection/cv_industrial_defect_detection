"""Security regression tests for the optional live SES email tool."""

from __future__ import annotations

import pytest

import report_paths
import strands_agent
from strands_agent import MESAgentManager, _build_report_link, _strict_boolean_env
from strands_agent import _credential_isolated_aws_session, _secure_ses_client


class FakeSESClient:
    def __init__(self):
        self.calls = []

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        return {"MessageId": "test-message-id"}


def email_manager(*, enabled: bool = True) -> MESAgentManager:
    manager = MESAgentManager.__new__(MESAgentManager)
    manager.email_enabled = enabled
    manager.region_name = "us-east-1"
    manager.sender_email = "sender@example.com"
    manager.recipient_email = "recipient@example.com"
    manager.base_url = "https://dashboard.example.com/reports"
    manager._init_email_tools()
    return manager


def test_live_email_escapes_model_html_and_uses_safe_report_link(
    tmp_path, monkeypatch
):
    reports_directory = tmp_path / "reports"
    reports_directory.mkdir()
    report = reports_directory / "root-cause_2026.pdf"
    report.write_bytes(b"%PDF-test")
    monkeypatch.setattr(report_paths, "REPORTS_DIR", reports_directory)

    fake_ses = FakeSESClient()
    client_calls = []

    def fake_client(_session, service, **kwargs):
        client_calls.append((service, kwargs))
        return fake_ses

    monkeypatch.setattr(strands_agent.boto3.Session, "client", fake_client)
    manager = email_manager()

    result = manager.execute_email_send(
        "Defect alert",
        '<img src="https://tracker.invalid/pixel" onerror="alert(1)">\nInvestigate',
        report.name,
    )

    assert result["success"] is True
    assert len(fake_ses.calls) == 1
    assert len(client_calls) == 1
    service, client_kwargs = client_calls[0]
    assert service == "ses"
    assert client_kwargs["region_name"] == "us-east-1"
    assert client_kwargs["endpoint_url"] == (
        "https://email.us-east-1.amazonaws.com"
    )
    assert client_kwargs["verify"] is True
    client_config = client_kwargs["config"]
    assert client_config.ignore_configured_endpoint_urls is True
    assert client_config.proxies == {}
    assert client_config.connect_timeout == 5
    assert client_config.read_timeout == 30
    assert client_config.retries == {
        "total_max_attempts": 3,
        "mode": "standard",
    }
    message = fake_ses.calls[0]["Message"]
    html_body = message["Body"]["Html"]["Data"]
    text_body = message["Body"]["Text"]["Data"]
    assert "<img" not in html_body
    assert "&lt;img" in html_body
    assert "<br>" in html_body
    assert "pdf=root-cause_2026.pdf" in text_body


# Explicit ids because pytest puts the generated id in PYTEST_CURRENT_TEST, and
# a 50,001-character body inlined into that name exceeds the 32,767-character
# Windows environment-variable limit - the test errors before it runs.
@pytest.mark.parametrize(
    ("subject", "body", "filename"),
    [
        pytest.param(
            "Header\r\nBcc: attacker@example.com",
            "Body",
            None,
            id="header-injection",
        ),
        pytest.param("Subject", "x" * 50_001, None, id="oversized-body"),
        pytest.param("Subject", "Body", "../outside.pdf", id="path-traversal"),
    ],
)
def test_invalid_live_email_input_never_reaches_ses(
    subject, body, filename, monkeypatch
):
    monkeypatch.setattr(
        strands_agent.boto3.Session,
        "client",
        lambda *_args, **_kwargs: pytest.fail("SES must not be called"),
    )
    manager = email_manager()

    result = manager.execute_email_send(subject, body, filename)

    assert result == {
        "success": False,
        "error": "Email content or configuration failed validation",
    }


def test_email_delivery_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MES_EMAIL_ENABLED", raising=False)
    assert _strict_boolean_env("MES_EMAIL_ENABLED") is False


def test_ses_client_initialization_failure_is_generic(monkeypatch):
    monkeypatch.setattr(
        strands_agent.boto3.Session,
        "client",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("credential provider included secret deployment detail")
        ),
    )
    manager = email_manager()

    result = manager.execute_email_send("Subject", "Body")

    assert result["success"] is False
    assert result["error"] == "Email delivery failed"
    assert "credential" not in str(result).lower()


def test_invalid_email_enable_value_fails_closed(monkeypatch):
    monkeypatch.setenv("MES_EMAIL_ENABLED", "tru")
    with pytest.raises(ValueError, match="MES_EMAIL_ENABLED"):
        _strict_boolean_env("MES_EMAIL_ENABLED")


def test_remote_report_links_require_https():
    with pytest.raises(ValueError, match="HTTPS"):
        _build_report_link("http://dashboard.example.com", "report.pdf")


def test_ses_client_ignores_ambient_endpoint_proxy_and_ca(monkeypatch):
    calls = []
    fake_ses = FakeSESClient()
    monkeypatch.setenv("AWS_ENDPOINT_URL_SES", "http://attacker.invalid")
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.invalid:8080")
    monkeypatch.setenv("AWS_CA_BUNDLE", "/tmp/attacker-ca.pem")
    monkeypatch.setattr(
        strands_agent.boto3.Session,
        "client",
        lambda _session, service, **kwargs: calls.append((service, kwargs))
        or fake_ses,
    )

    assert _secure_ses_client("us-east-1") is fake_ses
    service, kwargs = calls[0]
    assert service == "ses"
    assert kwargs["endpoint_url"] == "https://email.us-east-1.amazonaws.com"
    assert kwargs["verify"] is True
    assert kwargs["config"].ignore_configured_endpoint_urls is True
    assert kwargs["config"].proxies == {}


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
def test_ses_rejects_invalid_or_noncommercial_regions(
    region_name, monkeypatch
):
    monkeypatch.setattr(
        strands_agent.boto3.Session,
        "client",
        lambda *_args, **_kwargs: pytest.fail("SES must not be created"),
    )
    with pytest.raises(ValueError, match="AWS region"):
        _secure_ses_client(region_name)


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

    session = _credential_isolated_aws_session("us-east-1")

    # The ambient role profile is ignored, so there are no deferred STS
    # credentials to refresh and no signed source-credential request.
    assert session.get_credentials() is None


def test_static_environment_credentials_are_passed_explicitly(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "TESTACCESSKEY12345678")
    monkeypatch.setenv(
        "AWS_SECRET_ACCESS_KEY",
        "abcdefghijklmnopqrstuvwxyz1234567890AB",
    )
    monkeypatch.setenv("AWS_SESSION_TOKEN", "temporary-session-token")

    credentials = _credential_isolated_aws_session(
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
        _credential_isolated_aws_session("us-east-1")

    assert "do-not-log-this-secret" not in caplog.text
    assert "whitespace-token" not in caplog.text
