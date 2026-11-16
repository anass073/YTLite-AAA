@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
    echo Python is not installed.
    echo Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^), then run this again.
    pause
    exit /b 1
)
if not exist .venv (
    echo Creating Python environment...
    py -3 -m venv .venv || goto :fail
)
echo Installing dependencies...
.venv\Scripts\python -m pip install -q --upgrade pip
.venv\Scripts\python -m pip install -q -r requirements.txt || goto :fail
.venv\Scripts\python -m copier --setup
pause
exit /b 0

:fail
echo.
echo Setup failed - see the message above.
pause
exit /b 1
