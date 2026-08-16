@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [提示] 未找到虚拟环境, 请先双击 setup.bat 完成环境准备
    pause
    exit /b 1
)

".venv\Scripts\python.exe" fanqie_reader.py %*
pause
