<#
    Start Excellar Dashboards.

        .\run.ps1                 # web app only, on :8080
        .\run.ps1 -All            # web app + CallSpread poller + NAV scheduler
        .\run.ps1 -Port 9000

    One project, one command. The background workers are optional: the web app
    serves the last data on disk without them.
#>
param(
    [int]$Port = 8080,
    [switch]$All
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    Write-Host 'No virtualenv found. Creating one...' -ForegroundColor Yellow
    python -m venv (Join-Path $root '.venv')
    & $python -m pip install --upgrade pip
    & $python -m pip install -r (Join-Path $root 'requirements.txt')
}

if (-not (Test-Path (Join-Path $root '.env'))) {
    Copy-Item (Join-Path $root '.env.example') (Join-Path $root '.env')
    Write-Host 'Created .env from .env.example — add your keys before going live.' -ForegroundColor Yellow
}

& $python (Join-Path $root 'manage.py') migrate --noinput
& $python (Join-Path $root 'manage.py') seed_dashboards

if ($All) {
    Write-Host 'Starting the CallSpread poller ...' -ForegroundColor Cyan
    Start-Process powershell -ArgumentList @(
        '-NoExit', '-Command', "Set-Location '$root'; .\.venv\Scripts\python.exe manage.py callspread_poll"
    )
    Write-Host 'Starting the NAV scheduler (needs Redis) ...' -ForegroundColor Cyan
    Start-Process powershell -ArgumentList @(
        '-NoExit', '-Command', "Set-Location '$root'; .\.venv\Scripts\python.exe manage.py run_huey"
    )
}

Write-Host ''
Write-Host "  Excellar Dashboards  ->  http://localhost:$Port" -ForegroundColor Green
Write-Host ''

& $python (Join-Path $root 'manage.py') runserver "127.0.0.1:$Port"
