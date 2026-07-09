@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo =======================================================
echo      Stock Market Monitoring Radar v2
echo      Standalone Version - Modular Refactor
echo =======================================================
echo.

where python >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo Python environment not found!
    pause
    exit /b 1
)

python -c "import requests" >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo Installing requests...
    pip install requests -q
)

python -c "import akshare" >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo Installing akshare...
    pip install akshare>=1.14.0 -q
)

echo Environment ready, starting monitoring radar...
echo.

set PYTHONPATH=%cd%
python __main__.py
if %ERRORLEVEL% NEQ 0 (
    echo Error occurred, please check .env config and network connection
    pause
)
