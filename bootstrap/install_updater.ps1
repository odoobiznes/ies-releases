# IES updater — one-line install (Windows).
#
# In an Administrator PowerShell:
#   irm https://raw.githubusercontent.com/odoobiznes/ies-releases/master/bootstrap/install_updater.ps1 | iex
#
# What it does, idempotently:
#   1. Verifies Python 3.12 (winget install if absent)
#   2. Verifies NSSM (chocolatey install if absent)
#   3. Drops updater.py + config.yml at C:\Apps\ies-updater\
#   4. Sets up venv with pyyaml
#   5. Installs as Windows service via NSSM (auto-start on boot)
#
# After it runs, edit C:\Apps\ies-updater\config.yml + restart the service.
# Tested on Windows Server 2022.

#Requires -RunAsAdministrator

$ErrorActionPreference = 'Stop'
$IES   = "C:\Apps\ies-updater"
$RAW   = "https://raw.githubusercontent.com/odoobiznes"
$LogD  = "C:\Logs"

function Step($n, $msg) { Write-Host "[$n] $msg" -ForegroundColor Cyan }

# 1) prerequisites
Step "1/6" "verify Python 3.12"
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id Python.Python.3.12 -e --silent
    } else { throw "Python 3.12 not present and winget unavailable" }
}

Step "2/6" "verify NSSM"
if (-not (Get-Command nssm -ErrorAction SilentlyContinue)) {
    if (Get-Command choco -ErrorAction SilentlyContinue) {
        choco install nssm -y
    } else { throw "NSSM not present. Install from https://nssm.cc or run: choco install nssm -y" }
}

# 2) directories
Step "3/6" "directories"
New-Item -ItemType Directory -Force -Path $IES, $LogD | Out-Null

# 3) fetch updater.py
Step "4/6" "fetch updater.py + config template"
$updaterUrl = "$RAW/ies-releases/master/ies-updater/updater.py"
try {
    Invoke-WebRequest -UseBasicParsing -Uri $updaterUrl -OutFile "$IES\updater.py"
} catch {
    $updaterUrl = "$RAW/ies-releases/main/ies-updater/updater.py"
    Invoke-WebRequest -UseBasicParsing -Uri $updaterUrl -OutFile "$IES\updater.py"
}

if (-not (Test-Path "$IES\config.yml")) {
@'
# IES updater config — see https://github.com/odoobiznes/ies-releases
poll_interval_sec: 900
http_timeout_sec: 60
keep_versions: 3
release_index: https://raw.githubusercontent.com/odoobiznes/ies-releases/master/index.json

# telemetry_url: https://collab.it-enterprise.pro/api/v2/update-reports
telemetry_enabled: false

# Available services (Windows): pohoda-api, pohoda-digi, pohoda-xml-agent,
#   pohoda-kontrola, forms-doks, pohoda-api-gateway, ies-agent-manager
# Linux only: iesocr-worker
#
# Example (uncomment + edit):
# subscriptions:
#   pohoda-digi:
#     channel: stable
#     install_dir: C:\Apps\PohodaDigi

subscriptions: {}
'@ | Set-Content -Path "$IES\config.yml" -Encoding UTF8
}

# 4) venv
Step "5/6" "venv + pyyaml"
& py -3 -m venv "$IES\venv"
& "$IES\venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
& "$IES\venv\Scripts\python.exe" -m pip install --quiet pyyaml

# 5) NSSM service
Step "6/6" "install Windows service (NSSM)"
$existing = (& sc.exe query ies-updater 2>$null)
if ($LASTEXITCODE -eq 0) {
    & nssm stop ies-updater confirm 2>$null
    & nssm remove ies-updater confirm 2>$null
}
& nssm install ies-updater "$IES\venv\Scripts\python.exe" "$IES\updater.py"
& nssm set ies-updater AppDirectory $IES
& nssm set ies-updater AppStdout "$LogD\ies-updater.log"
& nssm set ies-updater AppStderr "$LogD\ies-updater.err"
& nssm set ies-updater Start SERVICE_AUTO_START
& nssm start ies-updater

Write-Host ""
Write-Host "DONE." -ForegroundColor Green
Write-Host "  Next steps:"
Write-Host "  1. notepad C:\Apps\ies-updater\config.yml   # add subscriptions"
Write-Host "  2. nssm restart ies-updater                  # apply"
Write-Host "  3. Get-Content C:\Logs\ies-updater.log -Tail 30 -Wait"
Write-Host ""
& sc.exe query ies-updater | Select-Object -First 5
