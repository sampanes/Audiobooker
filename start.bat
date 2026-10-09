@echo off
rem Take Recorder launcher. Safe to run twice: it stops any earlier copy first.
rem First run creates .venv and installs the pinned dependencies.
setlocal
cd /d "%~dp0"
set "NAME=take-recorder"

call "%~dp0stop.bat" quiet

if not exist ".venv\Scripts\python.exe" (
    echo [setup] Creating virtual environment...
    python -m venv .venv || goto :fail
)
fc /b requirements.txt ".venv\requirements.installed" >nul 2>&1
if errorlevel 1 (
    echo [setup] Installing pinned dependencies...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto :fail
    copy /y requirements.txt ".venv\requirements.installed" >nul
)

start "%NAME%" /min ".venv\Scripts\python.exe" app.py
echo [OK] %NAME% started. Stop it with stop.bat or by closing its window.
exit /b 0

:fail
echo [X] Setup failed -- see the messages above.
pause
exit /b 1
