@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

if exist "settings.bat" call "settings.bat"
if not defined TRACE_HOST set "TRACE_HOST=0.0.0.0"
if not defined TRACE_PORT set "TRACE_PORT=5080"

echo ==============================================================
echo  聚星同创仓库管理系统
echo ==============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
  echo 未检测到独立运行环境，正在自动安装...
  echo.
  call "%~dp0install.bat" --no-pause
  if errorlevel 1 goto :failed
)

".venv\Scripts\python.exe" -c "import flask, qrcode, bleak, waitress" >nul 2>nul
if errorlevel 1 (
  echo 运行环境不完整，正在自动修复...
  echo.
  call "%~dp0install.bat" --no-pause
  if errorlevel 1 goto :failed
)

rem Repeated double-clicks must not start a second server on the same port.
".venv\Scripts\python.exe" -c "import urllib.request; h=urllib.request.urlopen('http://127.0.0.1:%TRACE_PORT%/api/health',timeout=2); p=urllib.request.urlopen('http://127.0.0.1:%TRACE_PORT%/',timeout=2); raise SystemExit(0 if h.status==200 and p.status==200 and '聚星同创仓库管理系统' in p.read().decode('utf-8','ignore') else 1)" >nul 2>nul
if not errorlevel 1 goto :already_running

rem Give a clear message when another application, rather than this system,
rem already owns the configured port.
".venv\Scripts\python.exe" -c "import socket; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(('0.0.0.0',int('%TRACE_PORT%'))); s.close()" >nul 2>nul
if errorlevel 1 goto :port_in_use

echo 本机地址: http://127.0.0.1:%TRACE_PORT%
echo 服务运行期间请保持此窗口开启，按 Ctrl+C 停止。
echo.

".venv\Scripts\python.exe" "server.py"
if errorlevel 1 goto :failed
exit /b 0

:already_running
echo 系统已经在运行，无需重复启动。
echo 本机地址: http://127.0.0.1:%TRACE_PORT%
echo.
echo 如需停止服务，请关闭之前启动的“聚星同创仓库管理系统”窗口。
exit /b 0

:port_in_use
echo.
echo [错误] 端口 %TRACE_PORT% 已被其他程序占用，溯源系统无法启动。
echo 请关闭占用该端口的程序，或复制 settings.example.bat 为 settings.bat 后修改 TRACE_PORT。
pause
exit /b 1

:failed
echo.
echo [错误] 系统无法启动，请检查上方提示。
echo 如提示依赖缺失，请重新运行 install.bat。
pause
exit /b 1
