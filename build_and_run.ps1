param(
    [string]$Configuration = "Development",
    [string]$Platform = "x64",
    [switch]$SkipInstall,
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Sln = Join-Path $Root "renderdoc.sln"
$Exe = Join-Path $Root "$Platform\$Configuration\qrenderdoc.exe"
$InstallPy = Join-Path $Root "renderdoc_mcp\extension\install.py"

function Find-MSBuild {
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $vswhere) {
        $found = & $vswhere -latest -requires Microsoft.Component.MSBuild -find "MSBuild\**\Bin\MSBuild.exe" 2>$null
        if ($found) {
            if ($found -is [array]) { return $found[0] }
            return $found
        }
    }
    $fallback = @(
        "${env:ProgramFiles}\Microsoft Visual Studio\18\Professional\MSBuild\Current\Bin\MSBuild.exe",
        "${env:ProgramFiles}\Microsoft Visual Studio\2022\Professional\MSBuild\Current\Bin\MSBuild.exe",
        "${env:ProgramFiles}\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe"
    )
    foreach ($p in $fallback) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

$msbuild = Find-MSBuild
if (-not $msbuild) {
    Write-Error "MSBuild not found. Install Visual Studio with MSBuild."
}

Write-Host "==> Stopping running RenderDoc processes (if any)..." -ForegroundColor Cyan
Get-Process qrenderdoc, renderdoccmd, renderdocui -ErrorAction SilentlyContinue |
    Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

Write-Host "==> Building $Configuration|$Platform ..." -ForegroundColor Cyan
Write-Host "    MSBuild: $msbuild"
& $msbuild $Sln /p:Configuration=$Configuration /p:Platform=$Platform /m /nologo /v:minimal
if ($LASTEXITCODE -ne 0) {
    Write-Error "Build failed (exit $LASTEXITCODE)."
}

if (-not $SkipInstall -and (Test-Path $InstallPy)) {
    Write-Host "==> Installing AI extension..." -ForegroundColor Cyan
    $py = Get-Command python -ErrorAction SilentlyContinue
    if ($py) {
        & python $InstallPy
    } else {
        Write-Warning "python not on PATH; skipped extension install."
    }
}

if ($NoLaunch) {
    Write-Host "==> Build done (launch skipped)." -ForegroundColor Green
    exit 0
}

if (-not (Test-Path $Exe)) {
    Write-Error "Executable not found: $Exe"
}

Write-Host "==> Launching $Exe" -ForegroundColor Green
Start-Process $Exe
Write-Host "Done." -ForegroundColor Green
