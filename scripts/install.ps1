$root = Split-Path -Parent $PSScriptRoot

$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Write-Output "error: uv not found on PATH; install uv first, this script never downloads tools"
    exit 1
}

$pythonExe = $null
$foundVersion = ""
$oldVersion = ""
foreach ($candidate in @(
    @{ exe = "python"; args = @("--version") },
    @{ exe = "py"; args = @("-3", "--version") }
)) {
    $command = Get-Command $candidate.exe -ErrorAction SilentlyContinue
    if (-not $command) { continue }
    $foundVersion = (& $candidate.exe @($candidate.args) 2>&1 | Out-String).Trim()
    if ($foundVersion -notmatch "^Python\s+(\d+)\.(\d+)") { continue }
    $major = [int]$Matches[1]
    $minor = [int]$Matches[2]
    if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 12)) {
        $pythonExe = $candidate.exe
        break
    }
    $oldVersion = $foundVersion
}

if (-not $pythonExe) {
    if ($oldVersion) {
        Write-Output "error: $oldVersion is older than python 3.12"
    }
    else {
        Write-Output "error: python 3.12 or newer not found on PATH"
    }
    exit 1
}

Push-Location $root
try {
    Write-Output "python=$foundVersion"
    Write-Output "step=uv sync"
    & uv sync
    if ($LASTEXITCODE -ne 0) {
        $code = $LASTEXITCODE
        Write-Output "error: uv sync failed with exit $code"
        exit $code
    }
    Write-Output "step=nodebench doctor"
    & uv run nodebench doctor
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
