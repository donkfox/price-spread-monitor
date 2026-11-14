$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PRICE_MONITOR_ROOT = $project
Set-Location $project
$env:PYTHONIOENCODING = "utf-8"
$service = Join-Path $project "PriceMonitorService.exe"
if (Test-Path $service) {
    & $service
} else {
    python -m monitor.main
}
