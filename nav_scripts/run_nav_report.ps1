<#
    Wrapper that runs the NAV catalog report export via manage.py.
    Used by the Windows Task Scheduler job (see register_nav_report_task.ps1) and
    safe to run by hand for an on-demand report:

        powershell -ExecutionPolicy Bypass -File scripts\run_nav_report.ps1

    Picks the interpreter automatically:
      1. a native-Windows venv  (.venv\Scripts\python.exe)
      2. a WSL/Linux venv       (.venv/bin/python, invoked through wsl.exe)
      3. `python` on PATH        (last resort)

    This project's venv is WSL-based, so option 2 is the normal path. The venv
    used MUST have the project requirements installed (pip install -r
    requirements.txt) — a bare venv with no Django will fail.

    Logs each run to scripts\nav_report.log.
#>
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot   # project dir (contains manage.py)
$log = Join-Path $PSScriptRoot 'nav_report.log'
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'

$winPy = Join-Path $Root '.venv\Scripts\python.exe'
$posixPy = Join-Path $Root '.venv\bin\python'   # exists for a WSL/Linux venv

if (Test-Path $winPy) {
    Add-Content $log "[$stamp] running (windows venv) $winPy" -Encoding utf8
    & $winPy (Join-Path $Root 'manage.py') export_nav_report *>> $log
    $code = $LASTEXITCODE
}
elseif (Test-Path $posixPy) {
    # WSL venv: translate the Windows project path to a WSL path and run there.
    $wslRoot = (& wsl.exe wslpath -a "$Root").Trim()
    Add-Content $log "[$stamp] running (wsl venv) $wslRoot/.venv/bin/python" -Encoding utf8
    & wsl.exe -e bash -lc "cd '$wslRoot' && ./.venv/bin/python manage.py export_nav_report" *>> $log
    $code = $LASTEXITCODE
}
else {
    Add-Content $log "[$stamp] running (PATH python) — no project venv found" -Encoding utf8
    & python (Join-Path $Root 'manage.py') export_nav_report *>> $log
    $code = $LASTEXITCODE
}

Add-Content $log "[$stamp] exit code $code" -Encoding utf8
exit $code