@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

REM Resolve API key (FILE WINS — inherited User env often has a stale key):
REM   1) .cursor_api_key next to this script
REM   2) CURSOR_API_KEY already in THIS process
REM   3) User-level environment variable
REM   4) interactive paste (then save to .cursor_api_key)

set "KEY_SOURCE="
set "RESOLVED_KEY="

if exist "%~dp0.cursor_api_key" (
  for /f "usebackq delims=" %%A in (`powershell -NoProfile -Command "[IO.File]::ReadAllText('%~dp0.cursor_api_key').Trim().Trim([char]34)"`) do set "RESOLVED_KEY=%%A"
  if not "!RESOLVED_KEY!"=="" set "KEY_SOURCE=file:.cursor_api_key"
)

if "!RESOLVED_KEY!"=="" if not "%CURSOR_API_KEY%"=="" (
  set "RESOLVED_KEY=%CURSOR_API_KEY%"
  set "KEY_SOURCE=process env"
)

if "!RESOLVED_KEY!"=="" (
  for /f "usebackq delims=" %%A in (`powershell -NoProfile -Command "$k=[Environment]::GetEnvironmentVariable('CURSOR_API_KEY','User'); if ($k) { $k.Trim().Trim([char]34) }"`) do set "RESOLVED_KEY=%%A"
  if not "!RESOLVED_KEY!"=="" set "KEY_SOURCE=User env"
)

if "!RESOLVED_KEY!"=="" (
  echo.
  echo CURSOR_API_KEY is not set.
  echo   Run set_cursor_api_key.bat, or paste a User API Key below.
  echo   https://cursor.com/dashboard/integrations
  echo.
  set /p RESOLVED_KEY=Paste CURSOR_API_KEY: 
  if "!RESOLVED_KEY!"=="" (
    echo ERROR: still empty.
    pause
    exit /b 1
  )
  set "CURSOR_API_KEY=!RESOLVED_KEY!"
  powershell -NoProfile -Command ^
    "$k=$env:CURSOR_API_KEY.Trim().Trim([char]34); [IO.File]::WriteAllText((Join-Path '%~dp0' '.cursor_api_key'), $k); [Environment]::SetEnvironmentVariable('CURSOR_API_KEY',$k,'User')"
  for /f "usebackq delims=" %%A in (`powershell -NoProfile -Command "[IO.File]::ReadAllText('%~dp0.cursor_api_key').Trim()"`) do set "RESOLVED_KEY=%%A"
  set "KEY_SOURCE=pasted->.cursor_api_key"
)

set "CURSOR_API_KEY=!RESOLVED_KEY!"
for /f "usebackq delims=" %%A in (`powershell -NoProfile -Command "$env:CURSOR_API_KEY.Trim().Trim([char]34)"`) do set "CURSOR_API_KEY=%%A"

if "!CURSOR_API_KEY!"=="" (
  echo ERROR: CURSOR_API_KEY empty after resolve.
  pause
  exit /b 1
)

REM Keep User env in sync with file so stale setx values don't linger.
if /i "!KEY_SOURCE!"=="file:.cursor_api_key" (
  powershell -NoProfile -Command "[Environment]::SetEnvironmentVariable('CURSOR_API_KEY',$env:CURSOR_API_KEY,'User')" >nul 2>&1
)

set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"

REM %~dp0 ends with \ — "%~dp0" would swallow the closing quote. Use %CD%.
echo Starting Cursor sidecar on port 8080 ...
echo Panel: Window -^> AI assistant -^> Reconnect
echo Using API key from: !KEY_SOURCE!
"%PY%" -m renderdoc_mcp.cursor_sidecar --port 8080 --cwd "%CD%"
set "ERR=!ERRORLEVEL!"
if not "!ERR!"=="0" pause
exit /b !ERR!
