@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

rem 依赖保障：缺 httpx 才安装，已满足则跳过
python -c "import httpx" >nul 2>nul
if errorlevel 1 (
    echo [start] 未检测到 httpx，正在安装依赖...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [start] 依赖安装失败，请检查 Python/pip 与网络后重试。
        pause
        exit /b 1
    )
)

if "%1"=="" (python server.py 4173) else (python server.py %1)
pause
