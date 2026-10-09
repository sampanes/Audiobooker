@echo off
rem Stops Take Recorder. Asks the window to close first so a take in progress
rem is finalized, then force-kills after a few seconds if it is still alive.
rem Pass "quiet" to suppress output (start.bat does).
setlocal
cd /d "%~dp0"
set "NAME=take-recorder"
set "PID="
set "FOUND="

if exist "%NAME%.pid" set /p PID=<"%NAME%.pid"
if not defined PID goto :bytitle
tasklist /FI "PID eq %PID%" | findstr /i "python" >nul || goto :bytitle
set "FOUND=1"
taskkill /PID %PID% >nul 2>&1
ping -n 4 127.0.0.1 >nul
tasklist /FI "PID eq %PID%" | findstr /i "python" >nul && taskkill /PID %PID% /T /F >nul 2>&1

:bytitle
tasklist /FI "WINDOWTITLE eq %NAME%*" | findstr /i "python" >nul && (
    set "FOUND=1"
    taskkill /FI "WINDOWTITLE eq %NAME%*" /T /F >nul 2>&1
)
del "%NAME%.pid" >nul 2>&1

if /i "%~1"=="quiet" exit /b 0
if defined FOUND (echo [OK] %NAME% stopped.) else (echo [!] %NAME% was not running.)
exit /b 0
