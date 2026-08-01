from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
LAUNCH_PATH = ROOT_DIR / "Launch.py"
SPEC = importlib.util.spec_from_file_location("root_launch", LAUNCH_PATH)
assert SPEC is not None and SPEC.loader is not None
launch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launch)


def _prepare_frontend(tmp_path: Path, marker_value: str) -> tuple[Path, str]:
    frontend = tmp_path / "frontend"
    next_binary = frontend / "node_modules" / ".bin" / "next"
    next_binary.parent.mkdir(parents=True)
    next_binary.touch()
    lockfile = frontend / "package-lock.json"
    lockfile.write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
    expected_hash = hashlib.sha256(lockfile.read_bytes()).hexdigest()
    marker = frontend / "node_modules" / ".mes-package-lock.sha256"
    marker.write_text(marker_value, encoding="ascii")
    return frontend, expected_hash


def test_frontend_dependencies_are_reused_only_for_the_current_lock(
    monkeypatch,
    tmp_path,
):
    frontend, expected_hash = _prepare_frontend(tmp_path, "")
    (frontend / "node_modules" / ".mes-package-lock.sha256").write_text(
        expected_hash + "\n",
        encoding="ascii",
    )
    monkeypatch.setattr(launch, "FRONTEND_DIR", frontend)

    def unexpected_install(*_args, **_kwargs):
        raise AssertionError("a matching lockfile must not reinstall")

    monkeypatch.setattr(launch.subprocess, "run", unexpected_install)
    assert launch.manage_frontend_dependencies() is True


def test_stale_frontend_dependencies_use_safe_locked_install(
    monkeypatch,
    tmp_path,
):
    frontend, expected_hash = _prepare_frontend(tmp_path, "stale\n")
    monkeypatch.setattr(launch, "FRONTEND_DIR", frontend)
    calls = []

    def record_install(command, **kwargs):
        calls.append((command, kwargs))

    monkeypatch.setattr(launch.subprocess, "run", record_install)
    assert launch.manage_frontend_dependencies() is True

    command, kwargs = calls[0]
    assert command == [
        "npm",
        "ci",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
    ]
    assert kwargs["check"] is True
    assert kwargs["cwd"] == frontend
    assert "ANTHROPIC_API_KEY" not in kwargs["env"]
    assert "AWS_SECRET_ACCESS_KEY" not in kwargs["env"]
    assert (
        frontend / "node_modules" / ".mes-package-lock.sha256"
    ).read_text(encoding="ascii").strip() == expected_hash


def test_child_services_receive_only_their_own_secrets():
    source = {
        "PATH": "/safe/bin",
        "ANTHROPIC_API_KEY": "anthropic-secret",
        "AWS_ACCESS_KEY_ID": "aws-access",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "BETTER_AUTH_SECRET": "auth-secret",
        "DATABASE_URL": "postgresql://frontend-secret",
        "GOOGLE_CLIENT_SECRET": "google-secret",
        "MES_INTERNAL_API_TOKEN": "t" * 32,
        "MES_PG_PASSWORD": "postgres-secret",
        "MODEL_SHA256": "a" * 64,
        "UNRELATED_SECRET": "must-never-propagate",
    }

    backend = launch.service_environment("backend", source)
    bridge = launch.service_environment("bridge", source)
    frontend = launch.service_environment("frontend", source)
    inference = launch.service_environment("inference", source)
    viewer = launch.service_environment("viewer", source)

    assert backend["ANTHROPIC_API_KEY"] == "anthropic-secret"
    assert backend["AWS_SECRET_ACCESS_KEY"] == "aws-secret"
    assert "BETTER_AUTH_SECRET" not in backend
    assert "DATABASE_URL" not in backend

    assert bridge["MES_PG_PASSWORD"] == "postgres-secret"
    assert bridge["MES_INTERNAL_API_TOKEN"] == "t" * 32
    assert "ANTHROPIC_API_KEY" not in bridge
    assert "BETTER_AUTH_SECRET" not in bridge

    assert frontend["BETTER_AUTH_SECRET"] == "auth-secret"
    assert frontend["DATABASE_URL"] == "postgresql://frontend-secret"
    assert frontend["GOOGLE_CLIENT_SECRET"] == "google-secret"
    assert "ANTHROPIC_API_KEY" not in frontend
    assert "AWS_SECRET_ACCESS_KEY" not in frontend
    assert "MES_PG_PASSWORD" not in frontend

    assert inference["MES_INTERNAL_API_TOKEN"] == "t" * 32
    assert inference["MODEL_SHA256"] == "a" * 64
    assert "DATABASE_URL" not in inference
    assert "MES_PG_PASSWORD" not in inference
    assert "UNRELATED_SECRET" not in inference

    assert viewer["MES_INTERNAL_API_TOKEN"] == "t" * 32
    assert "ANTHROPIC_API_KEY" not in viewer
    assert "AWS_SECRET_ACCESS_KEY" not in viewer
    assert "MES_PG_PASSWORD" not in viewer
    assert "DATABASE_URL" not in viewer


def _write_launch_env(tmp_path: Path, text: str) -> Path:
    env_file = tmp_path / ".env"
    env_file.write_text(text, encoding="utf-8")
    env_file.chmod(0o600)
    return env_file


def test_launch_env_file_feeds_the_frontend_its_secrets(tmp_path):
    """The frontend has no other way in: it may not hold an .env of its own."""
    env_file = _write_launch_env(
        tmp_path,
        "BETTER_AUTH_SECRET='auth-secret'\n"
        '# a comment\n'
        "\n"
        'DATABASE_URL="postgresql://frontend-secret"\n'
        "ANTHROPIC_API_KEY=anthropic-secret\n",
    )

    values = launch.load_launch_env_file(env_file)
    frontend = launch.service_environment("frontend", values)

    assert values["BETTER_AUTH_SECRET"] == "auth-secret"
    assert values["DATABASE_URL"] == "postgresql://frontend-secret"
    assert frontend["BETTER_AUTH_SECRET"] == "auth-secret"
    # The allowlist still applies to file-supplied values.
    assert "ANTHROPIC_API_KEY" not in frontend


def test_launch_env_file_is_optional(tmp_path):
    assert launch.load_launch_env_file(tmp_path / "absent.env") == {}


def test_real_environment_overrides_the_launch_env_file(tmp_path):
    """A one-off `NAME=value python Launch.py` must still win for that run."""
    env_file = _write_launch_env(tmp_path, "MES_MODEL_ID=from-file\n")

    merged = dict(launch.load_launch_env_file(env_file))
    merged.update({"MES_MODEL_ID": "from-shell"})

    assert merged["MES_MODEL_ID"] == "from-shell"


def test_launch_env_file_never_prints_its_values(tmp_path, capsys):
    env_file = _write_launch_env(tmp_path, "BETTER_AUTH_SECRET=super-secret\n")

    launch.load_launch_env_file(env_file)

    printed = capsys.readouterr().out
    assert "BETTER_AUTH_SECRET" in printed
    assert "super-secret" not in printed
