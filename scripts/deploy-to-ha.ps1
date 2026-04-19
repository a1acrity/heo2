# deploy-to-ha.ps1 - push HEO II master to Home Assistant and reload.
#
# Usage:
#   .\deploy-to-ha.ps1
#   .\deploy-to-ha.ps1 -Ref fix/some-branch
#   .\deploy-to-ha.ps1 -SkipReload   # deploy only, no integration reload
#
# Assumes:
#   - ssh key auth to root@homeassistant2.local:2222
#   - HA long-lived token in $TokenPath (default C:\Users\paddy\.heo2\token)
#   - scripts/deploy.sh present alongside this script
[CmdletBinding()]
param(
    [string]$HaHost = "homeassistant2.local",
    [int]$Port = 2222,
    [string]$User = "root",
    [string]$Ref = "master",
    [string]$Repo = "a1acrity/heo2",
    [switch]$SkipReload,
    [string]$TokenPath = "$env:USERPROFILE\.heo2\token"
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$deployScript = Join-Path $scriptDir "deploy.sh"
if (-not (Test-Path $deployScript)) {
    throw "Missing deploy.sh at $deployScript"
}

Write-Host "=== HEO II deploy ===" -ForegroundColor Cyan
Write-Host ("ref:        {0}" -f $Ref)
Write-Host ("repo:       {0}" -f $Repo)
Write-Host ("target:     {0}@{1}:{2}" -f $User, $HaHost, $Port)
Write-Host ""

# Step 1: write a LF-only copy of deploy.sh so busybox sh doesn't choke on CR.
# Do it via .NET rather than PowerShell string piping, which re-adds CRLF.
$lfScript = Join-Path $env:TEMP "heo2-deploy.sh"
$content = [System.IO.File]::ReadAllText($deployScript) -replace "`r`n", "`n"
[System.IO.File]::WriteAllText($lfScript, $content)

# Step 2: scp the script to HA, then ssh and run it.
Write-Host "Step 1: upload deploy.sh to HA"
& scp -P $Port -o BatchMode=yes $lfScript "${User}@${HaHost}:/tmp/heo2-deploy.sh"
if ($LASTEXITCODE -ne 0) { throw "scp failed: $LASTEXITCODE" }

Write-Host "Step 2: run deploy.sh on HA"
$remote = "REPO='$Repo' REF='$Ref' sh /tmp/heo2-deploy.sh"
& ssh -p $Port -o BatchMode=yes "${User}@${HaHost}" $remote
if ($LASTEXITCODE -ne 0) { throw "deploy.sh failed with exit $LASTEXITCODE" }

Remove-Item $lfScript -ErrorAction SilentlyContinue

if ($SkipReload) {
    Write-Host "SkipReload set, done."
    return
}

Write-Host ""
Write-Host "Step 3: reload HEO II integration via Core API"
if (-not (Test-Path $TokenPath)) {
    throw "No token at $TokenPath - create a long-lived HA token and save there"
}
$token = (Get-Content $TokenPath -Raw).Trim()
$headers = @{ Authorization = "Bearer $token" }
$base = "http://${HaHost}:8123/api"

$entries = Invoke-RestMethod -Uri "$base/config/config_entries/entry" `
                              -Headers $headers -TimeoutSec 10
$heo = $entries | Where-Object { $_.domain -eq "heo2" } | Select-Object -First 1
if (-not $heo) {
    throw "No heo2 config entry found. Is the integration set up?"
}
Write-Host ("HEO II entry_id: {0}" -f $heo.entry_id)

$body = @{ entry_id = $heo.entry_id } | ConvertTo-Json -Compress
Invoke-RestMethod -Uri "$base/services/homeassistant/reload_config_entry" `
                  -Method Post -Headers $headers -Body $body `
                  -ContentType "application/json" -TimeoutSec 30 | Out-Null
Write-Host "Reload dispatched." -ForegroundColor Green

Start-Sleep -Seconds 4

Write-Host ""
Write-Host "Step 4: health check"
$healthy = Invoke-RestMethod -Uri "$base/states/binary_sensor.heo_ii_healthy" `
                             -Headers $headers -TimeoutSec 10
Write-Host ("binary_sensor.heo_ii_healthy = {0}  (lc={1})" `
            -f $healthy.state, $healthy.last_changed)

$lastRun = Invoke-RestMethod -Uri "$base/states/sensor.heo_ii_last_run" `
                             -Headers $headers -TimeoutSec 10
Write-Host ("sensor.heo_ii_last_run = {0}  (lc={1})" `
            -f $lastRun.state, $lastRun.last_changed)

$solar = Invoke-RestMethod -Uri "$base/states/sensor.heo_ii_solar_forecast_today" `
                           -Headers $headers -TimeoutSec 10
Write-Host ("sensor.heo_ii_solar_forecast_today = {0} kWh  (lc={1})" `
            -f $solar.state, $solar.last_changed)

$loadp = Invoke-RestMethod -Uri "$base/states/sensor.heo_ii_load_profile" `
                           -Headers $headers -TimeoutSec 10
Write-Host ("sensor.heo_ii_load_profile = {0}  (lc={1})" `
            -f $loadp.state, $loadp.last_changed)

Write-Host ""
Write-Host "Done." -ForegroundColor Green
