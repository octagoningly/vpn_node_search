# Build NodeBench Desktop as a Windows folder-distributed exe.
# Usage:  powershell -ExecutionPolicy Bypass -File gui/build_exe.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "==> install build deps"
uv sync --frozen
uv pip install pyinstaller

Write-Host "==> pyinstaller"
uv run pyinstaller --noconfirm gui/nodebench.spec

Write-Host ""
Write-Host "done. output: dist\NodeBench\NodeBench.exe"
Write-Host "把 dist\NodeBench 整个文件夹打包发给别人即可。"
Write-Host "对方首次运行会出现「首次设置向导」。"
