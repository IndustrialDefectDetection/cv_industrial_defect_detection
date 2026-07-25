import subprocess
import sys
from pathlib import Path
import webbrowser
import time
import os
import signal
import socket

#Runs using frontend UI, if needed run startup.py @BACKEND_DIR for streamlit.
ROOT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = ROOT_DIR / "sample-MES-ClosedLoop-Strands-Agent"
FRONTEND_DIR = ROOT_DIR / "frontend"


def backend_venv_python() -> Path:
    # Same venv startup.py creates; streamlit lives there, not in the launcher's Python.
    if os.name == "nt":
        return BACKEND_DIR / ".venv" / "Scripts" / "python.exe"
    return BACKEND_DIR / ".venv" / "bin" / "python"


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
            (("backend API", 8000), ("Next.js chat", 3000), ("trace dashboard", 8502))
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
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2):
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


def main():
   if not check_ports():
       sys.exit(1)
   backend_process =  subprocess.Popen(
        [
        sys.executable,
        str(BACKEND_DIR / "startup.py"),
        "--api"
    ],
    cwd = BACKEND_DIR,
    start_new_session=True
    )
   frontend_process = subprocess.Popen(
      [
         "npm",
         "run",
         "dev",
      ],
      cwd = FRONTEND_DIR,
      start_new_session=True,
      shell = (os.name == "nt")
   )
   # Under-the-hood trace viewer (TRACE_API.md) on port 8502.
   viewer_process = None
   venv_python = backend_venv_python()
   if venv_python.exists():
       viewer_process = subprocess.Popen(
          [
             str(venv_python),
             "-m",
             "streamlit",
             "run",
             "trace_viewer.py",
             "--server.port", "8502",
             "--server.headless", "true",
          ],
          cwd = BACKEND_DIR,
          start_new_session=True
       )
       print("Under-the-hood trace viewer: http://localhost:8502")
   else:
       print("Trace viewer not started: backend venv missing — rerun Launch.py once startup.py has finished installing.")
   # Wait for the backend to actually answer before opening the browser.
   # It builds six agents at startup (~10s), so a fixed 1s sleep opened the
   # dashboard onto a backend that was not listening yet.
   wait_for_backend()
   # One tab only: the dashboard has a chat box built in, so it's the whole
   # workflow. The Next.js chat still runs at localhost:3000 (printed above)
   # for anyone who prefers it.
   if viewer_process is not None:
       webbrowser.open("http://localhost:8502")
   else:
       webbrowser.open("http://localhost:3000")
   print("Chat UI (optional): http://localhost:3000")
   # The backend is essential; the two UIs are not. If a UI dies (e.g. `next`
   # missing because npm install was never run), say so and keep the rest
   # alive instead of silently tearing everything down.
   ui_processes = {"Next.js chat (port 3000)": frontend_process}
   if viewer_process is not None:
       ui_processes["trace dashboard (port 8502)"] = viewer_process
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
               print("Both UIs have exited - shutting down.")
               break
           time.sleep(1)
   except KeyboardInterrupt:
       print("Stopping application...")
   finally:
       stop(frontend_process)
       stop(backend_process)
       stop(viewer_process)


if(__name__ == "__main__"):
   main()
