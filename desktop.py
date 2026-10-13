# -*- coding: utf-8 -*-
"""
股票盯盘 - 跨平台桌面启动器
优先使用 pywebview 打开原生桌面窗口：
- Windows: Edge WebView2 内核 (Win10/11 自带)
- Linux: GTK/WebKit 或 Qt/WebEngine
- macOS: Cocoa/WebKit
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

# ---------- 无控制台模式兼容（PyInstaller --noconsole 时 sys.stdout/stderr 为 None）----------
# 无控制台时把 print / uvicorn 日志重定向到文件，保证不崩溃、启动失败可排查
LOG_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "StockDashboard"
LOG_FILE = None


def _setup_output():
    global LOG_FILE
    has_console = True
    if getattr(sys, "frozen", False):
        try:
            import ctypes
            has_console = ctypes.windll.kernel32.GetConsoleWindow() != 0
        except Exception:
            has_console = True
    if has_console and sys.stdout is not None and sys.stderr is not None:
        return  # 正常控制台，保持原样
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / "run.log"
        if log_path.exists() and log_path.stat().st_size > 512 * 1024:
            try:
                log_path.replace(LOG_DIR / "run.old.log")
            except OSError:
                pass
        f = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)
        sys.stdout = f
        sys.stderr = f
        LOG_FILE = log_path
    except Exception:
        LOG_FILE = None


_setup_output()


def log(msg):
    try:
        print(msg, flush=True)
    except Exception:
        pass


def _box(text, flags=0x0, title="股票盯盘"):
    """无控制台时用系统消息框提示（0x10=错误图标, 0x40=信息图标）。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, flags)
        return True
    except Exception:
        return False


def show_fatal(msg):
    """启动失败：有控制台走控制台交互，无控制台弹系统错误框。"""
    log(msg)
    if LOG_FILE is None:
        try:
            input("按回车键退出...")
            return
        except Exception:
            pass
    _box(msg, 0x10, "股票盯盘 · 启动失败")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def is_port_free(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


# 优先使用固定端口: 每次启动访问地址一致(方便收藏, 也避免防火墙反复提示);
# 被占用时按 8765 → 8766 → 8767 回退, 都占用才随机。自选股数据不依赖端口, 存在 watchlist.json。
PREFERRED_PORTS = (8765, 8766, 8767)


def pick_port() -> int:
    env = os.environ.get("PORT", "").strip()
    if env.isdigit() and 1 <= int(env) <= 65535:
        return int(env)
    for p in PREFERRED_PORTS:
        if is_port_free(p):
            return p
    return free_port()  # 首选端口都被占用时才随机(此时本地自选会另存一份)


PORT = pick_port()
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


def _setup_linux_qt_env():
    """配置 Linux 下 Qt WebEngine 所需的环境变量"""
    if sys.platform.startswith("linux"):
        # 设置 Qt 平台插件路径
        qt_plugin_path = "/usr/lib/x86_64-linux-gnu/qt5/plugins"
        if os.path.exists(qt_plugin_path):
            os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", qt_plugin_path)
        
        # 设置 Qt WebEngine 进程路径
        qt_webengine_process = "/usr/lib/x86_64-linux-gnu/qt5/libexec/QtWebEngineProcess"
        if os.path.exists(qt_webengine_process):
            os.environ.setdefault("QTWEBENGINEPROCESS_PATH", qt_webengine_process)
        
        # 设置 Qt WebEngine 资源路径
        qt_resources = "/usr/share/qt5/resources"
        if os.path.exists(qt_resources):
            os.environ.setdefault("QTWEBENGINE_RESOURCES_PATH", qt_resources)
        
        # 设置 Qt WebEngine 语言包路径
        qt_locales = "/usr/share/qt5/translations/qtwebengine_locales"
        if os.path.exists(qt_locales):
            os.environ.setdefault("QTWEBENGINE_LOCALES_PATH", qt_locales)
        
        # 设置 Qt 库数据路径（修复资源加载问题）
        qt_library_data = "/usr/lib/x86_64-linux-gnu/qt5"
        if os.path.exists(qt_library_data):
            os.environ.setdefault("QTWEBENGINE_LIBRARY_DATA_PATH", qt_library_data)
        
        # 强制使用 xcb 平台插件（避免 wayland 问题）
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
        
        # 禁用 GTK 后端，强制使用 Qt
        os.environ.setdefault("WEBVIEW_GUI", "qt")
        
        # 设置 Qt 数据目录（修复资源加载路径问题）
        qt_data_dir = "/usr/lib/x86_64-linux-gnu/qt5"
        if os.path.exists(qt_data_dir):
            os.environ.setdefault("QT_DATA_DIR", qt_data_dir)
        
        # 设置 Qt 库路径
        qt_lib_dir = "/usr/lib/x86_64-linux-gnu"
        if os.path.exists(qt_lib_dir):
            os.environ.setdefault("QT_LIB_DIR", qt_lib_dir)


def main():
    log(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 启动 port={PORT} "
        f"控制台={'无(日志→' + str(LOG_FILE) + ')' if LOG_FILE else '有'}")
    ok = start_server()
    if not ok:
        show_fatal("后端服务启动失败，请检查网络后重试。" +
                   (f"\n日志: {LOG_FILE}" if LOG_FILE else ""))
        return

    # Linux 下配置 Qt WebEngine 环境
    _setup_linux_qt_env()

    # 强制启用 no-sandbox 标志（修复 Linux 下白屏/崩溃问题）
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu --disable-software-rasterizer")
    
    # 强制使用 Qt 后端（避免 GTK 后端加载失败）
    os.environ.setdefault("PYWEBVIEW_GUI", "qt")

    # 优先尝试原生桌面窗口 (NO_WEBVIEW=1 可强制浏览器模式, 用于诊断)
    if os.environ.get("NO_WEBVIEW") != "1":
        try:
            import webview  # pywebview
            webview.create_window(
                "股票盯盘 · 自选 · 榜单 · 选股 · 资讯 · 回测 · 引擎 · 日报",
                URL,
                width=1360, height=900,
                min_size=(980, 640),
                background_color="#0e1116",
            )
            webview.start()
            return  # 窗口关闭即退出
        except Exception as e:
            msg = f"桌面窗口不可用({e.__class__.__name__}), 回退为浏览器模式: {URL}"
            log(msg)
            if LOG_FILE is not None:
                _box(msg + "\n\n浏览器模式下本进程需保持运行; 结束应用请在任务管理器中结束该进程。",
                     0x40, "股票盯盘")

    # 回退：系统默认浏览器
    webbrowser.open(URL)
    log(f"\n股票盯盘已启动: {URL}")
    log("本窗口仅作为服务进程, 最小化即可, 关闭则应用退出。\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
