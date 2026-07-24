@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM Resolve API key:
REM   1) env already set in THIS process
REM   2) .cursor_api_key file next to this script
REM   3) User-level environment variable
REM   4) interactive paste (then save to .cursor_api_key)

if not "%CURSOR_API_KEY%"=="" goto scrub_key

if exist "%~dp0.cursor_api_key" (
  for /f "usebackq delims=" %%A in (`powershell -NoProfile -Command "[IO.File]::ReadAllText('%~dp0.cursor_api_key').Trim().Trim([char]34)"`) do set "CURSOR_API_KEY=%%A"
)

if not "%CURSOR_API_KEY%"=="" goto scrub_key

for /f "usebackq delims=" %%A in (`powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('CURSOR_API_KEY','User')"`) do set "CURSOR_API_KEY=%%A"
if not "%CURSOR_API_KEY%"=="" goto scrub_key

echo.
echo CURSOR_API_KEY is not set in THIS process.
echo   Tip: set CURSOR_API_KEY=... in another CMD does NOT apply to a
echo   double-clicked .bat. Fix options:
echo     A^) run set_cursor_api_key.bat  ^(saves .cursor_api_key^)
echo     B^) in THIS window: set CURSOR_API_KEY=... ^& start_cursor_sidecar.bat
echo     C^) paste the key below
echo.
set /p CURSOR_API_KEY=Paste CURSOR_API_KEY: 
if "%CURSOR_API_KEY%"=="" (
  echo ERROR: still empty.
  pause
  exit /b 1
)
powershell -NoProfile -Command ^
  "$k=$env:CURSOR_API_KEY.Trim().Trim([char]34); [IO.File]::WriteAllText((Join-Path '%~dp0' '.cursor_api_key'), $k)"
echo Saved to .cursor_api_key for next launches.

:scrub_key
REM Strip quotes / whitespace that break Basic auth
for /f "usebackq delims=" %%A in (`powershell -NoProfile -Command "$env:CURSOR_API_KEY.Trim().Trim([char]34)"`) do set "CURSOR_API_KEY=%%A"
if "%CURSOR_API_KEY%"=="" (
  echo ERROR: CURSOR_API_KEY empty after scrub.
  pause
  exit /b 1
)

set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"

echo Starting Cursor sidecar on port 8080 ...
echo Panel: Window -^> AI assistant -^> Reconnect
"%PY%" -m renderdoc_mcp.cursor_sidecar --port 8080 --cwd "%~dp0"
set "ERR=%ERRORLEVEL%"
if not "%ERR%"=="0" pause
exit /b %ERR%
