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

# 检查 webview 依赖 (Linux 下需要 GTK/WebKit)
echo -e "${YELLOW}[检查] 检查 WebView 依赖...${NC}"
if ! dpkg -l | grep -q "libwebkit2gtk-4.1-0\|libwebkit2gtk-4.0-37"; then
    echo -e "${YELLOW}[提示] 可能需要安装 WebView2 依赖:${NC}"
    echo "  sudo apt update && sudo apt install -y libwebkit2gtk-4.1-0"
    echo "  (或 libwebkit2gtk-4.0-37，视 Ubuntu 版本而定)"
fi

echo -e "${GREEN}[3/3] 启动 股票盯盘...${NC}"
echo -e "${BLUE}(提示: 启动后会弹出窗口，关闭终端则应用退出)${NC}"
echo

python desktop.py