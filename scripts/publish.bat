@echo off
REM One-click: local speed test + push to GitHub public branch for edgetunnel.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0publish.ps1"
pause
