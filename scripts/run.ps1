$root = Split-Path -Parent $PSScriptRoot
$profileName = $env:NODEBENCH_PROFILE
if (-not $profileName) { $profileName = "local" }

$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Write-Output "error: uv not found on PATH; install uv first, this script never downloads tools"
    exit 1
}

Push-Location $root
try {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $logDir = Join-Path $root "output\logs"
    if (-not (Test-Path -LiteralPath $logDir)) {
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    }
    $logPath = Join-Path $logDir "run-$stamp.log"
    Write-Output "profile=$profileName log=$logPath"

    $captured = @()
    & uv run nodebench run --profile $profileName @args 2>&1 | Tee-Object -Variable captured
    $code = $LASTEXITCODE
    $lines = @($captured | ForEach-Object { [string]$_ })
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($logPath, [string[]]$lines, $encoding)
    Write-Output "exit=$code log=$logPath"
    exit $code
}
finally {
    Pop-Location
}
