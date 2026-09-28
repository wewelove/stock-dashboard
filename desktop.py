# -*- coding: utf-8 -*-
"""
股票盯盘 - Windows 桌面启动器
优先使用 pywebview 打开原生桌面窗口(Edge WebView2 内核, Win10/11 自带)；
若 pywebview 未安装或启动失败, 自动回退为系统默认浏览器打开。
"""
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

# PyInstaller onefile 模式下 static 目录在 _MEIPASS 内
if getattr(sys, "frozen", False):
    BASE = Path(sys._MEIPASS)
else:
    BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


PORT = free_port()
URL = f"http://127.0.0.1:{PORT}"
_server_ready = threading.Event()


def run_server():
    import uvicorn
    from app import app  # noqa: E402
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


def start_server():
    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    import urllib.request
    for _ in range(120):  # 最多等 60 秒
        try:
            urllib.request.urlopen(URL + "/api/health", timeout=1)
            _server_ready.set()
            return True
        except Exception:
            time.sleep(0.5)
    return False


def main():
    ok = start_server()
    if not ok:
        print("后端服务启动失败，请检查网络后重试。", flush=True)
        input("按回车键退出...")
        return

    # 优先尝试原生桌面窗口 (NO_WEBVIEW=1 可强制浏览器模式, 用于诊断)
    if os.environ.get("NO_WEBVIEW") != "1":
        try:
            import webview  # pywebview
            webview.create_window(
                "股票盯盘 · 自选盯盘 · 选股 · 资讯 · 回测",
                URL,
                width=1360, height=900,
                min_size=(980, 640),
                background_color="#0e1116",
            )
            webview.start()
            return  # 窗口关闭即退出
        except Exception as e:
            print(f"桌面窗口不可用({e.__class__.__name__}), 回退为浏览器模式。", flush=True)

    # 回退：系统默认浏览器
    webbrowser.open(URL)
    print(f"\n股票盯盘已启动: {URL}", flush=True)
    print("本窗口仅作为服务进程, 最小化即可, 关闭则应用退出。\n", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
