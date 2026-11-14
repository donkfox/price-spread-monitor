<#
.SYNOPSIS
    生成价差监控激活码（链动小铺自动发货库存）。

.EXAMPLE
    .\issue_codes.ps1 -Days 30 -Count 100
    .\issue_codes.ps1 -Days 1  -Count 500 -Batch "4.8元体验卡"
    .\issue_codes.ps1 -List
    .\issue_codes.ps1 -Extend -LicenseId 12 -Days 7
#>
[CmdletBinding(DefaultParameterSetName = 'Generate')]
param(
    [Parameter(ParameterSetName = 'Generate')]
    [ValidateSet(1, 30, 90, 365)]
    [int]$Days = 30,

    [Parameter(ParameterSetName = 'Generate')]
    [int]$Count = 10,

    [Parameter(ParameterSetName = 'Generate')]
    [string]$Batch = '',

    [Parameter(ParameterSetName = 'Generate')]
    [string]$Output,

    [Parameter(ParameterSetName = 'List')]
    [switch]$List,

    [Parameter(ParameterSetName = 'Extend')]
    [switch]$Extend,

    [Parameter(ParameterSetName = 'Extend')]
    [int]$LicenseId,

    [Parameter(ParameterSetName = 'Extend')]
    [int]$ExtendDays = 7,

    [string]$Database = 'D:\license-data\licenses.sqlite3',

    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot

if (-not $Python) {
    $candidates = @(
        (Join-Path $projectRoot '.venv\Scripts\python.exe'),
        'C:\Users\Tphunk\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'
    )
    $Python = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $Python) { $Python = 'python' }
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Database) | Out-Null
Push-Location $projectRoot
try {
    if ($List) {
        & $Python -m license_server.admin --database $Database list
        return
    }

    if ($Extend) {
        if (-not $LicenseId) { throw '需要 -LicenseId' }
        & $Python -m license_server.admin --database $Database extend --license-id $LicenseId --days $ExtendDays
        return
    }

    if (-not $Output) {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $Output = Join-Path (Split-Path -Parent $Database) "codes-$($Days)d-$stamp.csv"
    }
    if (-not $Batch) {
        $Batch = switch ($Days) { 1 { '4.8元体验卡' } 30 { '月卡' } 90 { '季卡' } 365 { '年卡' } }
    }

    & $Python -m license_server.admin --database $Database generate --days $Days --count $Count --output $Output --batch $Batch
    if ($LASTEXITCODE -ne 0) { throw "发码失败（exit $LASTEXITCODE）" }

    $rows = Import-Csv $Output
    Write-Host ''
    Write-Host "已生成 $($rows.Count) 条 $Days 天激活码" -ForegroundColor Green
    Write-Host "CSV   : $Output"
    Write-Host "库存样例: $($rows[0].activation_code)  (共 $($rows.Count) 条)"
    Write-Host ''
    Write-Host '上传方式：链动小铺 → 该商品 → 自动发货 → 导入卡密（整列 activation_code）'
}
finally {
    Pop-Location
}
