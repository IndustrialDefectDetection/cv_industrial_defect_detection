"""Security tests for local environment-file loading."""

from __future__ import annotations

import os

import pytest

from app_factory.shared.env_security import load_protected_env


def test_owner_only_regular_env_file_loads(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("AWS_REGION=us-east-1\n", encoding="utf-8")
    env_file.chmod(0o600)
    monkeypatch.delenv("AWS_REGION", raising=False)

    assert load_protected_env(env_file) is True
    assert os.environ["AWS_REGION"] == "us-east-1"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission check")
def test_group_or_world_accessible_env_file_is_rejected(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AWS_SECRET_ACCESS_KEY=do-not-load-this-secret\n",
        encoding="utf-8",
    )
    env_file.chmod(0o644)

    with pytest.raises(RuntimeError, match="readable or writable"):
        load_protected_env(env_file)


def test_symlinked_env_file_is_rejected(tmp_path):
    target = tmp_path / "credentials"
    target.write_text(
        "AWS_SECRET_ACCESS_KEY=do-not-load-this-secret\n",
        encoding="utf-8",
    )
    target.chmod(0o600)
    env_file = tmp_path / ".env"
    env_file.symlink_to(target)

    with pytest.raises(RuntimeError, match="non-regular"):
        load_protected_env(env_file)


def test_allowlist_does_not_export_unrelated_secrets(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MES_INTERNAL_API_TOKEN=" + ("t" * 32) + "\n"
        "ANTHROPIC_API_KEY=must-not-be-inherited\n"
        "AWS_SECRET_ACCESS_KEY=must-not-be-inherited\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    for name in (
        "MES_INTERNAL_API_TOKEN",
        "ANTHROPIC_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    assert load_protected_env(
        env_file,
        allowed_names=frozenset({"MES_INTERNAL_API_TOKEN"}),
    ) is True
    assert os.environ["MES_INTERNAL_API_TOKEN"] == "t" * 32
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "AWS_SECRET_ACCESS_KEY" not in os.environ
