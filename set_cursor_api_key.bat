@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================================
echo  Save CURSOR_API_KEY for start_cursor_sidecar.bat
echo  Get a key: Cursor Dashboard -^> Integrations -^> API Keys
echo  https://cursor.com/dashboard/integrations
echo  ^(Create User API Key; paste the full key, usually starts with key_^)
echo ============================================================
echo.
set /p KEY=Paste CURSOR_API_KEY: 
if "%KEY%"=="" (
  echo Empty key, aborted.
  pause
  exit /b 1
)

REM Write WITHOUT trailing space/CRLF junk that "echo %KEY%" often adds.
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
pause
exit /b 0
