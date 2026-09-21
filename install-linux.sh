#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

if [[ ! -f settings.env ]]; then
  cp settings.example.env settings.env
  chmod 600 settings.env
  echo "已创建 settings.env，请先修改密钥和管理员初始密码。"
fi

mkdir -p data exports/backups
echo "Linux 依赖安装完成。"
