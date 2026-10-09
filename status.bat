@echo off
rem Is Take Recorder running? Exit code 0 = yes, 1 = no.
setlocal
cd /d "%~dp0"
set "NAME=take-recorder"
set "PID="
if exist "%NAME%.pid" set /p PID=<"%NAME%.pid"
if defined PID tasklist /FI "PID eq %PID%" | findstr /i "python" >nul && (
    echo [OK] %NAME% is running, pid %PID%.
    exit /b 0
)
echo [!] %NAME% is not running.
exit /b 1
