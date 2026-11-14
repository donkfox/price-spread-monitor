$ErrorActionPreference = "Stop"
$taskName = "PDD-Dewu Price Monitor"
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}
$running = Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -in @("python.exe", "pythonw.exe") -and (
        $_.CommandLine -like "*-m monitor.main*" -or
        $_.CommandLine -like "*monitor_service.py*"
    )) -or
    $_.Name -eq "PriceMonitorService.exe"
}
foreach ($process in $running) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}
Write-Output "Price monitor stopped. Legacy startup task removed."
