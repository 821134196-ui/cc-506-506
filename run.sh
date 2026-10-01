#!/usr/bin/env bash
# 一条本地命令：初始化数据库与演示样图（含装错柜案例），并启动服务。
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"

echo "» 初始化数据库与演示数据…"
python3 -m app.seed

echo "» 启动馆内展签管理系统 http://127.0.0.1:${PORT}"
echo "  账号：李策展/张策展（策展人）· 文编辑（编辑）· 古保管（保管员），右上角切换。"
exec env PORT="$PORT" python3 -c "
from app.server import run
import os
run(port=int(os.environ.get('PORT', '8000')))
"
