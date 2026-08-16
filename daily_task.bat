@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

echo [%date% %time%] ===== 每日任务开始 ===== >> daily_task.log 2>&1
"tools\platform-tools\adb.exe" start-server >nul 2>&1
"tools\platform-tools\adb.exe" connect 192.168.1.10:5555 >> daily_task.log 2>&1   @REM 改为你的设备IP(如 192.168.1.10:5555)
".venv\Scripts\python.exe" fanqie_reader.py >> daily_task.log 2>&1
echo [%date% %time%] ===== 每日任务结束 (exit %errorlevel%) ===== >> daily_task.log 2>&1
