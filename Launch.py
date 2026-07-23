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
      start_new_session=True
   )
   #Wait for commands to run before opening browser
   time.sleep(1)
   webbrowser.open("http://localhost:3000")
   try:
       while backend_process.poll() is None and frontend_process.poll() is None:
           time.sleep(1)
   except KeyboardInterrupt:
       print("Stopping application...")
   finally:
       try:
           os.killpg(frontend_process.pid, signal.SIGTERM)
       except ProcessLookupError:
           pass
       try:
           os.killpg(backend_process.pid, signal.SIGTERM)
       except ProcessLookupError:
           pass
       frontend_process.wait()
       backend_process.wait()

       
       


if(__name__ == "__main__"):
   main()
   