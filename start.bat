@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
if "%1"=="" (python server.py 4173) else (python server.py %1)
pause
