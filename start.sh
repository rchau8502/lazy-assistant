#!/usr/bin/env bash
# 小助 · 懒人包一键启动：装环境 → 配置（首次）→ 起服务 → 开浏览器
set -euo pipefail
cd "$(dirname "$0")"

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "没找到 Python 3。请先安装：https://www.python.org/downloads/"
  exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "需要 Python 3.10 或更新版本，当前是：$("$PY" -V 2>&1)"
  exit 1
fi

echo "==> 准备运行环境（首次会装依赖，几十秒）"
if [ ! -d .venv ]; then "$PY" -m venv .venv; fi
if ! .venv/bin/python -c 'import fastapi, uvicorn, httpx, dotenv' >/dev/null 2>&1; then
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt
fi

if [ ! -f .env ]; then
  .venv/bin/python setup.py
fi

PORT=$(sed -n 's/^PORT=//p' .env | tail -1); PORT="${PORT:-8010}"
URL="http://127.0.0.1:${PORT}"

(
  sleep 1.8
  case "$(uname -s)" in
    Darwin) open "$URL" >/dev/null 2>&1 || true ;;
    Linux) xdg-open "$URL" >/dev/null 2>&1 || true ;;
  esac
) &

echo "==> 启动：$URL"
echo "    同一 Wi-Fi 下，手机用 http://$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}'):${PORT}"
echo "    按 Ctrl+C 停止"
exec .venv/bin/uvicorn app:app --host 0.0.0.0 --port "$PORT"
