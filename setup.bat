@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 Python, 请先安装 Python 3.10+ 并勾选 "Add Python to PATH"
    echo 下载: https://www.python.org/downloads/
    pause
    exit /b 1
)

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [错误] Python 版本需 3.10 及以上
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] 创建虚拟环境 .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败
        pause
        exit /b 1
    )
) else (
    echo [1/3] 虚拟环境已存在, 跳过
)

echo [2/3] 安装依赖(需要联网) ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [错误] 依赖安装失败, 请检查网络后重试
    pause
    exit /b 1
)

echo [3/3] 初始化已连接的安卓设备 ...
".venv\Scripts\python.exe" -m uiautomator2 init
if errorlevel 1 (
    echo [提示] 设备初始化失败: 请确认手机已开启 USB 调试并连接电脑
    echo        连好手机后可随时重新双击 setup.bat 再试
)

echo.
echo 准备完成! 下一步:
echo   1. 用记事本编辑 config.yaml, 把 book.name 改成你的书名
echo   2. 双击 run.bat 开始挂机
pause
