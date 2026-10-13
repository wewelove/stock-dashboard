#!/bin/bash
# 股票盯盘 · Ubuntu 24.04 / Linux 安装并启动脚本
# 用法: bash install.sh

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
echo -e "${BLUE}  股票盯盘 · Ubuntu 24.04 安装并启动${NC}"
echo -e "${BLUE}===========================================${NC}"
echo

# 检查 Python 版本
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}[错误] 未检测到 Python3${NC}"
    echo "请先安装 Python 3.9 或以上版本:"
    echo "  sudo apt update && sudo apt install -y python3 python3-venv python3-pip"
    exit 1
fi

PY_VERSION=$(python3 --version | cut -d' ' -f2)
echo -e "${GREEN}[检测] Python 版本: $PY_VERSION${NC}"

# 检查 Python 版本是否 >= 3.9
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [[ $PY_MAJOR -lt 3 ]] || [[ $PY_MAJOR -eq 3 && $PY_MINOR -lt 9 ]]; then
    echo -e "${RED}[错误] Python 版本过低，需要 3.9 或以上${NC}"
    exit 1
fi

# 创建虚拟环境
if [[ ! -d ".venv" ]]; then
    echo -e "${YELLOW}[1/3] 正在创建虚拟环境...${NC}"
    python3 -m venv .venv
    if [[ $? -ne 0 ]]; then
        echo -e "${RED}[错误] 虚拟环境创建失败${NC}"
        echo "请确保已安装 python3-venv: sudo apt install -y python3-venv"
        exit 1
    fi
else
    echo -e "${GREEN}[1/3] 虚拟环境已存在，跳过创建${NC}"
fi

# 激活虚拟环境
source .venv/bin/activate

# 升级 pip
echo -e "${YELLOW}[2/3] 正在升级 pip...${NC}"
pip install -q --upgrade pip

# 安装依赖
echo -e "${YELLOW}[2/3] 正在安装依赖 (首次约1-2分钟)...${NC}"

# 尝试多个镜像源
PIP_FAIL=1

echo "尝试 1/3: 清华源 ..."
pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt && PIP_FAIL=0

if [[ $PIP_FAIL -eq 1 ]]; then
    echo "尝试 2/3: 阿里云源 ..."
    pip install -q -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt && PIP_FAIL=0
fi

if [[ $PIP_FAIL -eq 1 ]]; then
    echo "尝试 3/3: 官方源 ..."
    pip install -q -r requirements.txt && PIP_FAIL=0
fi

if [[ $PIP_FAIL -eq 1 ]]; then
    echo -e "${RED}[错误] 依赖安装失败${NC}"
    echo "请手动执行以下命令排查:"
    echo "  source .venv/bin/activate"
    echo "  pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt"
    exit 1
fi

# 检查 webview 依赖 (Linux 下需要 Qt WebEngine)
echo -e "${YELLOW}[检查] 检查 WebView 依赖...${NC}"
if ! dpkg -l | grep -q "libqt5webenginecore5\|libqt5webenginewidgets5\|libqt5webengine5"; then
    echo -e "${YELLOW}[提示] 可能需要安装 Qt WebEngine 依赖:${NC}"
    echo "  sudo apt update && sudo apt install -y libqt5webenginecore5 libqt5webenginewidgets5 libqt5webengine5 qtwebengine5-dev"
    echo "  (这些包提供 Qt WebEngine 运行时，pywebview 在 Linux 下需要它来渲染网页)"
fi

# 检查 pywebview 所需的系统依赖
echo -e "${YELLOW}[检查] 检查 pywebview 系统依赖...${NC}"
MISSING_DEPS=""

# 检查 Qt 核心库
if ! dpkg -l | grep -q "libqt5core5t64\|libqt5core5"; then
    MISSING_DEPS="$MISSING_DEPS libqt5core5t64"
fi

# 检查 Qt GUI 库
if ! dpkg -l | grep -q "libqt5gui5t64\|libqt5gui5"; then
    MISSING_DEPS="$MISSING_DEPS libqt5gui5t64"
fi

# 检查 Qt Widgets 库
if ! dpkg -l | grep -q "libqt5widgets5t64\|libqt5widgets5"; then
    MISSING_DEPS="$MISSING_DEPS libqt5widgets5t64"
fi

# 检查 Qt Network 库
if ! dpkg -l | grep -q "libqt5network5t64\|libqt5network5"; then
    MISSING_DEPS="$MISSING_DEPS libqt5network5t64"
fi

# 检查 Qt WebEngine 库
if ! dpkg -l | grep -q "libqt5webenginecore5"; then
    MISSING_DEPS="$MISSING_DEPS libqt5webenginecore5"
fi

if ! dpkg -l | grep -q "libqt5webenginewidgets5"; then
    MISSING_DEPS="$MISSING_DEPS libqt5webenginewidgets5"
fi

if ! dpkg -l | grep -q "libqt5webengine5"; then
    MISSING_DEPS="$MISSING_DEPS libqt5webengine5"
fi

if [[ -n "$MISSING_DEPS" ]]; then
    echo -e "${YELLOW}[提示] 建议安装以下 Qt 依赖以获得最佳桌面窗口体验:${NC}"
    echo "  sudo apt update && sudo apt install -y $MISSING_DEPS"
    echo ""
    echo "  如果不安装这些依赖，应用仍可在浏览器模式下运行（当前默认行为）。"
fi

echo -e "${GREEN}[3/3] 启动 股票盯盘...${NC}"
echo -e "${BLUE}(提示: 启动后会弹出窗口，关闭终端则应用退出)${NC}"
echo

python desktop.py