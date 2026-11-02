@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "TASK_PYTHON="
py -3 -c "import sys,tkinter; assert sys.version_info >= (3,10)" >nul 2>&1
if not errorlevel 1 set "TASK_PYTHON=py -3"
if not defined TASK_PYTHON (
    python -c "import sys,tkinter; assert sys.version_info >= (3,10)" >nul 2>&1
    if not errorlevel 1 set "TASK_PYTHON=python"
)
if not defined TASK_PYTHON (
    echo 请先安装 Python 3.10 或以上版本，并包含 Tcl/Tk。
    echo 使用官方 Windows 安装程序，并勾选 Add Python to PATH。
    echo 安装完成后重新双击本文件。
    pause
    exit /b 1
)
if /I "%~1"=="--check" exit /b 0
set "TASK_APP_DIR=%~dp0"
%TASK_PYTHON% -c "import os,sys; sys.path.insert(0, os.path.join(os.environ['TASK_APP_DIR'], 'lib')); import yt_dlp, openpyxl, pycountry, ytscrape, scrapetube" >nul 2>&1
if errorlevel 1 (
    echo 首次运行，正在安装采集依赖，请稍候……
    %TASK_PYTHON% -m pip install --upgrade --target "%~dp0lib" -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo 自动安装依赖失败，请检查网络后重新启动。
        pause
        exit /b 1
    )
)
if /I "%~1"=="--install-only" exit /b 0
%TASK_PYTHON% -B "%~dp0竞品红人视频挖掘.py"
if errorlevel 1 pause
