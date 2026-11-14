$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PRICE_MONITOR_ROOT = $project
$source = Join-Path $project "monitor_service.py"
$service = Join-Path $project "PriceMonitorService.exe"
$running = Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -in @("python.exe", "pythonw.exe") -and (
        $_.CommandLine -like "*-m monitor.main*" -or
        $_.CommandLine -like "*monitor_service.py*"
    )) -or
    $_.Name -eq "PriceMonitorService.exe"
}
if (-not $running) {
    if (Test-Path $source) {
        $pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
        if (-not $pythonw) {
            throw "Missing Python background runtime: pythonw.exe"
        }
        Start-Process -FilePath $pythonw -ArgumentList @($source) -WorkingDirectory $project -WindowStyle Hidden
    }
    elseif (Test-Path $service) {
        Start-Process -FilePath $service -WorkingDirectory $project -WindowStyle Hidden
    }
    else {
        throw "Missing background service: $source or $service"
    }
}
Write-Output "Price monitor started for this Windows session only. Startup remains disabled."
