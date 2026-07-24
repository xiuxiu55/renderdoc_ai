# Start Cursor sidecar for the RenderDoc AI panel (CodeBuddy-compatible :8080).
#
# Key resolution order:
#   1) -ApiKey argument
#   2) process env CURSOR_API_KEY
#   3) repo file .cursor_api_key
#   4) User / Machine environment
#   5) interactive prompt (saves .cursor_api_key)
#
# Note: setting the var in another CMD/PowerShell window does NOT apply to a
# freshly double-clicked script. Prefer set_cursor_api_key.bat once.

param(
    [int]$Port = 8080,
    [string]$Model = "composer-2.5",
    [string]$ApiKey = "",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$KeyFile = Join-Path $Root ".cursor_api_key"

function Read-KeyFile([string]$path) {
    if (-not (Test-Path $path)) { return "" }
    $line = (Get-Content -LiteralPath $path -TotalCount 1 -ErrorAction SilentlyContinue)
    if ($null -eq $line) { return "" }
    return ($line.ToString().Trim())
}

if (-not $ApiKey) { $ApiKey = $env:CURSOR_API_KEY }
if (-not $ApiKey) { $ApiKey = Read-KeyFile $KeyFile }
if (-not $ApiKey) {
    $ApiKey = [Environment]::GetEnvironmentVariable("CURSOR_API_KEY", "User")
}
if (-not $ApiKey) {
    $ApiKey = [Environment]::GetEnvironmentVariable("CURSOR_API_KEY", "Machine")
}
if (-not $ApiKey) {
    Write-Host @"
CURSOR_API_KEY is not set in THIS process.

  Tip: `$env:CURSOR_API_KEY = '...'` in another window does not apply here.
  Prefer once: .\set_cursor_api_key.bat

Paste the key now (saved to .cursor_api_key):
"@ -ForegroundColor Yellow
    $ApiKey = (Read-Host "CURSOR_API_KEY").Trim()
    if (-not $ApiKey) {
        Write-Error "Empty key."
    }
    Set-Content -LiteralPath $KeyFile -Value $ApiKey -Encoding ascii
    Write-Host "Saved $KeyFile" -ForegroundColor Green
}

if (-not $Python) {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $Python = $c; break }
    }
}
if (-not $Python) { $Python = "python" }

Write-Host "Using $Python" -ForegroundColor Cyan
& $Python -c "import cursor_sdk, starlette, uvicorn" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing cursor-sdk / starlette / uvicorn ..." -ForegroundColor Yellow
    & $Python -m pip install "cursor-sdk" "starlette" "uvicorn" -q
}

$env:CURSOR_API_KEY = $ApiKey
Write-Host "Cursor sidecar -> http://127.0.0.1:$Port  (model=$Model)" -ForegroundColor Green
Write-Host "In RenderDoc: Window -> AI assistant -> Reconnect" -ForegroundColor Green
& $Python -m renderdoc_mcp.cursor_sidecar --port $Port --model $Model --cwd $Root --api-key $ApiKey
