"""统一日志出口 + `run_id` 贯通（可观测性的**唯一定义处**）。

为什么要它
----------
本仓库此前可观测性为零：**全仓没有一处 `import logging`**，所有信息都靠 `print`
写 stdout，出错信息与正常进度混在一起；同时约 26 处 `except` 只 `pass`，
出了问题连"哪一步、哪一次运行"都定位不到 —— 这正是 CI 连红 15 次却查不出原因的
根因（当时只能靠 GitHub 注解这个"应急出口"绕开）。

有了它，两件事变成可能：
1. **出错必出声**：诊断信息进 stderr（CI 可见）、进日志文件（可回溯），不再被
   `except: pass` 吞掉，也不再和业务输出混在同一个管道里；
2. **一次运行一条线**：`run_id` 贯穿"控制台任务 → 子进程 → 各扩展"，
   跨进程、跨模块的日志能被串成同一次运行。

约定（与项目既有判据一脉相承）
------------------------------
- **`print` 负责"给人看的结果呈现"**（`list` 的项目表格、`defects` 的 Markdown、
  `--json` 的载荷）——它是**产品输出**，要能被 `|` 管道接走，留在 stdout。
- **日志负责"排障用的诊断"**（进度、判定、降级、异常）——走 stderr + 可选文件。
  二者不是二选一，而是**两种受众**：前者给使用者，后者给排障的人。

`run_id` 的传播
---------------
- 控制台每次任务用它的 `tid` 作 run_id，通过环境变量 `STA_RUN_ID` 传给子进程；
- 子进程（`project_manager.py`）启动时读取它，于是 CLI 与 Web 触发**同一条 id**；
- 扩展被 in-process import 时天然共享（`ContextVar`），无需额外传递。

设计取舍
--------
- **不占用 stdlib `logging` 这个名字**：本模块叫 `obs`（observability），
  避免 `import logging` 在包内产生歧义。
- **`propagate=True`**：记录继续向 stdlib root 传播，`pytest` 的 `caplog`
  才能抓到（否则测试无法断言"有没有出声"）。
- **惰性解析流**：handler 在 emit 时才读 `sys.stderr`，因此运行期的重定向
  （`run_console.py` 写 `web_server.log`、pytest 的 `capsys`）都能生效。
"""
from __future__ import annotations

import contextvars
import itertools
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Optional, Union

#: 环境变量：run_id 的跨进程载体（控制台 -> 子进程）。
RUN_ID_ENV = "STA_RUN_ID"
#: 环境变量：日志级别（默认 INFO）。
LOG_LEVEL_ENV = "STA_LOG_LEVEL"
#: 环境变量：可选日志文件路径。
LOG_FILE_ENV = "STA_LOG_FILE"

_LOGGER_ROOT = "sta"
_FMT = "%(asctime)s %(levelname)-5s [%(run_id)s] %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"

# run_id 用 ContextVar 承载：显式传参会把"每次调用都要记得带上"变成人的责任，
# 一旦某处忘了就断线；ContextVar 让"当前这次运行"成为环境事实。
_run_id: contextvars.ContextVar[str] = contextvars.ContextVar("sta_run_id", default="-")
_configured = False
#: 进程内自增序号，保证 `new_run_id` 在同一进程内绝不重复（见其 docstring）。
_run_seq = itertools.count()


# --------------------------------------------------------------------------- #
# run_id
# --------------------------------------------------------------------------- #
def new_run_id(prefix: str = "run") -> str:
    """生成短可读的 run_id：``<prefix>-<HHMMSS>-<4位随机><进程内序号>``。

    用短 id 而非 UUID 全集：它要出现在每一行日志里，人得能一眼分辨两次运行。

    唯一性**不靠概率**：4 位 hex 在同一秒内取 200 次就有约 1/4 概率相撞
    （本模块的守护测试正是这样发现问题的），所以再缀一个进程内自增序号 ——
    时间+随机负责跨进程/跨时刻不撞，序号负责同进程绝不重复。
    """
    return f"{prefix}-{time.strftime('%H%M%S')}-{uuid.uuid4().hex[:4]}{next(_run_seq):x}"


def set_run_id(rid: Optional[str]) -> str:
    """设置当前上下文的 run_id（空值归一为 ``-``），返回最终值。"""
    val = (rid or "").strip() or "-"
    _run_id.set(val)
    return val


def get_run_id() -> str:
    """取当前上下文的 run_id；未设置时为 ``-``。"""
    return _run_id.get()


def adopt_env_run_id(prefix: str = "cli") -> str:
    """采用 ``STA_RUN_ID``（若有），否则新生成一个。

    这是**入口函数**该调用的那一个：控制台传了 id 就沿用（同一次运行跨进程同一条线），
    直接命令行跑就自己生成。
    """
    return set_run_id(os.environ.get(RUN_ID_ENV) or new_run_id(prefix))


class _RunIdFilter(logging.Filter):
    """把当前 run_id 注入每条记录（格式串里的 ``%(run_id)s`` 靠它）。"""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 (logging API)
        if not getattr(record, "run_id", None):
            record.run_id = _run_id.get()
        return True


class _LazyStderrHandler(logging.StreamHandler):
    """写当前 ``sys.stderr``（而非创建 handler 时绑定的那个流）。

    必要原因：日志配置通常发生在**模块导入期**，而重定向（写文件 / pytest 捕获）
    发生在之后。若 handler 绑定旧流，就会出现"日志配了但看不到"的假象 ——
    排查这种问题比没有日志更费时间。
    """

    def __init__(self) -> None:
        super().__init__(stream=None)

    @property
    def stream(self):  # type: ignore[override]
        return sys.stderr

    @stream.setter
    def stream(self, value) -> None:  # noqa: ARG002 (父类会赋值，这里刻意忽略)
        pass


# --------------------------------------------------------------------------- #
# logger 配置
# --------------------------------------------------------------------------- #
def setup(level: Optional[Union[str, int]] = None, log_file: Optional[str] = None,
          force: bool = False) -> logging.Logger:
    """配置 ``sta`` 根 logger（**幂等**：重复调用不会叠加 handler）。

    level   : 日志级别（字符串或 int）；默认取 ``STA_LOG_LEVEL``，再默认 INFO。
    log_file: 追加一份文件日志；默认取 ``STA_LOG_FILE``。
    force   : 强制重配（仅测试用；生产路径请依赖幂等）。
    """
    global _configured
    root = logging.getLogger(_LOGGER_ROOT)
    if _configured and not force:
        return root

    lvl: Union[str, int] = level or os.environ.get(LOG_LEVEL_ENV) or "INFO"
    if isinstance(lvl, str):
        lvl = lvl.strip().upper() or "INFO"
    root.setLevel(lvl)
    root.propagate = True          # 让 pytest 的 caplog 能抓到（见模块 docstring）

    for h in list(root.handlers):  # force 重配时先清空，避免重复输出
        root.removeHandler(h)

    fmt = logging.Formatter(_FMT, datefmt=_DATEFMT)
    flt = _RunIdFilter()
    # filter 挂在 **logger** 上而不是只挂 handler：Logger.handle 在 callHandlers
    # 之前就会过滤，因此无论记录最终被哪个 handler 消费（包括 pytest 的 caplog、
    # 或调用方自己加的 handler），`record.run_id` 都已就位。
    if not any(isinstance(f, _RunIdFilter) for f in root.filters):
        root.addFilter(flt)
    sh = _LazyStderrHandler()
    sh.setFormatter(fmt)
    sh.addFilter(flt)
    root.addHandler(sh)

    path = log_file or os.environ.get(LOG_FILE_ENV)
    if path:
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(str(path), encoding="utf-8")
            fh.setFormatter(fmt)
            fh.addFilter(flt)
            root.addHandler(fh)
        except OSError as e:
            # 文件日志没配上就等于"以为有可回溯记录、其实没有"——必须出声。
            root.warning("日志文件不可写，已降级为仅 stderr：%s（%s）", path, e)

    _configured = True
    return root


def get_logger(name: str = _LOGGER_ROOT) -> logging.Logger:
    """取一个子 logger；**首次调用会顺带完成 setup()**（调用方不必记得配置）。"""
    setup()
    if name == _LOGGER_ROOT or name.startswith(_LOGGER_ROOT + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_LOGGER_ROOT}.{name}")


def is_configured() -> bool:
    """是否已配置（供测试与启动自检使用）。"""
    return _configured
