@echo off
cd /d "%~dp0"
if not exist config.yaml (
    echo Run setup.bat first.
    pause
    exit /b 1
)
:loop
.venv\Scripts\python -m copier
echo.
echo Bot stopped. Restarting in 15 seconds - close this window to stop it for good.
timeout /t 15 /nobreak >nul
goto loop
