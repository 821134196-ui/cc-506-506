#!/usr/bin/env bash
# 一条本地命令：重置种子数据（含装错柜案例）并启动服务。
set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-8000}"

echo ">> 初始化种子数据（六类问题 + 装错柜被拒案例）..."
python3 -m app.seed data/museum.db

echo ">> 启动服务：http://127.0.0.1:${PORT}"
exec python3 -m app.server --db data/museum.db --port "${PORT}"
