#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

if [[ ! -x .venv/bin/python ]]; then
  echo "未找到 Python 虚拟环境，请先运行 ./install-linux.sh"
  exit 1
fi

if [[ ! -f settings.env ]]; then
  echo "未找到 settings.env，请复制 settings.example.env 并填写生产配置"
  exit 1
fi

set -a
source settings.env
set +a

exec .venv/bin/python server.py
