import subprocess
import sys
from pathlib import Path
import webbrowser
import time
import os
import hashlib
import secrets
import signal
import socket

#Runs using frontend UI, if needed run startup.py @BACKEND_DIR for streamlit.
ROOT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = ROOT_DIR / "sample-MES-ClosedLoop-Strands-Agent"
FRONTEND_DIR = ROOT_DIR / "frontend"
CHATBOT_DIR = ROOT_DIR / "industrial-data-store-simulation-chatbot"
MLOPS_DIR = ROOT_DIR / "steel-defect-detection-mlops"
MODEL_WEIGHTS = MLOPS_DIR / "runs/detect/steel_defect_colab_50_epochs/weights/best.pt"

LAUNCH_ENV_FILE = ROOT_DIR / ".env"

_BASE_CHILD_ENV_NAMES = frozenset({
    "APPDATA",
    "CI",
    "COLORTERM",
    "COMSPEC",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "LOGNAME",
    "NO_COLOR",
    "PATH",
    "PATHEXT",
    "SHELL",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "TZ",
    "USER",
    "USERPROFILE",
    "WINDIR",
    "XDG_CACHE_HOME",
})
_SERVICE_ENV_NAMES = {
    "backend": frozenset({
        "ANTHROPIC_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_DEFAULT_REGION",
        "AWS_REGION",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }),
    "bridge": frozenset({
        "MES_AGENT_TIMEOUT",
        "MES_AGENT_URL",
        "MES_ANALYZE_STUB",
        "MES_INTERNAL_API_TOKEN",
    }),
    "frontend": frozenset({
        "AUTH_ALLOW_EMAIL_SIGNUP",
        "AUTH_ALLOW_GOOGLE_SIGNUP",
        "AUTH_TRUSTED_PROXY_SECRET",
        "BACKEND_URL",
        "BETTER_AUTH_SECRET",
        "BETTER_AUTH_URL",
        "DATABASE_CA_CERT",
        "DATABASE_URL",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "MES_INTERNAL_API_TOKEN",
        "NEXT_TELEMETRY_DISABLED",
        "NODE_ENV",
    }),
    "inference": frozenset({
        "MES_INTERNAL_API_TOKEN",
        "MES_INFERENCE_CONCURRENCY",
        "MES_MAX_BATCH_BYTES",
        "MES_MAX_BATCH_FILES",
        "MES_MAX_IMAGE_BYTES",
        "MES_MAX_IMAGE_PIXELS",
        "MODEL_PATH",
        "MODEL_SHA256",
    }),
    "viewer": frozenset({
        "MES_AGENT_URL",
        "MES_BRIDGE_URL",
        "MES_INTERNAL_API_TOKEN",
        "MES_VIEWER_HEALTH_MAX_RETRIES",
        "MES_VIEWER_HEALTH_RETRY",
    }),
}
_SERVICE_ENV_PREFIXES = {
    "backend": ("MES_",),
    "bridge": ("MES_BRIDGE_", "MES_PG_"),
    "frontend": ("NEXT_PUBLIC_",),
    "inference": (),
    "viewer": (),
}


def service_environment(
    service: str,
    source: dict[str, str] | None = None,
) -> dict[str, str]:
    """Give each child only its runtime settings, not every parent secret."""

    if service not in _SERVICE_ENV_NAMES:
        raise ValueError(f"Unknown service environment: {service}")
    source_environment = dict(os.environ if source is None else source)
    allowed_names = _BASE_CHILD_ENV_NAMES | _SERVICE_ENV_NAMES[service]
    allowed_prefixes = _SERVICE_ENV_PREFIXES[service]
    child_environment = {
        name: value
        for name, value in source_environment.items()
        if name in allowed_names
        or any(name.startswith(prefix) for prefix in allowed_prefixes)
    }
    child_environment.setdefault("PYTHONIOENCODING", "utf-8")
    return child_environment


def load_launch_env_file(env_path: Path = LAUNCH_ENV_FILE) -> dict:
    """Read the launcher's own .env, which every service draws its share from.

    Each sub-project already loads its own .env once it is running. The
    frontend cannot: `frontend/scripts/run-next-secure.mjs` refuses to start if
    an .env file exists there at all on Windows, because it cannot verify the
    file's permissions, and tells the operator to supply the values through the
    process environment instead. This is that process environment - one
    gitignored file at the repo root, filtered per service by the allowlists
    above, so BETTER_AUTH_SECRET and DATABASE_URL reach Next.js without ever
    being written inside frontend/.

    Real environment variables win, so `MES_MODEL_ID=... python Launch.py`
    still overrides the file for one run.
    """

    if not env_path.exists():
        return {}

    if env_path.is_symlink() or not env_path.is_file():
        print(f"Refusing to read {env_path.name}: not a regular file")
        sys.exit(1)

    if os.name == "posix":
        # Same standard the sub-projects hold their own .env files to. Windows
        # has no equivalent bit to check; the file inherits the profile's ACL.
        mode = env_path.stat().st_mode & 0o077
        if mode:
            print(
                f"Refusing to read {env_path.name}: it is readable or writable "
                "by other users. Run: chmod 600 .env"
            )
            sys.exit(1)

    values = {}
    for line_number, raw_line in enumerate(
        env_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator:
            print(f"Ignoring {env_path.name} line {line_number}: no '=' found")
            continue
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name:
            values[name] = value

    if values:
        # Names only. Printing a value would put a secret in the terminal
        # scrollback and in any log the operator pastes into a bug report.
        print(f"Loaded {len(values)} values from {env_path.name}: "
              f"{', '.join(sorted(values))}")
    return values


def venv_python(project_dir: Path) -> Path:
    """The interpreter inside a sub-project's own venv.

    Each sub-project keeps its own: the agent venv has streamlit and psycopg2,
    the mlops one has torch and ultralytics. Deliberately not shared - see
    CLAUDE.md on the three toolchains.
    """
    if os.name == "nt":
        return project_dir / ".venv" / "Scripts" / "python.exe"
    return project_dir / ".venv" / "bin" / "python"


def backend_venv_python() -> Path:
    # Same venv startup.py creates; streamlit lives there, not in the launcher's Python.
    return venv_python(BACKEND_DIR)


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            # Windows reports a port held exclusively by another process as
            # WinError 10013 (access forbidden), not "address in use", which
            # makes uvicorn's raw traceback very hard to read.
            return False


def check_ports() -> bool:
    """Refuse to start when a required port is taken, and say which."""
    busy = [(name, port) for name, port in
            (("backend API", 8000), ("Next.js chat", 3000),
             ("trace dashboard", 8502), ("detection bridge", 8081),
             ("inference API", 8080))
            if not port_is_free(port)]
    if not busy:
        return True
    print("Cannot start - these ports are already in use:")
    for name, port in busy:
        print(f"  port {port} ({name})")
    print("\nSomething is already running - most likely another copy of Launch.py,")
    print("or a leftover server from an earlier session. Close it and try again.")
    print("To see what is holding a port:")
    print("  Get-NetTCPConnection -LocalPort 8000 | Select-Object OwningProcess")
    return False


def wait_for_backend(timeout: int = 90) -> bool:
    """Block until GET /health answers, so the UI never opens onto a dead API.

    Uses urllib rather than requests: this launcher runs on the system
    Python, which need not have the backend venv's dependencies.
    """
    import urllib.request

    print("Waiting for the backend to finish starting", end="", flush=True)
    # Never let ambient HTTP(S)_PROXY settings route a loopback readiness
    # check through an external proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with opener.open("http://127.0.0.1:8000/health", timeout=2):
                print(" ready.")
                return True
        except Exception:
            print(".", end="", flush=True)
            time.sleep(1)
    print(f"\nBackend did not answer within {timeout}s - opening the UI anyway;"
          " it will keep retrying on its own.")
    return False


def stop(process):
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        # os.killpg doesn't exist on Windows; taskkill /T takes the whole tree
        # (npm -> node, streamlit workers) so nothing is orphaned.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    process.wait()

def manage_frontend_dependencies():
    lockfile = FRONTEND_DIR / "package-lock.json"
    dependency_marker = (
        FRONTEND_DIR / "node_modules" / ".mes-package-lock.sha256"
    )
    bin_dir = FRONTEND_DIR / "node_modules" / ".bin"
    next_installed = (
        (bin_dir / "next").exists()
        or (bin_dir / "next.cmd").exists()
    )
    if not lockfile.is_file():
        print(f"Frontend lockfile is missing: {lockfile}")
        return False

    lockfile_hash = hashlib.sha256(lockfile.read_bytes()).hexdigest()
    try:
        installed_hash = dependency_marker.read_text(encoding="ascii").strip()
    except (FileNotFoundError, OSError, UnicodeError):
        installed_hash = ""

    if next_installed and secrets.compare_digest(
        installed_hash,
        lockfile_hash,
    ):
        return True

    print("Installing locked frontend dependencies")
    try:
        subprocess.run(
            [
                "npm",
                "ci",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
            ],
            cwd=FRONTEND_DIR,
            env={
                name: value
                for name, value in service_environment(
                    "frontend",
                ).items()
                if name in _BASE_CHILD_ENV_NAMES
                or name == "PYTHONIOENCODING"
            },
            check=True,
            shell=(os.name == "nt"),
        )
        dependency_marker.write_text(
            lockfile_hash + "\n",
            encoding="ascii",
        )
        return True
    except (subprocess.CalledProcessError, OSError) as e:
        print(f"Failed to install frontend dependencies: {e}")
    except FileNotFoundError:
        print("npm not found. Please install Node.js and npm.")
    return False

def migrate_frontend_database(frontend_env):
    """Create the frontend's tables if they are not there yet.

    Better Auth ships no schema file - it derives one from its options - so a
    database that has never run this has no "user", "session", "account" or
    "verification" table, and every sign-up fails on the first query. The
    browser reports only "Authentication failed", which reads like a rejected
    password rather than an empty database. The same applies to the chat
    history tables, whose SQL previously had no runner at all.

    Idempotent, so it costs a fraction of a second on every subsequent launch.
    """
    print("Checking frontend database tables")
    try:
        subprocess.run(
            ["npm", "run", "db:migrate"],
            cwd=FRONTEND_DIR,
            env=frontend_env,
            check=True,
            shell=(os.name == "nt"),
        )
        return True
    except subprocess.CalledProcessError:
        # Starting Next.js anyway would produce the exact confusing failure
        # this function exists to prevent.
        print("Frontend database migration failed; check DATABASE_URL.")
    except (OSError, FileNotFoundError) as e:
        print(f"Frontend database migration could not run: {e}")
    return False

def main():
   if not check_ports():
       sys.exit(1)
   # Everything speaks to the same PostgreSQL database. The camera pipeline
   # writes detections there, so an agent left on SQLite would look in the
   # wrong place and report that no defects were found.
   if not manage_frontend_dependencies():
        sys.exit(1)
   parent_env = dict(load_launch_env_file())
   parent_env.update(os.environ)
   parent_env.setdefault("MES_DB_BACKEND", "postgres")
   if len(parent_env.get("MES_INTERNAL_API_TOKEN", "")) < 32:
       # One high-entropy secret authenticates every loopback-only service.
       # startup.py persists the inherited value so manually started clients
       # can use the same token after this launcher exits.
       parent_env["MES_INTERNAL_API_TOKEN"] = secrets.token_urlsafe(32)

   backend_env = service_environment("backend", parent_env)
   bridge_env = service_environment("bridge", parent_env)
   frontend_env = service_environment("frontend", parent_env)
   inference_env = service_environment("inference", parent_env)
   viewer_env = service_environment("viewer", parent_env)

   # Before anything starts, so a failure here exits cleanly rather than
   # leaving a backend running with no launcher to shut it down.
   if not migrate_frontend_database(frontend_env):
       sys.exit(1)

   backend_process =  subprocess.Popen(
        [
        sys.executable,
        str(BACKEND_DIR / "startup.py"),
        "--api"
    ],
    cwd = BACKEND_DIR,
    env = backend_env,
    start_new_session=True
    )
   frontend_process = subprocess.Popen(
      [
         "npm",
         "run",
         "dev",
      ],
      cwd = FRONTEND_DIR,
      env = frontend_env,
      start_new_session=True,
      shell = (os.name == "nt")
   )
   # The camera itself: YOLOv8 inference on port 8080, which the simulator
   # POSTs images to. Runs from the mlops venv - that is the only one with
   # torch and ultralytics, and mixing them into the agent venv would break
   # the toolchain separation CLAUDE.md asks for.
   inference_process = None
   mlops_python = venv_python(MLOPS_DIR)
   if not mlops_python.exists():
       print("Inference API not started: no venv in steel-defect-detection-mlops.")
       print("  python -m venv .venv && .venv/Scripts/activate")
       print("  pip install ultralytics fastapi uvicorn python-multipart prometheus-client")
   elif not MODEL_WEIGHTS.exists():
       # The API would start and then 503 every /predict, which looks like a
       # broken bridge rather than a missing file.
       print(f"Inference API not started: model weights missing at {MODEL_WEIGHTS}.")
       print("  Train on Colab, or copy best.pt across from whoever has it.")
   else:
       inference_process = subprocess.Popen(
          [
             str(mlops_python),
             "-m", "uvicorn",
             "deployment.api:app",
             "--host", "127.0.0.1",
             "--port", "8080",
          ],
          cwd = MLOPS_DIR,
          env = inference_env,
          start_new_session=True
       )
       print("Inference API: http://localhost:8080  (docs at /docs)")

   # The camera-side bridge (CONTRACTS.md): receives detections, applies the
   # 0.80 gate, batches them, and hands each burst to the agent. Runs from the
   # backend venv because that is where psycopg2 and fastapi are installed.
   bridge_process = None
   backend_python = backend_venv_python()
   if backend_python.exists():
       bridge_process = subprocess.Popen(
          [
             str(backend_python),
             "-m", "uvicorn",
             "bridge.bridge:app",
             "--host", "127.0.0.1",
             "--port", "8081",
          ],
          cwd = CHATBOT_DIR,
          env = bridge_env,
          start_new_session=True
       )
       print("Detection bridge: http://localhost:8081")

   # Under-the-hood trace viewer (TRACE_API.md) on port 8502.
   viewer_process = None
   if backend_python.exists():
       viewer_process = subprocess.Popen(
          [
             str(backend_python),
             "-m",
             "streamlit",
             "run",
             "trace_viewer.py",
             "--server.port", "8502",
             "--server.address", "127.0.0.1",
             "--server.headless", "true",
          ],
          cwd = BACKEND_DIR,
          env = viewer_env,
          start_new_session=True
       )
       print("Under-the-hood trace viewer: http://localhost:8502")
   else:
       print("Trace viewer not started: backend venv missing — rerun Launch.py once startup.py has finished installing.")
   # Wait for the backend to actually answer before opening the browser.
   # It builds six agents at startup (~10s), so a fixed 1s sleep opened the
   # dashboard onto a backend that was not listening yet.
   wait_for_backend()
   # Open the chat, because that is where you ask the system something. The
   # dashboard used to carry a chat box of its own, which is why this used to
   # open the dashboard instead - it now holds only the dropdowns and the Run
   # Analysis button, so opening it left the actual chatbot unopened and
   # looking like it had not started.
   if frontend_process.poll() is None:
       webbrowser.open("http://localhost:3000")
   elif viewer_process is not None:
       webbrowser.open("http://localhost:8502")
   print("\nChat with the agents:   http://localhost:3000")
   print("Watch them work:        http://localhost:8502  (live trace, "
         "defect-type dropdown, Run Analysis)")
   # The backend is essential; the two UIs are not. If a UI dies (e.g. `next`
   # missing because npm install was never run), say so and keep the rest
   # alive instead of silently tearing everything down.
   ui_processes = {"Next.js chat (port 3000)": frontend_process}
   if viewer_process is not None:
       ui_processes["trace dashboard (port 8502)"] = viewer_process
   if bridge_process is not None:
       ui_processes["detection bridge (port 8081)"] = bridge_process
   if inference_process is not None:
       ui_processes["inference API (port 8080)"] = inference_process
   if inference_process is not None and bridge_process is not None:
       print("\nCamera demo - fire a burst from another terminal:")
       print(f"  cd {CHATBOT_DIR.name}")
       print(f"  {backend_python} -m bridge.simulator"
             f" --image-dir ../{MLOPS_DIR.name}/data/demo_burst --interval 0.5")
   try:
       while True:
           if backend_process.poll() is not None:
               print(f"Backend exited (code {backend_process.returncode}) - shutting down.")
               break
           for name, process in list(ui_processes.items()):
               if process.poll() is not None:
                   print(f"WARNING: {name} exited (code {process.returncode}) - "
                         "see messages above for why; everything else keeps running.")
                   del ui_processes[name]
           if not ui_processes:
               print("Every non-backend service has exited - shutting down.")
               break
           time.sleep(1)
   except KeyboardInterrupt:
       print("Stopping application...")
   finally:
       stop(frontend_process)
       stop(backend_process)
       stop(viewer_process)
       stop(bridge_process)
       stop(inference_process)


if(__name__ == "__main__"):
   main()
