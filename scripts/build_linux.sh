#!/usr/bin/env bash
# cloudctl agent 本地构建（Linux / macOS / proot）
# 用法：
#   bash scripts/build_linux.sh            # 含虚拟环境
#   SKIP_VENV=1 bash scripts/build_linux.sh
# 产物：agent/dist/cloudctl-agent
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AGENT="$ROOT/agent"
cd "$AGENT"

if [ -z "${SKIP_VENV:-}" ]; then
  echo "[1/4] 创建虚拟环境 .venv"
  python3 -m venv .venv
  PY="$AGENT/.venv/bin/python"
else
  echo "[1/4] 跳过虚拟环境"
  PY="$(command -v python3)"
fi

echo "[2/4] 安装依赖"
"$PY" -m pip install --upgrade pip
"$PY" -m pip install -r requirements.txt

if [ ! -f config.json ] && [ -z "${NO_CONFIG:-}" ]; then
  echo "[3/4] 生成 config.json（后续请填凭证，或跑 python3 scripts/setup_config.py）"
  cp config.example.json config.json
else
  echo "[3/4] 已存在 config.json，未覆盖"
fi

echo "[4/4] 打包单文件"
"$PY" -m PyInstaller --noconfirm --clean --onefile --name cloudctl-agent \
  --add-data "cloudctl_agent/devstatic:cloudctl_agent/devstatic" \
  --hidden-import websockets --hidden-import pynput \
  --hidden-import pynput.keyboard --hidden-import pynput.mouse \
  --collect-submodules pynput run_agent.py

if [ -f dist/cloudctl-agent ]; then
  chmod +x dist/cloudctl-agent
  echo "构建完成：$AGENT/dist/cloudctl-agent"
else
  echo "构建未产出可执行文件，请检查报错" >&2
  exit 1
fi
