"""软件测试智能体 · 桌面客户端（原生窗口）。

思路：把已跑通的 Flask 控制台（web_console/app.py）用 pywebview 包进一个原生窗口，
不开浏览器、无 Electron、不引入前端框架——界面与 Web 版完全同一套。
依赖 pywebview 时是原生窗口；未安装则自动回退到浏览器打开，不会报错卡死。

启动：
    .venv/Scripts/python web_console/desktop.py
可选参数：
    --browser   强制用浏览器打开（调试用）
    --no-gui    仅启动服务、不弹窗口（CI / 无图形环境 / 验证用）
    --port N    指定端口（默认 8765）
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "extensions"))    # 共享实现层 common/*
from common.obs import get_logger                # noqa: E402

log = get_logger("desktop")


def _free_port(preferred: int) -> int:
    """优先用给定端口；被占用时自动顺延，避免端口冲突导致启动失败。"""
    for p in range(preferred, preferred + 20):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue  # 可忽略：端口被占用是预期探测结果，顺延到下一个即可
    return preferred


def _keep_alive(url: str) -> None:
    """服务持续运行（窗口关闭或无法弹窗时的兜底）。Ctrl+C 退出。"""
    print(f"控制台服务已就绪：{url}（按 Ctrl+C 退出）")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return  # 可忽略：Ctrl+C 是正常退出方式，不是异常


def main() -> None:
    ap = argparse.ArgumentParser(description="软件测试智能体 · 桌面客户端")
    ap.add_argument("--browser", action="store_true", help="强制用浏览器打开（调试用）")
    ap.add_argument("--no-gui", action="store_true",
                    help="仅启动服务、不弹原生窗口（CI / 无图形环境 / 验证用）")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    port = _free_port(args.port)
    url = f"http://127.0.0.1:{port}"

    import web_console.app as console  # 复用同一套 Flask 控制台

    # Flask 放后台守护线程，主线程（窗口/keep-alive）负责维持进程存活
    threading.Thread(
        target=lambda: console.app.run(host="127.0.0.1", port=port,
                                       debug=False, use_reloader=False),
        daemon=True,
    ).start()

    # 等服务起来再开窗口，否则会看到空白页
    health = f"http://127.0.0.1:{port}/"
    import urllib.request
    for _ in range(50):
        try:
            urllib.request.urlopen(health, timeout=1).close()
            break
        except Exception:
            time.sleep(0.2)
    else:
        print("警告：服务未在预期时间内就绪，仍尝试打开界面。")

    if args.no_gui:
        _keep_alive(url)
        return

    if args.browser:
        print(f"浏览器模式：{url}")
        webbrowser.open(url)
        _keep_alive(url)
        return

    try:
        import webview
    except ImportError as e:
        log.warning("未安装 pywebview，已回退到浏览器：%s（安装：pip install pywebview）", e)
        webbrowser.open(url)
        _keep_alive(url)
        return

    print(f"桌面客户端启动：{url}")
    try:
        webview.create_window(
            title="软件测试智能体 · 控制台",
            url=url,
            width=1280,
            height=820,
            min_size=(980, 640),
            background_color="#F5F6FA",
            confirm_close=True,
        )
        webview.start()
    except Exception as exc:  # 无图形环境 / 缺少 WebView2 运行时等
        log.warning("无法创建原生窗口，已回退为仅服务模式：%s", exc)
        webbrowser.open(url)
        _keep_alive(url)


if __name__ == "__main__":
    main()
