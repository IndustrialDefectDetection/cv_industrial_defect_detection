"""Hardened AWS transports for the MES applications.

AWS endpoint and proxy environment variables are intentionally ignored here.
Every credentialed client is pinned to the public AWS HTTPS endpoint for a
validated commercial region and uses the system trust store.
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Final

import boto3
from botocore.config import Config
from botocore.credentials import CredentialResolver
from botocore.session import get_session
from strands.models import BedrockModel
from .env_security import load_protected_env


load_protected_env(Path(__file__).resolve().parents[2] / ".env")

_DEFAULT_REGION: Final = "us-east-1"
_AWS_REGION_PATTERN: Final = re.compile(
    r"^(?:af|ap|ca|eu|il|me|mx|sa|us)-"
    r"(?:central|east|north|northeast|northwest|south|southeast|southwest|west)-"
    r"[1-9][0-9]*$"
)
_MODEL_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9._:/-]{1,256}$")
_SERVICE_ENDPOINT_PREFIX: Final = {
    "bedrock": "bedrock",
    "bedrock-runtime": "bedrock-runtime",
}
_REGION_DISCOVERY_SERVICE: Final = {
    "bedrock": "bedrock",
    # Botocore's endpoint metadata lists the Bedrock regions under the
    # management-plane service. Runtime availability follows the same regions.
    "bedrock-runtime": "bedrock",
}


def _bounded_timeout(value: object) -> float:
    """Return a finite AWS timeout in the supported operational range."""

    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("AWS read timeout must be numeric") from exc
    if not math.isfinite(timeout) or not 1 <= timeout <= 300:
        raise ValueError("AWS read timeout must be between 1 and 300 seconds")
    return timeout


def validate_aws_region(region_name: object, *, service_name: str) -> str:
    """Validate a commercial AWS region against local Botocore endpoint data."""

    if service_name not in _REGION_DISCOVERY_SERVICE:
        raise ValueError("Unsupported AWS service")
    if not isinstance(region_name, str) or not _AWS_REGION_PATTERN.fullmatch(
        region_name
    ):
        raise ValueError("AWS region is invalid")

    discovery_service = _REGION_DISCOVERY_SERVICE[service_name]
    available_regions = set(
        get_session().get_available_regions(
            discovery_service,
            partition_name="aws",
        )
    )
    if region_name not in available_regions:
        raise ValueError(
            f"AWS region is not available for {service_name}"
        )
    return region_name


def _resolve_region(region_name: object | None, *, service_name: str) -> str:
    candidate = (
        region_name
        if region_name is not None
        else os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or _DEFAULT_REGION
    )
    return validate_aws_region(candidate, service_name=service_name)


def _official_endpoint(service_name: str, region_name: str) -> str:
    try:
        prefix = _SERVICE_ENDPOINT_PREFIX[service_name]
    except KeyError as exc:
        raise ValueError("Unsupported AWS service") from exc
    return f"https://{prefix}.{region_name}.amazonaws.com"


def _client_config(read_timeout_seconds: object) -> Config:
    read_timeout = _bounded_timeout(read_timeout_seconds)
    return Config(
        connect_timeout=min(read_timeout, 5),
        read_timeout=read_timeout,
        retries={"total_max_attempts": 3, "mode": "standard"},
        proxies={},
        ignore_configured_endpoint_urls=True,
        tcp_keepalive=True,
    )


def _static_environment_credentials() -> tuple[str, str, str | None] | None:
    """Read static credentials only; never activate an outbound provider."""

    access_key = os.getenv("AWS_ACCESS_KEY_ID")
    secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
    session_token = os.getenv("AWS_SESSION_TOKEN")

    if access_key is None and secret_key is None and session_token is None:
        return None
    if access_key is None or secret_key is None:
        raise ValueError(
            "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY must be set together"
        )

    for name, value, maximum in (
        ("AWS_ACCESS_KEY_ID", access_key, 256),
        ("AWS_SECRET_ACCESS_KEY", secret_key, 4_096),
        ("AWS_SESSION_TOKEN", session_token, 16_384),
    ):
        if value is None:
            continue
        if (
            not value
            or value != value.strip()
            or len(value) > maximum
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError(f"{name} is invalid")
    return access_key, secret_key, session_token


def _credential_isolated_session(region_name: str) -> boto3.Session:
    """Build a session that cannot invoke STS, process, ECS, or IMDS providers."""

    botocore_session = get_session()
    botocore_session.register_component(
        "credential_provider",
        CredentialResolver([]),
    )
    credentials = _static_environment_credentials()
    if credentials is not None:
        access_key, secret_key, session_token = credentials
        botocore_session.set_credentials(
            access_key,
            secret_key,
            session_token,
        )

    # A credentialless session is safe to construct for dry-run/test paths. An
    # attempted AWS call fails with NoCredentialsError without consulting role
    # profiles, web identity, credential_process, ECS, or IMDS.
    return boto3.Session(
        botocore_session=botocore_session,
        region_name=region_name,
    )


class _SecureBotoSession(boto3.Session):
    """A Boto session that permits one fixed service and endpoint only."""

    def __init__(
        self,
        *,
        service_name: str,
        region_name: str,
        read_timeout_seconds: object,
    ) -> None:
        self._secure_service_name = service_name
        self._secure_region_name = region_name
        self._secure_endpoint_url = _official_endpoint(
            service_name,
            region_name,
        )
        self._secure_client_config = _client_config(read_timeout_seconds)
        isolated_session = _credential_isolated_session(region_name)
        super().__init__(
            botocore_session=isolated_session._session,
            region_name=region_name,
        )

    def client(self, service_name: str, *args, **kwargs):
        """Create the single pre-authorized client with immutable transport."""

        if args or service_name != self._secure_service_name:
            raise ValueError("AWS service is not authorized for this session")

        incoming_config = kwargs.get("config")
        user_agent_extra = (
            getattr(incoming_config, "user_agent_extra", None)
            if incoming_config is not None
            else None
        )
        client_config = self._secure_client_config
        if user_agent_extra:
            client_config = client_config.merge(
                Config(user_agent_extra=user_agent_extra)
            )

        return super().client(
            service_name=service_name,
            region_name=self._secure_region_name,
            endpoint_url=self._secure_endpoint_url,
            verify=True,
            config=client_config,
        )


def create_secure_aws_client(
    service_name: str,
    *,
    region_name: object | None = None,
    read_timeout_seconds: object = 120,
):
    """Create a verified, no-proxy Bedrock client for an official endpoint."""

    region = _resolve_region(region_name, service_name=service_name)
    session = _SecureBotoSession(
        service_name=service_name,
        region_name=region,
        read_timeout_seconds=read_timeout_seconds,
    )
    return session.client(service_name)


def create_bedrock_model(
    model_id: object,
    *,
    region_name: object | None = None,
    read_timeout_seconds: object = 120,
) -> BedrockModel:
    """Create a Strands Bedrock model backed by the hardened runtime client."""

    if not isinstance(model_id, str) or not _MODEL_ID_PATTERN.fullmatch(
        model_id
    ):
        raise ValueError("Bedrock model ID is invalid")

    region = _resolve_region(region_name, service_name="bedrock-runtime")
    session = _SecureBotoSession(
        service_name="bedrock-runtime",
        region_name=region,
        read_timeout_seconds=read_timeout_seconds,
    )
    return BedrockModel(
        boto_session=session,
        boto_client_config=_client_config(read_timeout_seconds),
        endpoint_url=_official_endpoint("bedrock-runtime", region),
        model_id=model_id,
    )
