#!/usr/bin/env bash
# ============================================================
# iOS Screen Mirror — 一键安装脚本
# ============================================================
# 用法: bash setup.sh
# ============================================================

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║      iOS Screen Mirror — 环境安装          ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ---- 1. 检查 Homebrew ----
if ! command -v brew &>/dev/null; then
    echo -e "${YELLOW}[!] Homebrew 未安装，正在安装...${NC}"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
else
    echo -e "${GREEN}[✓] Homebrew 已安装${NC}"
fi

# ---- 2. 检查 Python3 ----
if ! command -v python3 &>/dev/null; then
    echo -e "${RED}[✗] Python3 未安装，请安装: brew install python3${NC}"
    exit 1
fi
PY_VER=$(python3 --version 2>&1)
echo -e "${GREEN}[✓] Python: ${PY_VER}${NC}"

# ---- 3. 安装 libimobiledevice ----
if ! command -v idevice_id &>/dev/null; then
    echo -e "${YELLOW}[!] libimobiledevice 未安装，正在安装...${NC}"
    brew install libimobiledevice
else
    echo -e "${GREEN}[✓] libimobiledevice 已安装${NC}"
fi

# ---- 4. 安装 Python 依赖 ----
echo ""
echo "正在安装 Python 依赖..."
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
pip3 install --upgrade pip -q
pip3 install -r "${SCRIPT_DIR}/requirements.txt" -q

echo -e "${GREEN}[✓] Python 依赖安装完成${NC}"

# ---- 5. 验证安装 ----
echo ""
echo "验证安装结果:"
echo -n "  customtkinter:    "
python3 -c "import customtkinter; print('✓', customtkinter.__version__)" 2>/dev/null || echo "✗"

echo -n "  Pillow:           "
python3 -c "from PIL import Image; import PIL; print('✓', PIL.__version__)" 2>/dev/null || echo "✗"

echo -n "  pymobiledevice3:  "
python3 -c "import pymobiledevice3; print('✓')" 2>/dev/null || echo "✗"

echo -n "  idevice_id:       "
command -v idevice_id &>/dev/null && echo "✓" || echo "✗"

echo -n "  ideviceinfo:      "
command -v ideviceinfo &>/dev/null && echo "✓" || echo "✗"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  安装完成! 运行方式:                                 ║"
echo "║                                                      ║"
echo "║  python3 ios_mirror.py                               ║"
echo "║                                                      ║"
echo "║  iOS 17+ 首次使用:                                   ║"
echo "║  1. 解锁 iPhone 并在手机上信任此电脑                 ║"
echo "║  2. 程序会自动挂载 DDI 并启动 tunneld                ║"
echo "║     (需要输入 macOS 密码)                            ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
