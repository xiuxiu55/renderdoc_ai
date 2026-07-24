@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================================
echo  Save CURSOR_API_KEY for start_cursor_sidecar.bat
echo.
echo  Create the key HERE (Cloud Agents User API Key):
echo    https://cursor.com/dashboard?tab=cloud-agents
echo    -^> click "My Settings" -^> scroll to API Keys
echo  Or:
echo    https://cursor.com/dashboard/api
echo.
echo  Key format looks like: crsr_xxxxxxxx...
echo  Do NOT use a GitHub token / OAuth / Integrations webhook secret.
echo ============================================================
echo.
set /p KEY=Paste CURSOR_API_KEY: 
if "%KEY%"=="" (
  echo Empty key, aborted.
  pause
  exit /b 1
)

REM Write WITHOUT trailing space/CRLF junk that "echo %KEY%" often adds.
set "KEY=%KEY%"
powershell -NoProfile -Command ^
  "$k=$env:KEY.Trim().Trim([char]34); if(-not $k){exit 2}; [IO.File]::WriteAllText((Join-Path '%~dp0' '.cursor_api_key'), $k); [Environment]::SetEnvironmentVariable('CURSOR_API_KEY',$k,'User')"
if errorlevel 1 (
  echo Failed to save key.
  pause
  exit /b 1
)

echo.
echo Saved:
echo   - file: %~dp0.cursor_api_key
echo   - User env CURSOR_API_KEY ^(new terminals^)
echo.
echo Next: close any running sidecar, then start_cursor_sidecar.bat
echo Startup should print: api_key OK via /v1/me
pause
exit /b 0
