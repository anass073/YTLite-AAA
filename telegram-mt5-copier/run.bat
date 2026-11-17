@echo off
cd /d "%~dp0"
if not exist config.yaml (
    echo Run setup.bat first.
    pause
    exit /b 1
)
:loop
.venv\Scripts\python -m copier
if "%errorlevel%"=="3" goto already
echo.
echo Bot stopped. Restarting in 15 seconds - close this window to stop it for good.
timeout /t 15 /nobreak >nul
goto loop
:already
echo.
echo Not started: the bot is already running in another window.
echo Close all bot windows (and any python.exe in Task Manager), then start run.bat once.
pause
exit /b 3
