# Archives the per-job crawl databases under data\ into backups\ (UTC timestamped zip).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$source = Join-Path $root "data"
if (-not (Test-Path $source)) { Write-Error "No data folder found at $source"; exit 1 }
$target = Join-Path $root "backups"
New-Item -ItemType Directory -Force -Path $target | Out-Null
$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
Compress-Archive -Path "$source\*" -DestinationPath (Join-Path $target "webnexus-data-$stamp.zip")
Write-Output "Backup written to $target"
