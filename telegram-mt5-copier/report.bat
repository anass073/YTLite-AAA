@echo off
cd /d "%~dp0"
if not exist config.yaml (
    echo Run setup.bat first.
    pause
    exit /b 1
)
.venv\Scripts\python -m copier --report %*
echo.
pause
