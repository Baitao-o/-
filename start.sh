#!/usr/bin/env bash
# 麦门省钱搭子 —— 一键启动本地界面（macOS / Linux / WSL / Git Bash）
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python >/dev/null 2>&1; then
  echo "[错误] 找不到 python，请先安装 Python 3.8+"
  exit 1
fi

echo
echo "  =========================================================="
echo "    麦门省钱搭子 · 本地界面"
echo "  =========================================================="
echo "  浏览器会自动打开。若没有，请手动访问下面打印的地址。"
echo "  按 Ctrl+C 停止服务。"
echo "  =========================================================="
echo

exec python web/server.py
