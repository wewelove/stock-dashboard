#!/bin/bash
# 股票盯盘 · Ubuntu 24.04 / Linux 打包脚本
# 用法: bash build.sh
# 产物: dist/股票盯盘 (Linux 可执行文件)

set -euo pipefail

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo -e "${BLUE}===========================================${NC}"
echo -e "${BLUE}  股票盯盘 · Ubuntu 24.04 打包为可执行文件${NC}"
echo -e "${BLUE}===========================================${NC}"
echo

# 检查虚拟环境
if [[ ! -f ".venv/bin/activate" ]]; then
    echo -e "${RED}[错误] 请先运行 ./install.sh 完成环境安装，再运行打包${NC}"
    exit 1
fi

# 激活虚拟环境
source .venv/bin/activate

# 安装 PyInstaller
echo -e "${YELLOW}[1/3] 安装打包工具 PyInstaller...${NC}"
pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple pyinstaller 2>/dev/null \
    || pip install -q -i https://mirrors.aliyun.com/pypi/simple/ pyinstaller 2>/dev/null \
    || pip install -q pyinstaller

# 检查系统依赖
echo -e "${YELLOW}[检查] 检查打包所需系统依赖...${NC}"
MISSING_DEPS=()
for dep in libwebkit2gtk-4.1-0 libwebkit2gtk-4.0-37; do
    if dpkg -l | grep -q "^ii  $dep "; then
        echo -e "${GREEN}  ✓ $dep 已安装${NC}"
        break
    fi
    MISSING_DEPS+=("$dep")
done

if [[ ${#MISSING_DEPS[@]} -gt 0 ]]; then
    echo -e "${YELLOW}[提示] 建议安装 WebView 依赖以确保打包产物可运行:${NC}"
    echo "  sudo apt update && sudo apt install -y libwebkit2gtk-4.1-0"
    echo
fi

# 打包
echo -e "${YELLOW}[2/3] 正在打包 (约2-5分钟)...${NC}"

pyinstaller --onefile --noconsole --noconfirm --clean \
    --name "股票盯盘" \
    --add-data "static:static" \
    --collect-all webview \
    --collect-all tzdata \
    --hidden-import uvicorn.logging \
    --hidden-import uvicorn.loops.auto \
    --hidden-import uvicorn.protocols.http.auto \
    --hidden-import uvicorn.protocols.websockets.auto \
    --hidden-import uvicorn.lifespan.on \
    --hidden-import finance_toolkit \
    desktop.py

if [[ $? -ne 0 ]]; then
    echo -e "${RED}[错误] 打包失败，请查看上方报错信息${NC}"
    exit 1
fi

echo -e "${GREEN}[3/3] 打包完成！${NC}"
echo -e "${GREEN}产物位置: $SCRIPT_DIR/dist/股票盯盘${NC}"
echo
echo -e "${BLUE}使用说明:${NC}"
echo "  1. 产物为单文件 Linux 可执行程序，无需安装 Python"
echo "  2. 可复制到任意兼容的 Linux 发行版 (Ubuntu 20.04+/Debian 11+/CentOS 7+ 等) 运行"
echo "  3. 目标机器需安装 WebView 依赖: libwebkit2gtk-4.1-0 或 libwebkit2gtk-4.0-37"
echo "  4. 首次运行可能需要添加执行权限: chmod +x 股票盯盘"
echo
echo -e "${YELLOW}注意: PyInstaller 不支持跨平台打包 (Linux 打包只能产出 Linux 程序，Windows 需在 Windows 上打包)${NC}"