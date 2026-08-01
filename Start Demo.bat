@echo off
REM Double-click entry point for the demo launcher.
REM
REM Uses pythonw so no console window appears behind the app. If pythonw is
REM missing, falls back to python and says why, rather than flashing a window
REM shut and leaving nothing to read.

cd /d "%~dp0"

where pythonw >nul 2>&1
if %errorlevel%==0 (
    start "" pythonw "demo.py"
    exit /b 0
)

where python >nul 2>&1
if %errorlevel%==0 (
    echo pythonw was not found, so this console will stay open.
    python "demo.py"
    pause
    exit /b 0
)

echo Python was not found on your PATH.
echo Install Python 3.10 or newer from https://python.org and try again.
pause
exit /b 1
