from __future__ import annotations

import os
import socket

from app import create_app


app = create_app()


def local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            connection.connect(("8.8.8.8", 80))
            return connection.getsockname()[0]
    except OSError:
        return "服务器电脑IP"


if __name__ == "__main__":
    host = os.environ.get("TRACE_HOST", "0.0.0.0")
    port = int(os.environ.get("TRACE_PORT", "5080"))
    print("=" * 62)
    print(" 聚星同创仓库管理系统")
    print(f" 本机访问：http://127.0.0.1:{port}")
    print(f" 局域网访问：http://{local_ip()}:{port}")
    print(" 请保持此窗口运行；按 Ctrl+C 停止服务。")
    if os.environ.get("PTS_ENV", "development").strip().lower() != "production":
        print(" 当前为本地模式；部署服务器前请配置 PTS_ENV=production。")
    print("=" * 62)
    try:
        from waitress import serve

        serve(app, host=host, port=port, threads=8, channel_timeout=120)
    except ImportError:
        print("未安装 waitress，临时使用 Flask 服务；建议重新运行 install.bat。")
        app.run(host=host, port=port, threaded=True, debug=False)
    except OSError as error:
        print(f"[错误] 无法监听 {host}:{port}：{error}")
        print("请关闭占用该端口的程序，或在 settings.bat 中修改 TRACE_PORT。")
        raise SystemExit(1) from error
