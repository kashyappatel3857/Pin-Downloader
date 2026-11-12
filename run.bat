@echo off
title Pinterest Media Downloader
cd /d "%~dp0"

echo ===================================================
echo     Starting Pinterest Media Downloader
echo ===================================================
echo.

python main.py %*

echo.
pause
