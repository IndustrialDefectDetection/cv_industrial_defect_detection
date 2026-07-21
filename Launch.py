import subprocess
import sys
from pathlib import Path
import webbrowser
import time

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
    cwd = BACKEND_DIR
    )
   frontend_process = subprocess.Popen(
      [
         "npm",
         "run",
         "dev",
      ],
      cwd = FRONTEND_DIR
   )
   #Wait for commands to run before opening browser
   time.sleep(1)
   webbrowser.open("http://localhost:3000")

if(__name__ == "__main__"):
   main()