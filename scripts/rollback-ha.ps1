# rollback-ha.ps1 - restore the most recent HEO II backup on Home Assistant.
#
# Usage:
#   .\rollback-ha.ps1                # rollback, don't reload
#   .\rollback-ha.ps1 -Reload        # rollback AND reload config entry
#   .\rollback-ha.ps1 -Restart       # rollback AND restart HA (for module-cache clearing)
#
# Rollback always uses the most recent backup in /config/heo2_backups/.
[CmdletBinding()]
param(
    [string]$HaHost = "homeassistant2.local",
    [int]$Port = 2222,
    [string]$User = "root",
    [switch]$Reload,
    [switch]$Restart,
    [string]$TokenPath = "$env:USERPROFILE\.heo2\token"
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$rollbackScript = Join-Path $scriptDir "rollback.sh"
if (-not (Test-Path $rollbackScript)) {
    throw "Missing rollback.sh at $rollbackScript"
}

Write-Host "=== HEO II rollback ===" -ForegroundColor Yellow
Write-Host ("target: {0}@{1}:{2}" -f $User, $HaHost, $Port)

# Write LF-only copy, scp across, ssh run (same pattern as deploy-to-ha.ps1)
$lfScript = Join-Path $env:TEMP "heo2-rollback.sh"
$content = [System.IO.File]::ReadAllText($rollbackScript) -replace "`r`n", "`n"
[System.IO.File]::WriteAllText($lfScript, $content)

& scp -P $Port -o BatchMode=yes $lfScript "${User}@${HaHost}:/tmp/heo2-rollback.sh"
if ($LASTEXITCODE -ne 0) { throw "scp failed: $LASTEXITCODE" }

& ssh -p $Port -o BatchMode=yes "${User}@${HaHost}" "sh /tmp/heo2-rollback.sh"
if ($LASTEXITCODE -ne 0) { throw "rollback.sh failed with exit $LASTEXITCODE" }

Remove-Item $lfScript -ErrorAction SilentlyContinue

if ($Restart) {
    Write-Host ""
    Write-Host "Restarting HA to clear module cache..." -ForegroundColor Yellow
    $token = (Get-Content $TokenPath -Raw).Trim()
    $headers = @{ Authorization = "Bearer $token" }
    try {
        Invoke-RestMethod -Uri "http://${HaHost}:8123/api/services/homeassistant/restart" `
                          -Method Post -Headers $headers -Body "{}" `
                          -ContentType "application/json" -TimeoutSec 10 | Out-Null
    } catch {
        Write-Host "(restart call timed out, which is normal — HA is stopping)"
    }
    Write-Host "Wait ~60s for HA to come back. Check UI for integration status."
    return
}

if ($Reload) {
    Write-Host ""
    Write-Host "Reloading config entry via Core API..."
    $token = (Get-Content $TokenPath -Raw).Trim()
    $headers = @{ Authorization = "Bearer $token" }
    $base = "http://${HaHost}:8123/api"
    $entries = Invoke-RestMethod -Uri "$base/config/config_entries/entry" -Headers $headers -TimeoutSec 10
    $heo = $entries | Where-Object { $_.domain -eq "heo2" } | Select-Object -First 1
    if ($heo) {
        $body = @{ entry_id = $heo.entry_id } | ConvertTo-Json -Compress
        Invoke-RestMethod -Uri "$base/services/homeassistant/reload_config_entry" `
                          -Method Post -Headers $headers -Body $body `
                          -ContentType "application/json" -TimeoutSec 30 | Out-Null
        Write-Host "Reload dispatched." -ForegroundColor Green
        Write-Host "NOTE: reload only re-runs setup, it does NOT reload Python modules."
        Write-Host "If you need module changes picked up, use -Restart instead."
    } else {
        Write-Host "No heo2 config entry found"
    }
    return
}

Write-Host ""
Write-Host "Rollback complete. Neither -Reload nor -Restart set; HEO II still running old (cached) code." -ForegroundColor Yellow
Write-Host "Use -Restart to fully activate the rolled-back code."
