import datetime
import getpass
import os
import secrets
import tempfile
import venv
from pathlib import Path
import subprocess
import hashlib
import ssl
from urllib.request import (
    HTTPSHandler,
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)
from urllib.error import URLError, HTTPError
import sys


PROJECT_DIR = Path(__file__).resolve().parent;
VENV_DIR = PROJECT_DIR / '.venv';
REQUIREMENTS = PROJECT_DIR / 'requirements.txt';
ENV = PROJECT_DIR / '.env';
_MIN_INTERNAL_TOKEN_BYTES = 32
_MAX_API_KEY_CHARS = 512
_DEFAULT_ENV_VARS = {
    "MES_MODEL_ID": "claude-sonnet-4-6",
    "MES_MAX_TOKENS": "4096",
    "MES_TEMPERATURE": "0.2",
}

def get_python_venv():
    # Windows python virtual environment location
    if(os.name == 'nt'):
        return VENV_DIR / "Scripts" / "python.exe";
    else:
        # Mac/Linux venv location
        return VENV_DIR / "bin" / "python";

def _valid_api_key_shape(value):
    """Reject malformed/header-unsafe values before making a network request."""
    if not isinstance(value, str):
        return False
    if not 20 <= len(value) <= _MAX_API_KEY_CHARS or not value.isascii():
        return False
    return value.startswith("sk-ant-") and all(
        character.isalnum() or character in "-_" for character in value
    )


class _NoRedirectHandler(HTTPRedirectHandler):
    """Never forward the Anthropic API key to a redirected origin."""

    def redirect_request(self, *_args, **_kwargs):
        return None


# Validates API Key
def validate_api_key(api_key):
    if not _valid_api_key_shape(api_key):
        return False
    url = "https://api.anthropic.com/v1/models"
    headers = {
        "x-api-key": api_key,
        #Latest version according to Anthropic API documentation. Must be updated if API version changes in the future.
        "anthropic-version": "2023-06-01"
    }
    request = Request(url, headers=headers)
    # Do not send the API key through proxy settings inherited from an
    # untrusted shell. HTTPSHandler keeps normal certificate verification.
    opener = build_opener(
        ProxyHandler({}),
        HTTPSHandler(context=ssl.create_default_context()),
        _NoRedirectHandler(),
    )
    try:
        with opener.open(request, timeout=10) as response:
            if response.status == 200:
                return True
    #Checks if API key is valid
    except HTTPError as e:
        if(e.code == 401):
            print("Invalid API key.")
            return False
        else:
            print("Unable to verify the API key because the service returned an error.")
            return None
    #Checks connection
    except (URLError, TimeoutError):
        print("Unable to verify the API key because the connection failed.")
        return None
    
def _valid_internal_token(value):
    """Accept only header-safe tokens containing at least 32 bytes of entropy."""
    if not isinstance(value, str) or not value.isascii():
        return False
    if not _MIN_INTERNAL_TOKEN_BYTES <= len(value.encode("ascii")) <= 512:
        return False
    return all(character.isalnum() or character in "-_" for character in value)


def _repair_env_permissions():
    """Keep secrets unreadable by other local accounts."""
    if not ENV.exists():
        return
    if ENV.is_symlink():
        raise RuntimeError(f"Refusing to use symlinked environment file: {ENV}")
    if os.name != "nt":
        ENV.chmod(0o600)


def _write_env_lines(lines):
    """Atomically replace .env with owner-only permissions."""
    ENV.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=ENV.parent,
            prefix=".env.",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.writelines(lines)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        if os.name != "nt":
            temporary_path.chmod(0o600)
        os.replace(temporary_path, ENV)
        temporary_path = None
        _repair_env_permissions()
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _render_env_lines(original_lines, env_vars):
    updated_lines = []
    written_keys = set()
    for line in original_lines:
        if line.strip().startswith("#") or "=" not in line or line.strip() == "":
            updated_lines.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in env_vars:
            updated_lines.append(f"{key}={env_vars[key]}\n")
            written_keys.add(key)
        else:
            updated_lines.append(line)
    for key, value in env_vars.items():
        if key not in written_keys:
            updated_lines.append(f"{key}={value}\n")
    return updated_lines


def _ensure_internal_api_token(env_vars):
    """Choose one token for every child and persist it without printing it."""
    inherited_token = os.getenv("MES_INTERNAL_API_TOKEN", "")
    stored_token = env_vars.get("MES_INTERNAL_API_TOKEN", "")

    if inherited_token:
        if not _valid_internal_token(inherited_token):
            raise RuntimeError(
                "Inherited MES_INTERNAL_API_TOKEN must be 32-512 bytes and "
                "contain only letters, numbers, '-' or '_'"
            )
        token = inherited_token
    elif _valid_internal_token(stored_token):
        token = stored_token
    else:
        token = secrets.token_urlsafe(_MIN_INTERNAL_TOKEN_BYTES)
        print("Generated a protected internal service token.")

    changed = stored_token != token
    env_vars["MES_INTERNAL_API_TOKEN"] = token
    os.environ["MES_INTERNAL_API_TOKEN"] = token
    return changed


def check_env():
    if not ENV.exists():
        print("Error: .env not found. Creating....")
        env_vars = {"ANTHROPIC_API_KEY": prompt_api_key(), **_DEFAULT_ENV_VARS}
        _ensure_internal_api_token(env_vars)
        _write_env_lines(
            [f"{key}={value}\n" for key, value in env_vars.items()]
        )
        return

    _repair_env_permissions()
    original_lines, env_vars = parse_env_vars()
    rewrite_env = False

    api_key = env_vars.get("ANTHROPIC_API_KEY", "")
    key_has_valid_shape = _valid_api_key_shape(api_key)
    valid_key = validate_api_key(api_key) if key_has_valid_shape else False
    if not valid_key:
        if valid_key is None:
            print("Unable to verify API key due to connection issues. Please check your internet connection and try again.")
            print("Stopping....")
            raise SystemExit(1)
        print("Error: ANTHROPIC_API_KEY not found or invalid in .env. Please update the .env file with a valid API key.")
        env_vars["ANTHROPIC_API_KEY"] = prompt_api_key()
        rewrite_env = True

    for key, value in _DEFAULT_ENV_VARS.items():
        if key not in env_vars:
            env_vars[key] = value
            rewrite_env = True

    rewrite_env = _ensure_internal_api_token(env_vars) or rewrite_env
    if rewrite_env:
        _write_env_lines(_render_env_lines(original_lines, env_vars))
    else:
        _repair_env_permissions()


# parses .env into dictionary of variables
def parse_env_vars():
    env_vars = {}
    original_lines = []
    with open(ENV, 'r', encoding="utf-8") as f:
        original_lines = f.readlines()
        for original_line in original_lines:
            # Ignore comments and lines without '='
            if "=" not in original_line or original_line.strip().startswith("#"):
                continue
            key, value = original_line.strip().split('=', 1)
            env_vars[key] = value
    return original_lines, env_vars


def prompt_api_key():
    while True:
        user_key = getpass.getpass(
            "Enter your ANTHROPIC_API_KEY (hidden; or type exit to quit): "
        ).strip()
        if user_key.lower() == "exit":
            print("Exiting...")
            raise SystemExit(0)
        if not _valid_api_key_shape(user_key):
            print("Invalid API key. Please try again.")
            continue

        valid_key = validate_api_key(user_key)
        if valid_key:
            return user_key
        if valid_key is None:
            print("Unable to verify API key due to connection issues. Please check your internet connection and try again.")
            print("Stopping....")
            raise SystemExit(1)
        print("Invalid API key. Please try again.")


# Installs requirements from requirements.txt
def install_requirements(python_venv):
    print("--Installing Requirements--")
    subprocess.run(
        [
         str(python_venv),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--only-binary=:all:",
            "-r",
            str(REQUIREMENTS)
        ],
        check=True
    );


def _streamlit_command(python_venv):
    """Build a local-only Streamlit command for the standalone UI."""
    return [
        str(python_venv),
        "-m",
        "streamlit",
        "run",
        "app.py",
        "--server.address",
        "127.0.0.1",
    ]


def main():
    api_mode = "--api" in sys.argv
    os.chdir(PROJECT_DIR);
    python_venv = get_python_venv();
    # rename incompaitble venv
    if VENV_DIR.exists() and not python_venv.exists():
        print("--Creating virtual environment--");
        # Timestamps to avoid collisions with previous backups
        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        VENV_DIR.rename(VENV_DIR.with_suffix(f'.venv_backup{timestamp}'));
    print("--Checking environment--")
    check_env();
    # create virtual environment if it doesn't exist
    if not python_venv.exists():
        print("--Creating virtual environment--")
        venv.create(VENV_DIR, with_pip=True);
    
    #Hashes requirements.txt and checks for changes to avoid unnecessary reinstallation of packages
    with open (REQUIREMENTS, 'rb') as f:
        requirements_content = f.read()
        requirements_hash = hashlib.sha256(requirements_content).hexdigest()
    hash_file = VENV_DIR / 'requirements_hash.txt'
    if hash_file.exists():
        with open(hash_file, 'r+') as f:
            current_hash = f.read()
            if current_hash != requirements_hash:
                print("--Requirements changed, reinstalling packages--")
                install_requirements(python_venv)
                f.seek(0)
                f.write(requirements_hash)
                f.truncate()
    else:
        print("--Installing requirements for the first time--")
        install_requirements(python_venv)
        with open(hash_file, 'w') as f:
            f.write(requirements_hash)

   
    # Connects to Next.Js frontend or streamlit
    if(api_mode):
        #Starts Next.Js
        print("--Connecting to UI--")
        # No --reload, deliberately. Two measured reasons:
        #  1. Its StatReload watcher polls this whole directory - including
        #     .venv's tens of thousands of files - several times a second,
        #     pegging a full CPU core for the entire life of the server
        #     (563 CPU-seconds burned over a 563-second run).
        #  2. On Windows it runs the app in a multiprocessing spawn worker,
        #     where agent runs intermittently wedge in the Strands SDK's
        #     asyncio teardown (ProactorEventLoop.close blocking in _poll),
        #     leaving the run never returning and the report never produced.
        # Reload was not useful here anyway: it watches .py files, not .env,
        # and .env changes need a full restart regardless.
        subprocess.run(
        [
            str(python_venv),
            "-m",
            "uvicorn",
            "api:app",
            "--host",
            "127.0.0.1",
        ],
        check=True
    );
    else:
        # run streamlit
        print("--Starting Streamlit--")
        subprocess.run(
        _streamlit_command(python_venv),
        check=True
    );

if(__name__ == "__main__"):
    main();
