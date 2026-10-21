# Copy the integration to a mounted HA config share, e.g. .\scripts\deploy.ps1 \homeassistant\config
param([Parameter(Mandatory)][string]$ConfigPath)
$dest = Join-Path $ConfigPath "custom_components\powerpal_ble"
if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
Copy-Item "$PSScriptRoot\..\custom_components\powerpal_ble" $dest -Recurse
Get-ChildItem $dest -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
Write-Host "Deployed to $dest - restart Home Assistant."
