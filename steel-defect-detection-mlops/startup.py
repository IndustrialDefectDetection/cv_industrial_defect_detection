#!/usr/bin/env python3

import os
import subprocess
import sys
from pathlib import Path

from deployment.model_integrity import verify_model_integrity


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

    # Locate Python inside the venv
    if os.name == "nt":
        python = venv_dir / "Scripts" / "python.exe"
    else:
        python = venv_dir / "bin" / "python"

    # Install the exact top-level UI runtime instead of the much larger
    # floating training/notebook environment.
    print("\nInstalling pinned Streamlit requirements...")
    run([
        str(python),
        "-m",
        "pip",
        "install",
        "--requirement",
        "deployment/requirements-streamlit.txt",
    ])

    # PyTorch model files are executable serialized artifacts. Never download
    # or deserialize an unreviewed replacement automatically.
    weights = (
        project_dir
        / "runs"
        / "detect"
        / "steel_defect_colab_50_epochs"
        / "weights"
        / "best.pt"
    )

    try:
        verify_model_integrity(weights)
    except RuntimeError as exc:
        raise SystemExit(
            f"Model verification failed: {exc}. Copy the reviewed best.pt "
            "artifact into the documented weights path."
        ) from exc
    print("\nModel weights passed SHA-256 verification.")

    # Launch Streamlit
    print("\nLaunching Streamlit application...")
    run([
        str(python),
        "-m",
        "streamlit",
        "run",
        "streamlit_app.py",
        "--server.address",
        "127.0.0.1",
        "--server.headless",
        "true",
    ])


if __name__ == "__main__":
    main()
