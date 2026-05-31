param(
    [switch]$NoBuild,
    [switch]$DebugRun,
    [switch]$Console
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$VenvDir = Join-Path $ProjectDir '.venv'
$PythonExe = Join-Path $VenvDir 'Scripts\python.exe'
$SpecFile = Join-Path $ProjectDir 'CCWebView.spec'
$ExePath = Join-Path $ProjectDir 'dist\CCWebView\CCWebView.exe'

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Get-SystemPython {
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        return @('py', '-3')
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        return @($python.Source)
    }

    throw 'Python was not found. Install Python 3 or add it to PATH.'
}

Set-Location -LiteralPath $ProjectDir

Write-Step "Project: $ProjectDir"

if (-not (Test-Path -LiteralPath $VenvDir)) {
    Write-Step 'Creating virtual environment'
    $cmd = Get-SystemPython
    & $cmd[0] @($cmd[1..($cmd.Count - 1)]) -m venv $VenvDir
}

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Virtualenv Python not found: $PythonExe"
}

Write-Step 'Upgrading pip and installing build/runtime dependencies'
& $PythonExe -m pip install --upgrade pip setuptools wheel

$Requirements = Join-Path $ProjectDir 'requirements.txt'
if (Test-Path -LiteralPath $Requirements) {
    & $PythonExe -m pip install -r $Requirements
} else {
    & $PythonExe -m pip install pywebview pyinstaller pywin32
}

if (-not $NoBuild) {
    if (-not (Test-Path -LiteralPath $SpecFile)) {
        throw "Spec file not found: $SpecFile"
    }

    Write-Step 'Stopping running CCWebView instances, if any'
    Get-Process CCWebView -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

    Write-Step 'Cleaning old build output'
    Remove-Item -LiteralPath (Join-Path $ProjectDir 'build') -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $ProjectDir 'dist') -Recurse -Force -ErrorAction SilentlyContinue

    Write-Step 'Building with PyInstaller'
    & $PythonExe -m PyInstaller --noconfirm --clean $SpecFile
}

if (Test-Path -LiteralPath $ExePath) {
    Write-Step "Running built app: $ExePath"
    Start-Process -FilePath $ExePath -WorkingDirectory (Split-Path -Parent $ExePath)
    exit 0
}

Write-Step 'Built exe not found, running from Python source instead'
$argsList = @('-m', 'app.main')
if ($DebugRun) {
    $argsList += '--debug'
}
& $PythonExe @argsList
