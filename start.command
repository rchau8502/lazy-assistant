#!/bin/bash
# 双击运行（macOS）。第一次会装依赖并让你选模型。
cd "$(dirname "$0")" || exit 1
exec ./start.sh
