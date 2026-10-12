#!/bin/bash
# 股票盯盘 · Ubuntu 24.04 / Linux 直接启动脚本 (需已安装环境)
# 用法: bash run.sh

set -euo pipefail

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo -e "${BLUE}===========================================${NC}"
echo -e "${BLUE}  股票盯盘 · 启动${NC}"
echo -e "${BLUE}===========================================${NC}"
echo

# 检查虚拟环境
if [[ ! -f ".venv/bin/activate" ]]; then
    echo -e "${RED}[错误] 未找到虚拟环境${NC}"
    echo "请先运行 ./install.sh 完成安装"
    exit 1
fi

# 激活虚拟环境
source .venv/bin/activate

echo -e "${GREEN}启动 股票盯盘...${NC}"
echo -e "${BLUE}(提示: 启动后会弹出窗口，关闭终端则应用退出)${NC}"
echo

python desktop.py