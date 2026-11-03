@echo off
rem Plugin probe: can Auto-Tune, Graillon, Melda, Valhalla, iZotope... run
rem outside FL Studio? Loads each one, plays a test voice through it, and
rem writes plugin-probe\report-<date>.md plus the processed audio.
rem Pass --all to probe every VST3 plugin, not just the vocal ones.
setlocal
cd /d "%~dp0"

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

".venv\Scripts\python.exe" tools\probe_plugins.py %*
echo.
pause
exit /b 0

:fail
echo [X] Setup failed -- see the messages above.
pause
exit /b 1
