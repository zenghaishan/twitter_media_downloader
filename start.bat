@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ==============================================
echo    Twitter Media Downloader  -  Quick Start
echo ==============================================
echo.

REM ---- 1. release port if occupied by a stale process ----
set PORT=12345
for /f "tokens=5" %%a in ('netstat -ano -p tcp ^| findstr "LISTENING" ^| findstr ":%PORT% "') do (
  if not "%%a"=="0" (
    echo [info] port %PORT% held by PID %%a, stopping it...
    taskkill /F /PID %%a >nul 2>&1
  )
)

REM ---- 2. locate python (PATH first, then known fallback path) ----
set "PY=python"
where python >nul 2>&1
if errorlevel 1 (
  if exist "%~dp0\.python_path.txt" (
    set /p PY=<"%~dp0\.python_path.txt"
  ) else (
    REM fallback: TRAE builtin python used on this machine
    if exist "C:\Users\Administrator\AppData\Roaming\TRAE SOLO CN\ModularData\ai-agent\vm\tools\python\python.exe" (
      set "PY=C:\Users\Administrator\AppData\Roaming\TRAE SOLO CN\ModularData\ai-agent\vm\tools\python\python.exe"
    )
  )
)

"%PY%" --version >nul 2>&1
if errorlevel 1 (
  echo [error] python not found. Please set python path in .python_path.txt
  pause
  exit /b 1
)

REM ---- 3. launch the service (keep console window to watch logs) ----
echo [start] launching service on http://127.0.0.1:%PORT% ...
echo [stop ] press Ctrl+C or close this window to stop the service.
echo.
"%PY%" run.py

echo.
echo [error] service exited with code %errorlevel%
pause