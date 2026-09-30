#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

# Same requirement as install.bat, and for the same reason: CI tests one version,
# and a venv built from an older interpreter fails later with errors that do not
# mention Python. Checked before the venv exists, so the failure is immediate and
# names the version.
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else 1)' 2>/dev/null; then
  echo "[错误] 系统要求 Python 3.13 或更高版本。"
  echo "当前版本: $(python3 --version 2>&1 || echo '未找到 python3')"
  exit 1
fi
echo "[Python] $(python3 --version 2>&1)"

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
