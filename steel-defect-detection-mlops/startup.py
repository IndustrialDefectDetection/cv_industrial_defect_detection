#!/usr/bin/env python3

import os
import subprocess
import sys
from pathlib import Path


def run(cmd, cwd=None):
    print(f"\n>>> {' '.join(cmd)}")
    subprocess.run(cmd, check=True, cwd=cwd)


def main():
    project_dir = Path(__file__).parent.resolve()
    os.chdir(project_dir)

    venv_dir = project_dir / "venv"

    # Create virtual environment
    if not venv_dir.exists():
        print("Creating virtual environment...")
        run([sys.executable, "-m", "venv", "venv"])
    else:
        print("Virtual environment already exists.")

    # Locate Python and pip inside the venv
    if os.name == "nt":
        python = venv_dir / "Scripts" / "python.exe"
        pip = venv_dir / "Scripts" / "pip.exe"
    else:
        python = venv_dir / "bin" / "python"
        pip = venv_dir / "bin" / "pip"

    # Upgrade pip
    print("\nUpgrading pip...")
    run([str(python), "-m", "pip", "install", "--upgrade", "pip"])

    # Install dependencies
    print("\nInstalling requirements...")
    run([str(pip), "install", "-r", "requirements.txt"])

    # Download model
    weights = (
        project_dir
        / "runs"
        / "detect"
        / "steel_defect_colab_50_epochs"
        / "weights"
        / "best.pt"
    )

    if weights.exists():
        print("\nModel weights already exist.")
    else:
        print("\nDownloading model weights...")
        run([str(python), "scripts/download_model.py"])

    # Launch Streamlit
    print("\nLaunching Streamlit application...")
    run([str(python), "-m", "streamlit", "run", "streamlit_app.py"])


if __name__ == "__main__":
    main()
