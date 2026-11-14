param(
    [Parameter(Mandatory = $true)][string]$LicenseServerUrl,
    [Parameter(Mandatory = $true)][string]$TrialPurchaseUrl,
    [Parameter(Mandatory = $true)][string]$MonthlyPurchaseUrl,
    [Parameter(Mandatory = $true)][string]$QuarterlyPurchaseUrl,
    [Parameter(Mandatory = $true)][string]$AnnualPurchaseUrl
)
$ErrorActionPreference = "Stop"
$OfficialLicenseServerUrl = "https://example.com"
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$releaseRoot = Join-Path $project "release"
$release = Join-Path $releaseRoot "PriceMonitorCenter"
$dist = Join-Path $project "dist"
$build = Join-Path $project "build"
$commercialSpecPath = Join-Path $build "commercial-specs"
$icon = Join-Path $project "assets\app.ico"
$licenseCertificate = Join-Path $project "assets\license-server-ca.pem"
$commercialRuntimeHook = Join-Path $project "assets\commercial-runtime-hook.py"

if (-not (Test-Path $commercialRuntimeHook)) {
    throw "Commercial runtime hook not found: $commercialRuntimeHook"
}
if ($LicenseServerUrl.TrimEnd("/") -ne $OfficialLicenseServerUrl) {
    throw "Commercial builds must use the pinned official license server: $OfficialLicenseServerUrl"
}
$purchaseUrls = @($TrialPurchaseUrl, $MonthlyPurchaseUrl, $QuarterlyPurchaseUrl, $AnnualPurchaseUrl)
if ($purchaseUrls.Where({ -not $_.StartsWith("https://") }).Count -gt 0) {
    throw "Commercial builds require all four HTTPS purchase URLs"
}
New-Item -ItemType Directory -Path $commercialSpecPath -Force | Out-Null

python -m pip install -r (Join-Path $project "requirements.txt") -r (Join-Path $project "build-requirements.txt")

python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name "PriceMonitorCenter" --icon $icon `
    --specpath $commercialSpecPath `
    --runtime-hook $commercialRuntimeHook `
    --collect-all openpyxl --collect-all qrcode --hidden-import PIL.ImageTk `
    (Join-Path $project "desktop_app.py")

python -m PyInstaller --noconfirm --clean --onefile --noconsole `
    --name "PriceMonitorService" --icon $icon `
    --specpath $commercialSpecPath `
    --runtime-hook $commercialRuntimeHook `
    --collect-all openpyxl `
    (Join-Path $project "monitor_service.py")

if (Test-Path $releaseRoot) {
    $resolvedReleaseRoot = [System.IO.Path]::GetFullPath($releaseRoot)
    $resolvedProject = [System.IO.Path]::GetFullPath($project)
    if (-not $resolvedReleaseRoot.StartsWith($resolvedProject, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clear release path outside project: $resolvedReleaseRoot"
    }
    Remove-Item -LiteralPath $resolvedReleaseRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $release -Force | Out-Null

Copy-Item -LiteralPath (Join-Path $dist "PriceMonitorCenter.exe") -Destination $release
Copy-Item -LiteralPath (Join-Path $dist "PriceMonitorService.exe") -Destination $release
foreach ($file in @("run-monitor.ps1", "start-monitor.ps1", "stop-monitor.ps1", "open-app.ps1", "README.md")) {
    Copy-Item -LiteralPath (Join-Path $project $file) -Destination $release
}
Copy-Item -LiteralPath (Join-Path $project "config.release.json") -Destination (Join-Path $release "config.json")
New-Item -ItemType Directory -Path (Join-Path $release "assets") -Force | Out-Null
Copy-Item -LiteralPath $icon -Destination (Join-Path $release "assets\app.ico")
if (Test-Path $licenseCertificate) {
    Copy-Item -LiteralPath $licenseCertificate -Destination (Join-Path $release "assets\license-server-ca.pem")
}

$sourceConfig = Get-Content -Raw -Encoding UTF8 (Join-Path $project "config.json") | ConvertFrom-Json
$releaseConfig = Get-Content -Raw -Encoding UTF8 (Join-Path $project "config.release.json") | ConvertFrom-Json
$releaseConfigPath = Join-Path $release "config.json"
if (-not (Test-Path $licenseCertificate)) {
    throw "License server certificate not found: $licenseCertificate"
}
$releaseConfig.licensing.enabled = $true
$releaseConfig.licensing.server_url = $OfficialLicenseServerUrl
$releaseConfig.licensing.purchase_urls.trial_1d = $TrialPurchaseUrl
$releaseConfig.licensing.purchase_urls.monthly_30d = $MonthlyPurchaseUrl
$releaseConfig.licensing.purchase_urls.quarterly_90d = $QuarterlyPurchaseUrl
$releaseConfig.licensing.purchase_urls.annual_365d = $AnnualPurchaseUrl
$releaseConfigJson = $releaseConfig | ConvertTo-Json -Depth 12
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText(
    $releaseConfigPath,
    $releaseConfigJson + [Environment]::NewLine,
    $utf8NoBom
)
$platformTools = Split-Path -Parent $sourceConfig.adb_path
if (-not (Test-Path (Join-Path $platformTools "adb.exe"))) {
    throw "ADB platform-tools not found: $platformTools"
}
New-Item -ItemType Directory -Path (Join-Path $release "tools") -Force | Out-Null
Copy-Item -LiteralPath $platformTools -Destination (Join-Path $release "tools\platform-tools") -Recurse
$fastDumpJar = Join-Path $project "tools\fast-ui-dump\fast-ui-dump.jar"
if (-not (Test-Path $fastDumpJar)) {
    throw "Fast UI dump helper not found: $fastDumpJar"
}
Copy-Item -LiteralPath $fastDumpJar -Destination (Join-Path $release "tools\platform-tools\price-monitor-fast-dump.jar")
New-Item -ItemType Directory -Path (Join-Path $release "secrets") -Force | Out-Null

$launcher = @'
@echo off
cd /d "%~dp0"
start "" "PriceMonitorCenter.exe"
'@
Set-Content -LiteralPath (Join-Path $release "Open Price Monitor.cmd") -Value $launcher -Encoding ASCII

$zip = Join-Path $releaseRoot "PriceMonitorCenter-Windows.zip"
if (Test-Path $zip) { Remove-Item -LiteralPath $zip -Force }
Compress-Archive -Path (Join-Path $release "*") -DestinationPath $zip -CompressionLevel Optimal
$archiveListing = python -m PyInstaller.utils.cliutils.archive_viewer -l (Join-Path $release "PriceMonitorCenter.exe")
if ($archiveListing -notmatch "commercial-runtime-hook") {
    throw "Desktop executable is missing the commercial runtime hook"
}
$archiveListing = python -m PyInstaller.utils.cliutils.archive_viewer -l (Join-Path $release "PriceMonitorService.exe")
if ($archiveListing -notmatch "commercial-runtime-hook") {
    throw "Service executable is missing the commercial runtime hook"
}
Write-Output "Release created: $zip"
