@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "TASK_PYTHON="
py -3 -c "import sys; assert sys.version_info >= (3,10)" >nul 2>&1
if not errorlevel 1 set "TASK_PYTHON=py -3"
if not defined TASK_PYTHON (
    python -c "import sys; assert sys.version_info >= (3,10)" >nul 2>&1
    if not errorlevel 1 set "TASK_PYTHON=python"
)
if not defined TASK_PYTHON (
    echo 这个文件只安装采集库，不会安装 Python 本身。
    echo 请先安装 Python 3.10 或以上，并勾选 Add Python to PATH。
    pause
    exit /b 1
)
if /I "%~1"=="--check" exit /b 0
%TASK_PYTHON% -m pip install --upgrade --target "%~dp0lib" -r "%~dp0requirements.txt"
if errorlevel 1 echo 依赖安装失败，请检查网络连接后重试。
pause
