@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" manage.py backup
) else (
  python manage.py backup
)

if errorlevel 1 (
  echo [错误] 备份失败。
) else (
  echo 备份文件保存在 exports\backups 目录。
)
pause

