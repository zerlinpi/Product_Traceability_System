@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

set "NO_PAUSE="
if /I "%~1"=="--no-pause" set "NO_PAUSE=1"

echo ==============================================================
echo  聚星同创仓库管理系统 - 安装程序
echo ==============================================================
echo.

set "BASE_PYTHON="
for /f "usebackq delims=" %%P in (`py -3 -c "import sys; print(sys.executable)" 2^>nul`) do set "BASE_PYTHON=%%P"

if not defined BASE_PYTHON (
  for /f "usebackq delims=" %%P in (`python -c "import sys; print(sys.executable)" 2^>nul`) do set "BASE_PYTHON=%%P"
)

if not defined BASE_PYTHON (
  echo [错误] 未找到 Python 3.11 或更高版本。
  echo 请先从 https://www.python.org/downloads/windows/ 安装 Python。
  goto :failed
)

"%BASE_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
  echo [错误] 系统要求 Python 3.11 或更高版本。
  echo 当前 Python 路径: %BASE_PYTHON%
  goto :failed
)

for /f "delims=" %%V in ('"%BASE_PYTHON%" --version 2^>^&1') do echo [Python] %%V

set "VENV_HEALTHY="
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
  if not errorlevel 1 set "VENV_HEALTHY=1"
)

if not defined VENV_HEALTHY (
  if exist ".venv" (
    echo [1/3] 检测到损坏或已迁移的独立运行环境，正在重建...
    rmdir /s /q ".venv"
    if exist ".venv" goto :failed
  ) else (
    echo [1/3] 正在创建独立运行环境...
  )
  "%BASE_PYTHON%" -m venv ".venv"
  if errorlevel 1 goto :failed
) else (
  echo [1/3] 独立运行环境已存在且可用。
)

if not exist ".venv\Scripts\python.exe" (
  echo [错误] 独立运行环境创建失败。
  goto :failed
)

echo [2/3] 正在更新安装工具...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :failed

echo [3/3] 正在安装系统依赖...
".venv\Scripts\python.exe" -m pip install -r "requirements.txt"
if errorlevel 1 goto :failed

echo.
echo [完成] 独立运行环境安装成功。
echo 双击 start.bat 启动系统；服务器部署前请按 README 配置 settings.bat。
if not defined NO_PAUSE pause
exit /b 0

:failed
echo.
echo [错误] 安装失败，请查看上方提示后重试。
if not defined NO_PAUSE pause
exit /b 1
