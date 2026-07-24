import subprocess
import sys
from pathlib import Path
import webbrowser
import time
import os
import signal

#Runs using frontend UI, if needed run startup.py @BACKEND_DIR for streamlit.
ROOT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = ROOT_DIR / "sample-MES-ClosedLoop-Strands-Agent"
FRONTEND_DIR = ROOT_DIR / "frontend"


def backend_venv_python() -> Path:
    # Same venv startup.py creates; streamlit lives there, not in the launcher's Python.
    if os.name == "nt":
        return BACKEND_DIR / ".venv" / "Scripts" / "python.exe"
    return BACKEND_DIR / ".venv" / "bin" / "python"


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
   #Wait for commands to run before opening browser
   time.sleep(1)
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
