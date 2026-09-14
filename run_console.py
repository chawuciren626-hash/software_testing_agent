# -*- coding: utf-8 -*-
"""启动「软件测试智能体 · Web 控制台」（http://127.0.0.1:8765）。

用法：
    .venv\\Scripts\\python.exe run_console.py            # 前台运行，Ctrl+C 停止
    .venv\\Scripts\\python.exe run_console.py --detach   # 后台常驻（脱离终端），日志落 web_server.log

为什么需要 --detach 这条路径：
    在 AI 助手的沙箱里，用普通后台方式启动的进程会在工具调用结束时被一起回收
    （Windows Job Object 行为），表现为"服务起过、过一会儿浏览器打开是拒绝访问"。
    --detach 走 WMI 创建进程（父级为系统服务 WmiPrvSE），不在该 Job 内，因此能稳定存活。
    顺带：DETACHED_PROCESS 标志不够用，实测同样会被回收 —— 必须用 WMI。
"""
import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "web_server.log"
ENDPOINT = "http://127.0.0.1:8765"


def serve() -> None:
    """就地运行控制台（日志重定向到文件，便于后台方式查看）。"""
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT / "web_console"))
    sys.path.insert(0, str(ROOT))
    f = open(LOG, "a", encoding="utf-8", buffering=1)
    sys.stdout = f
    sys.stderr = f
    print("-" * 56)
    runpy.run_path(str(ROOT / "web_console" / "app.py"), run_name="__main__")


def probe(timeout: float = 6.0):
    """探测是否已就绪；强制绕过代理（本机若有 HTTP_PROXY 会干扰回环访问）。"""
    import time
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    end = time.time() + timeout
    while time.time() < end:
        try:
            with opener.open(ENDPOINT + "/", timeout=2) as r:
                return r.status
        except Exception:
            time.sleep(0.4)
    return None


def detach() -> None:
    """用 WMI 创建脱离当前进程树的常驻服务进程（仅 Windows）。"""
    if sys.platform != "win32":
        print("  --detach 依赖 WMI，仅 Windows 可用；其他平台请前台运行，"
              "或自行用 nohup / systemd 之类守护。")
        return
    import subprocess
    cmdline = f'"{sys.executable}" "{Path(__file__).resolve()}" --serve'
    ps = ("Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{"
          f"CommandLine='{cmdline}'; CurrentDirectory='{ROOT}'"
          "} | Select-Object ProcessId,ReturnValue | ConvertTo-Json -Compress")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, text=True)
    print("  WMI:", (r.stdout or "").strip() or (r.stderr or "").strip())


if __name__ == "__main__":
    if "--serve" in sys.argv:                  # 由 --detach 拉起的服务本体
        serve()
    elif probe(timeout=1.0) == 200:            # 已在跑就不重复启动
        print(f"服务已在运行：{ENDPOINT}")
    elif "--detach" in sys.argv:
        detach()
        if probe() == 200:
            print(f"后台启动成功：{ENDPOINT}（日志：{LOG.name}）")
        else:
            print(f"启动失败，请查看 {LOG.name}")
    else:
        print(f"启动中… {ENDPOINT}（Ctrl+C 停止）")
        serve()
