# One-click: measure locally, then push public files so edgetunnel auto-picks up.
# Usage:  powershell -ExecutionPolicy Bypass -File scripts/publish.ps1
# Env:    NODEBENCH_PROFILE  (default auto-collect)

$root = Split-Path -Parent $PSScriptRoot
$profileName = $env:NODEBENCH_PROFILE
if (-not $profileName) { $profileName = "auto-collect" }

# Load local secrets from .env (never committed; see .env.example)
$envFile = Join-Path $root ".env"
if (Test-Path -LiteralPath $envFile) {
    Get-Content -LiteralPath $envFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line -match "^([A-Za-z_][A-Za-z0-9_]*)=(.*)$") {
            $name = $Matches[1]
            $value = $Matches[2].Trim('"').Trim("'")
            if ($value) { Set-Item -Path "Env:$name" -Value $value }
        }
    }
    Write-Output "loaded .env"
}

$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Write-Output "error: uv not found on PATH; install uv first"
    exit 1
}
$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) {
    Write-Output "error: git not found on PATH"
    exit 1
}

Push-Location $root
try {
    Write-Output "==> 1/2 run nodebench --profile $profileName (local speed test)"
    Write-Output "    please wait: collect candidates + probe ~3-10 min, no output is normal"
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    & uv run nodebench run --profile $profileName
    $code = $LASTEXITCODE
    $sw.Stop()
    Write-Output ("    took {0:N1} min, exit={1}" -f $sw.Elapsed.TotalMinutes, $code)

    $staging = Join-Path $root "output\publish-staging"
    if (-not (Test-Path -LiteralPath $staging)) {
        Write-Output "error: staging dir missing: $staging"
        exit 5
    }

    Write-Output "==> 2/2 push staging -> GitHub public branch"
    $work = Join-Path $root "output\_public_branch"
    if (Test-Path -LiteralPath $work) {
        Remove-Item -LiteralPath $work -Recurse -Force
    }
    New-Item -ItemType Directory -Path $work -Force | Out-Null

    foreach ($name in @("cf-addapi.txt", "cf-addcsv.csv", "manifest.json", "report.json")) {
        Copy-Item (Join-Path $staging $name) (Join-Path $work $name) -Force
    }

    # Independent repo snapshot on branch `public` (raw URLs serve these files).
    & git -C $work init -b public | Out-Null
    & git -C $work config user.email "nodebench@local"
    & git -C $work config user.name "nodebench-publish"
    & git -C $work add cf-addapi.txt cf-addcsv.csv manifest.json report.json
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
    & git -C $work commit -m "publish: nodebench CF endpoints $stamp"
    & git -C $work remote add origin (git -C $root remote get-url origin)
    & git -C $work push -u origin public --force

    Write-Output ""
    Write-Output "published. edgetunnel ADDAPI (set once, never changes):"
    Write-Output "  https://raw.githubusercontent.com/octagoningly/vpn_node_search/public/cf-addapi.txt"
    Write-Output "ADDCSV:"
    Write-Output "  https://raw.githubusercontent.com/octagoningly/vpn_node_search/public/cf-addcsv.csv"
    exit $code
}
finally {
    Pop-Location
}