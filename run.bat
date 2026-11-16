@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo Setting up for the first time...
    py -3 -m venv .venv || exit /b 1
    .venv\Scripts\python.exe -m pip install -r requirements.txt || exit /b 1
)
.venv\Scripts\python.exe dictate.py %*
pause
