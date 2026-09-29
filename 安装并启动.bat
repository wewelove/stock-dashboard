@echo off
chcp 65001 >nul
title 股票盯盘 - 环境安装
cd /d "%~dp0"

echo ============================================
echo   股票盯盘 · 首次安装 / 启动
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

rem 安装 truststore: 让 pip 使用系统信任的 Windows 证书库, 可绕过部分网络下
rem 证书吊销检查(CRL/OCSP)不可达导致的 TLS 握手失败。安装失败不影响后续步骤。
python -m pip install -q --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ truststore 2>nul

set "PIP_FAIL=1"

echo [2/3] 尝试 1/3: 清华源 ...
python -m pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
if not errorlevel 1 set "PIP_FAIL="

if defined PIP_FAIL (
    echo [2/3] 尝试 2/3: 阿里云源 ...
    python -m pip install -q -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
    if not errorlevel 1 set "PIP_FAIL="
)

if defined PIP_FAIL (
    echo [2/3] 尝试 3/3: 官方源 ...
    python -m pip install -q -r requirements.txt
    if not errorlevel 1 set "PIP_FAIL="
)

if defined PIP_FAIL (
    echo.
    echo [错误] 依赖安装失败。可手动执行以下命令排查:
    echo     .venv\Scripts\activate
    echo     python -m pip install -i https://mirrors.aliyun.com/pypi/simple/ truststore
    echo     python -m pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
    pause
    exit /b 1
)

echo [3/3] 启动 股票盯盘...
echo (提示: 启动后可最小化本黑色控制台窗口, 关闭它则应用退出)
python desktop.py
pause