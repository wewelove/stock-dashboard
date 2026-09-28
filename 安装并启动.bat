@echo off
chcp 65001 >nul
title A股盯盘台 - 环境安装
cd /d "%~dp0"

echo ============================================
echo   A股盯盘台 · 首次安装 / 启动
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 Python。
    echo 请先安装 Python 3.9 或以上版本:
    echo     https://www.python.org/downloads/
    echo 安装时务必勾选 "Add Python to PATH"
    echo.
    set /p go=现在打开 Python 下载页吗? [Y/N]:
    if /i "%go%"=="Y" start https://www.python.org/downloads/
    pause
    exit /b 1
)

python --version

if not exist ".venv" (
    echo [1/3] 正在创建独立运行环境...
    python -m venv .venv
    if errorlevel 1 (
        echo [错误] 虚拟环境创建失败。
        pause
        exit /b 1
    )
)

call .venv\Scripts\activate.bat

echo [2/3] 正在安装依赖 (首次约1-2分钟)...
python -m pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
if errorlevel 1 (
    echo 清华源失败, 改用官方源重试...
    python -m pip install -q -r requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖安装失败, 请检查网络。
        pause
        exit /b 1
    )
)

echo [3/3] 启动 A股盯盘台...
echo (提示: 启动后可最小化本黑色控制台窗口, 关闭它则应用退出)
python desktop.py
pause
