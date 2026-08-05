<#
    Daily run for Windows hosts, driven by Task Scheduler.

    Use this if NRLA's estate is Windows, which is the likeliest case. Same
    reasoning as the shell version: run from an NRLA egress IP and gov.wales is
    reachable directly, which no cloud host can manage.

    SETUP
      1. Copy the project to C:\senedd-monitor
      2. py -3.11 -m venv C:\senedd-monitor\.venv
         C:\senedd-monitor\.venv\Scripts\pip install -r requirements.txt
      3. Copy deploy\monitor.env.example to C:\senedd-monitor\monitor.env and
         fill it in. Restrict its permissions to the service account.
      4. Register the scheduled task (one line, as Administrator):

         schtasks /Create /TN "Senedd policy monitor" /SC DAILY /ST 07:30 `
           /RU "NRLA\svc-senedd" /RP `
           /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\senedd-monitor\deploy\run-daily.ps1"

      For four runs a day during sitting weeks, add /RI 300 /DU 13:00 to repeat
      every five hours across the working day.

    Run it once by hand first, and read the log.
#>

$ErrorActionPreference = 'Continue'

$Root      = if ($env:MONITOR_ROOT) { $env:MONITOR_ROOT } else { 'C:\senedd-monitor' }
$Python    = Join-Path $Root '.venv\Scripts\python.exe'
$LogDir    = Join-Path $Root 'logs'
$BackupDir = Join-Path $Root 'backups'
$Lookback  = if ($env:MONITOR_LOOKBACK_DAYS) { $env:MONITOR_LOOKBACK_DAYS } else { '21' }

New-Item -ItemType Directory -Force -Path $LogDir, $BackupDir | Out-Null
$Log = Join-Path $LogDir ("run-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmm'))

function Write-Log($Message) {
    $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ'), $Message
    Write-Host  $line
    Add-Content -Path $Log -Value $line
}

Set-Location $Root

# Load configuration. Secrets live in a permission-restricted file, not in the
# scheduled task definition.
$EnvFile = Join-Path $Root 'monitor.env'
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        if ($_ -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*"?([^"]*)"?\s*$') {
            [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
        }
    }
    Write-Log 'loaded configuration from monitor.env'
} else {
    Write-Log 'WARNING: no monitor.env found - email will stay in dry-run mode'
}

Write-Log '=== Senedd monitor daily run starting ==='

# Tests first. A broken collector must not quietly publish a misleading page.
& $Python -m tests.test_monitor *>&1 | Add-Content -Path $Log
if ($LASTEXITCODE -ne 0) {
    Write-Log 'TESTS FAILED - aborting before touching the archive or sending anything'
    exit 1
}
Write-Log 'tests passed'

& $Python -m monitor.cli collect --days $Lookback *>&1 | Add-Content -Path $Log
$CollectStatus = $LASTEXITCODE
if ($CollectStatus -ne 0) {
    Write-Log "NOTE: collect reported a source failure (exit $CollectStatus). The dashboard will flag this run as incomplete."
}

$Publish = if ($env:MONITOR_PUBLISH_TO) { $env:MONITOR_PUBLISH_TO } else { Join-Path $Root 'out\index.html' }
& $Python -m monitor.cli dashboard --out $Publish *>&1 | Add-Content -Path $Log
Write-Log "dashboard published to $Publish"

& $Python -m monitor.cli alert --send *>&1 | Add-Content -Path $Log
& $Python -m monitor.cli digest --days 1 --send --dashboard-url $env:MONITOR_DASHBOARD_URL *>&1 | Add-Content -Path $Log
& $Python -m monitor.cli stats *>&1 | Add-Content -Path $Log

# The archive is one file, so backup and recovery are both trivial.
Copy-Item 'data\monitor.sqlite3' (Join-Path $BackupDir ("monitor-{0}.sqlite3" -f (Get-Date -Format 'yyyyMMdd'))) -Force
Get-ChildItem $BackupDir -Filter 'monitor-*.sqlite3' |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } | Remove-Item -Force
Get-ChildItem $LogDir -Filter 'run-*.log' |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-90) } | Remove-Item -Force
Write-Log 'archive backed up'

Write-Log '=== finished ==='
exit $CollectStatus
