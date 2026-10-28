@echo off
rem Windows launcher for yt-dlp-extended. Double-click it, or run it from a
rem terminal: it stops this app's running server, sets up the virtualenv on
rem first run, starts a fresh server and opens the web UI.
setlocal
cd /d "%~dp0"

if "%YTX_PORT%"=="" (set "PORT=8723") else (set "PORT=%YTX_PORT%")
set "URL=http://127.0.0.1:%PORT%/"

echo yt-dlp-extended launcher
echo ------------------------

rem --- stop the previous instance -------------------------------------------
rem Matched on its own uvicorn command line, never on whatever owns the port,
rem so an unrelated program is never killed.
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*uvicorn app.main:app*' } | ForEach-Object { Write-Host ('Stopping running server (pid ' + $_.ProcessId + ')'); Stop-Process -Id $_.ProcessId -Force }"

rem --- pick a Python 3.10+ ---------------------------------------------------
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=py -3"
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo error: Python 3.10 or newer is required. Install it with:
  echo     winget install Python.Python.3.12
  pause
  exit /b 1
)

rem --- virtualenv + dependencies ---------------------------------------------
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtualenv ...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo error: could not create the virtualenv.
    pause
    exit /b 1
  )
)
echo Checking dependencies ...
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo error: installing dependencies failed - see the output above.
  pause
  exit /b 1
)

rem --- warn about missing external tools ------------------------------------
for %%B in (yt-dlp ffmpeg ffprobe) do (
  where %%B >nul 2>&1 || echo warning: %%B is not on PATH - set an explicit path on the Settings tab.
)

echo.
echo   yt-dlp-extended at %URL%
echo   Ctrl-C to stop.
echo.

rem --- open the browser once the server answers ------------------------------
if not "%YTX_NO_BROWSER%"=="1" (
  start "" /b powershell -NoProfile -Command "for ($i = 0; $i -lt 60; $i++) { try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 '%URL%api/health' | Out-Null; Start-Process '%URL%'; break } catch { Start-Sleep -Milliseconds 500 } }"
)

".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT% --timeout-graceful-shutdown 3

echo.
echo Server stopped.
pause
