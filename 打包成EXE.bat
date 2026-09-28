@echo off
chcp 65001 >nul
title 股票盯盘 - 打包为EXE
cd /d "%~dp0"

echo ============================================
echo   股票盯盘 · 一键打包为 Windows EXE
echo ============================================
echo.

if not exist ".venv\Scripts\activate.bat" (
    echo [错误] 请先双击 "安装并启动.bat" 完成环境安装, 再回来打包。
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat

echo [1/3] 安装打包工具 PyInstaller...
python -m pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple pyinstaller
if errorlevel 1 python -m pip install -q -i https://mirrors.aliyun.com/pypi/simple/ pyinstaller
if errorlevel 1 python -m pip install -q pyinstaller

echo [2/3] 正在打包 (约2-5分钟, 产物约60MB)...
python -m PyInstaller --onefile --noconfirm --clean ^
    --name "股票盯盘" ^
    --add-data "static;static" ^
    --collect-all webview ^
    --collect-all tzdata ^
    --hidden-import uvicorn.logging ^
    --hidden-import uvicorn.loops.auto ^
    --hidden-import uvicorn.protocols.http.auto ^
    --hidden-import uvicorn.protocols.websockets.auto ^
    --hidden-import uvicorn.lifespan.on ^
    desktop.py

if errorlevel 1 (
    echo [错误] 打包失败, 请把上方报错信息反馈给我。
    pause
    exit /b 1
)

echo.
echo [3/3] 打包完成!
echo 产物位置: %~dp0dist\股票盯盘.exe
echo 双击即可运行, 无需安装 Python, 可复制到任意 Windows 10/11 电脑。
echo 首次运行会先启动服务(约3秒)再弹窗, 请稍候。
echo.
explorer "%~dp0dist"
pause
