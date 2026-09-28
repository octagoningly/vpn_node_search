@echo off
REM Launch NodeBench Desktop GUI from source.
powershell -NoProfile -ExecutionPolicy Bypass -Command "uv run python gui/app.py"
pause
