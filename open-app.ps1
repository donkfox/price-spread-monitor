$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PRICE_MONITOR_ROOT = $project
$app = Join-Path $project "PriceMonitorCenter.exe"
if (Test-Path $app) {
    Start-Process -FilePath $app
} else {
    Start-Process -FilePath "pythonw.exe" -ArgumentList "-m", "monitor.desktop" -WorkingDirectory $project
}
