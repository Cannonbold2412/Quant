param(
    [string]$Python = "python",
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "Creating virtual environment..."
    & $Python -m venv .venv
}

$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"

Write-Host "Ensuring pip is available..."
& $venvPython -m ensurepip --upgrade

if (-not $SkipInstall) {
    Write-Host "Installing dependencies..."
    & $venvPython -m pip install -r requirements.txt
}

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example"
}

Write-Host ""
Write-Host "Bootstrap complete."
Write-Host "1. Create/download your Google Drive credential JSON, then place it in credentials\google_drive (see README for step-by-step Google Cloud setup)."
Write-Host "2. Activate the environment with: .venv\Scripts\Activate.ps1"
Write-Host "3. Run: python main.py"
