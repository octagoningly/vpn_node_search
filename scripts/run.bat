@echo off
setlocal
set "ROOT=%~dp0.."
if "%NODEBENCH_PROFILE%"=="" (set "PROFILE=local") else (set "PROFILE=%NODEBENCH_PROFILE%")

where uv >nul 2>&1
if errorlevel 1 (
    echo error: uv not found on PATH; install uv first, this script never downloads tools
    exit /b 1
)

pushd "%ROOT%"
if errorlevel 1 (
    echo error: cannot enter project root "%ROOT%"
    exit /b 1
)

if not exist "output\logs" mkdir "output\logs"
for /f "delims=" %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "STAMP=%%i"
set "LOG=output\logs\run-%STAMP%.log"
echo profile=%PROFILE% log=%CD%\%LOG%

uv run nodebench run --profile %PROFILE% %* > "%LOG%" 2>&1
set "CODE=%ERRORLEVEL%"
type "%LOG%"
echo exit=%CODE% log=%CD%\%LOG%
popd
exit /b %CODE%
