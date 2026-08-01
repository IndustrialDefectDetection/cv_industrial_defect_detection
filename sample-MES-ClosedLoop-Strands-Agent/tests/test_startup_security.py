"""Focused checks for startup secret generation and local file protection."""

import importlib.util
import os
import stat
from pathlib import Path
from urllib.request import Request

import pytest

from env_security import load_protected_env, remove_cross_service_secrets


BACKEND_DIR = Path(__file__).resolve().parent.parent
STARTUP_PATH = BACKEND_DIR / "startup.py"


def _load_startup():
    spec = importlib.util.spec_from_file_location(
        "mes_startup_security_test",
        STARTUP_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_check_env_persists_inherited_token_and_repairs_mode(
    monkeypatch,
    tmp_path,
):
    startup = _load_startup()
    env_path = tmp_path / ".env"
    env_path.write_text(
        "ANTHROPIC_API_KEY=sk-ant-test-value-not-real\n"
        "MES_MODEL_ID=test-model\n"
        "MES_MAX_TOKENS=4096\n"
        "MES_TEMPERATURE=0.2\n",
        encoding="utf-8",
    )
    env_path.chmod(0o644)

    inherited_token = "A" * 32
    monkeypatch.setattr(startup, "ENV", env_path)
    monkeypatch.setenv("MES_INTERNAL_API_TOKEN", inherited_token)
    monkeypatch.setattr(startup, "validate_api_key", lambda _key: True)

    startup.check_env()

    _lines, env_vars = startup.parse_env_vars()
    assert env_vars["MES_INTERNAL_API_TOKEN"] == inherited_token
    assert os.environ["MES_INTERNAL_API_TOKEN"] == inherited_token
    if os.name != "nt":
        assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_check_env_generates_a_strong_internal_token(monkeypatch, tmp_path):
    startup = _load_startup()
    env_path = tmp_path / ".env"
    monkeypatch.setattr(startup, "ENV", env_path)
    monkeypatch.delenv("MES_INTERNAL_API_TOKEN", raising=False)
    monkeypatch.setattr(
        startup,
        "prompt_api_key",
        lambda: "sk-ant-test-value-not-real",
    )

    startup.check_env()

    _lines, env_vars = startup.parse_env_vars()
    token = env_vars["MES_INTERNAL_API_TOKEN"]
    assert startup._valid_internal_token(token)
    assert len(token.encode("ascii")) >= 32
    if os.name != "nt":
        assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_invalid_inherited_token_is_rejected(monkeypatch):
    startup = _load_startup()
    monkeypatch.setenv("MES_INTERNAL_API_TOKEN", "too-short")

    with pytest.raises(RuntimeError, match="MES_INTERNAL_API_TOKEN"):
        startup._ensure_internal_api_token({})


def test_api_key_prompt_uses_hidden_input(monkeypatch):
    startup = _load_startup()
    prompts = []
    fake_key = "sk-ant-test-value-not-real"
    monkeypatch.setattr(
        startup.getpass,
        "getpass",
        lambda prompt: prompts.append(prompt) or fake_key,
    )
    monkeypatch.setattr(startup, "validate_api_key", lambda _key: True)

    assert startup.prompt_api_key() == fake_key
    assert prompts and "hidden" in prompts[0]


def test_dependency_install_uses_pinned_binary_packages(monkeypatch):
    startup = _load_startup()
    calls = []
    monkeypatch.setattr(
        startup.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    startup.install_requirements(Path("/test/python"))

    command, kwargs = calls[0]
    assert "--only-binary=:all:" in command
    assert "--no-input" in command
    assert kwargs["check"] is True
    requirements = startup.REQUIREMENTS.read_text(encoding="utf-8")
    dependency_lines = [
        line for line in requirements.splitlines()
        if line and not line.startswith("#")
    ]
    assert dependency_lines
    assert all("==" in line for line in dependency_lines)


@pytest.mark.parametrize(
    "candidate",
    [
        "sk-ant-valid-looking\r\nInjected: header",
        "sk-ant-\N{SNOWMAN}" + "x" * 20,
        "sk-ant-" + "x" * 600,
    ],
)
def test_api_key_shape_rejects_header_unsafe_values(candidate):
    startup = _load_startup()

    assert startup._valid_api_key_shape(candidate) is False
    assert startup.validate_api_key(candidate) is False


def test_api_key_validation_never_follows_redirects():
    startup = _load_startup()
    original = Request(
        "https://api.anthropic.com/v1/models",
        headers={"x-api-key": "sk-ant-test-value-not-real"},
    )

    redirected = startup._NoRedirectHandler().redirect_request(
        original,
        None,
        302,
        "Found",
        {},
        "https://example.invalid/collect",
    )

    assert redirected is None


def test_network_validation_failure_exits_unsuccessfully(monkeypatch):
    startup = _load_startup()
    monkeypatch.setattr(
        startup.getpass,
        "getpass",
        lambda _prompt: "sk-ant-test-value-not-real",
    )
    monkeypatch.setattr(startup, "validate_api_key", lambda _key: None)

    with pytest.raises(SystemExit) as exc:
        startup.prompt_api_key()

    assert exc.value.code == 1


def test_standalone_streamlit_is_bound_to_loopback():
    startup = _load_startup()

    command = startup._streamlit_command(Path("/test/python"))

    address_index = command.index("--server.address")
    assert command[address_index + 1] == "127.0.0.1"


def test_streamlit_config_keeps_direct_invocations_on_loopback():
    config_path = BACKEND_DIR / ".streamlit" / "config.toml"
    config = config_path.read_text(encoding="utf-8")

    assert 'address = "127.0.0.1"' in config
    assert "enableCORS = true" in config
    assert "enableXsrfProtection = true" in config


def test_direct_env_loader_rejects_broad_permissions(monkeypatch, tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX file modes do not apply on Windows")
    env_path = tmp_path / ".env"
    env_path.write_text("MES_TEST_SECRET=not-real\n", encoding="utf-8")
    env_path.chmod(0o644)

    with pytest.raises(RuntimeError, match="other local accounts"):
        load_protected_env(env_path)

    monkeypatch.delenv("MES_TEST_SECRET", raising=False)
    env_path.chmod(0o600)
    assert load_protected_env(env_path) is True
    assert os.environ["MES_TEST_SECRET"] == "not-real"


def test_direct_env_loader_rejects_symlinks(tmp_path):
    target = tmp_path / "target.env"
    target.write_text("MES_TEST_SECRET=not-real\n", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / ".env"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Symbolic links are not supported in this environment")

    with pytest.raises(RuntimeError, match="non-regular"):
        load_protected_env(link)


def test_direct_env_loader_can_export_only_viewer_settings(
    monkeypatch, tmp_path
):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "MES_INTERNAL_API_TOKEN=" + ("t" * 32) + "\n"
        "ANTHROPIC_API_KEY=must-not-reach-viewer\n"
        "AWS_SECRET_ACCESS_KEY=must-not-reach-viewer\n",
        encoding="utf-8",
    )
    env_path.chmod(0o600)
    for name in (
        "MES_INTERNAL_API_TOKEN",
        "ANTHROPIC_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    assert load_protected_env(
        env_path,
        allowed_names=frozenset({"MES_INTERNAL_API_TOKEN"}),
    ) is True
    assert os.environ["MES_INTERNAL_API_TOKEN"] == "t" * 32
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "AWS_SECRET_ACCESS_KEY" not in os.environ


def test_viewer_can_remove_inherited_cross_service_credentials(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-reach-viewer")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-reach-viewer")
    monkeypatch.setenv("DATABASE_URL", "postgresql://must-not-reach-viewer")
    monkeypatch.setenv("MES_INTERNAL_API_TOKEN", "t" * 32)

    remove_cross_service_secrets()

    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "AWS_SECRET_ACCESS_KEY" not in os.environ
    assert "DATABASE_URL" not in os.environ
    assert os.environ["MES_INTERNAL_API_TOKEN"] == "t" * 32
