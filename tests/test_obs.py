"""序 3 守护：统一日志出口 + run_id 贯通（`extensions/common/obs.py`）。

守的是什么
----------
可观测性"配好了"是看不见的——没有断言时，一次重构就能让它悄悄退化回
"全仓没日志 / 异常被吞掉"，而且**不会有任何测试变红**（这正是当初 CI 连红
15 次却定位不到原因的形态）。所以这里把它钉死成两类断言：

1. **运行时行为**：`setup()` 幂等、run_id 唯一并可注入日志记录、文件 handler 可用。
2. **静态纪律（AST 扫描 A 世界）**：
   - R1 不得再有 `print(..., file=sys.stderr)`（诊断必须走日志）；
   - R2 不得再有 `except` 块内的 `print(`（错误报告必须走日志）；
   - R3 每个**静默** `except`（体只有 pass/continue/return）必须有 log 调用或分类注释。

边界（有意为之，不是遗漏）
--------------------------
`print` 并没有被全面禁止：**"给人看的结果呈现"**（`list` 的表格、`defects` 的
Markdown、`--json` 载荷）留在 stdout 是**产品行为**（要能被 `|` 管道接走）。
本测试只针对"排障用的诊断"这条通道。依据见
`docs/HARNESS_ARCHITECTURE_REVIEW.md` §4.2 / §7 序 3。
"""
from __future__ import annotations

import ast
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "extensions"))

from common import obs  # noqa: E402

# A 世界 = 确定性流水线（extensions / project_manager / web_console）。
# B 世界（src/agentic_explorer）尚未接线，其护栏属 §7 序 5，不在本测试范围内。
A_WORLD = (
    [ROOT / "project_manager.py", ROOT / "run_console.py",
     ROOT / "software_testing_agent.py"]
    + sorted((ROOT / "extensions").rglob("*.py"))
    + sorted((ROOT / "web_console").rglob("*.py"))
)

_TAG_OK = "可忽略"
_TAG_MUST = "必须出声"
_SILENT_TAGS = (_TAG_OK, _TAG_MUST)


# --------------------------------------------------------------------------- #
# 运行时行为
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _restore_logging():
    """每个用例后把全局日志配置还原为默认，避免互相污染（setup 是进程级幂等）。"""
    yield
    obs.setup(level="INFO", log_file=None, force=True)


def test_setup_is_idempotent():
    """重复调用不叠加 handler —— 否则每行日志会被打印 N 次。"""
    obs.setup(force=True)
    n = len(logging.getLogger(obs._LOGGER_ROOT).handlers)
    obs.setup()
    obs.setup()
    assert len(logging.getLogger(obs._LOGGER_ROOT).handlers) == n


def test_setup_force_replaces_handlers(tmp_path):
    """force 重配时旧 handler 被清掉（换文件不会双写）。"""
    obs.setup(log_file=str(tmp_path / "a.log"), force=True)
    obs.setup(log_file=str(tmp_path / "b.log"), force=True)
    name = tmp_path / "b.log"
    obs.get_logger("unit").warning("to-b")
    assert name.is_file() and "to-b" in name.read_text(encoding="utf-8")


def test_new_run_id_unique_and_readable():
    """唯一性必须是**结构性**的，不能靠概率。

    这条断言最初就是红的：4 位 hex 在同一秒内取 200 次约 1/4 概率相撞 ——
    于是实现改为"时间+随机"负责跨进程、"进程内序号"负责同进程绝不重复。
    """
    ids = {obs.new_run_id("x") for _ in range(500)}
    assert len(ids) == 500                    # 同进程内绝不重复（500 远超概率能保证的量）
    one = obs.new_run_id("cli")
    assert one.startswith("cli-") and one.count("-") == 2   # <prefix>-<HHMMSS>-<随机+序号>


def test_run_id_flows_into_log_records(caplog):
    """run_id 必须自动出现在日志记录上（靠 filter，不靠调用方记得传参）。"""
    obs.setup(force=True)
    obs.set_run_id("rid-test-1234")
    with caplog.at_level(logging.WARNING):
        obs.get_logger("unit").warning("hello")
    hit = [r for r in caplog.records if r.getMessage() == "hello"]
    assert hit, "日志没有到达记录层（propagate 或 filter 配错了）"
    assert hit[0].run_id == "rid-test-1234"


def test_run_id_defaults_to_dash():
    assert obs.set_run_id("") == "-"
    assert obs.get_run_id() == "-"


def test_adopt_env_run_id(monkeypatch):
    """控制台传入的 STA_RUN_ID 必须被沿用（这才是"同一次运行"的关键）。"""
    monkeypatch.setenv(obs.RUN_ID_ENV, "tid-abc")
    assert obs.adopt_env_run_id("cli") == "tid-abc"
    monkeypatch.delenv(obs.RUN_ID_ENV, raising=False)
    got = obs.adopt_env_run_id("cli")
    assert got.startswith("cli-") and got != "tid-abc"


def test_get_logger_prefix_and_identity():
    a = obs.get_logger("regression")
    b = obs.get_logger("sta.regression")
    assert a is b and a.name == "sta.regression"
    assert obs.get_logger() is logging.getLogger(obs._LOGGER_ROOT)


def test_unwritable_log_file_degrades_with_warning(caplog):
    """文件日志配不上时必须出声，且不能让进程起不来。"""
    bad = str(Path("NUL" if sys.platform == "win32" else "/dev/null") / "x" / "y.log")
    with caplog.at_level(logging.WARNING):
        root = obs.setup(log_file=bad, force=True)
    assert any("日志文件不可写" in r.getMessage() for r in caplog.records)
    # 仍然只剩 stderr 通道可用，但没有抛异常
    assert root.handlers


# --------------------------------------------------------------------------- #
# 静态纪律：AST 扫描 A 世界
# --------------------------------------------------------------------------- #
def _parse_files():
    for f in A_WORLD:
        if not f.is_file():
            continue
        src = f.read_text(encoding="utf-8")
        try:
            yield f, src, ast.parse(src)
        except SyntaxError as e:  # pragma: no cover
            pytest.fail(f"{f.relative_to(ROOT)} 无法解析：{e}")


def _rel(f: Path) -> str:
    return str(f.relative_to(ROOT)).replace("\\", "/")


def _is_stderr_print(node: ast.Call) -> bool:
    for kw in node.keywords:
        if kw.arg == "file":
            v = kw.value
            if isinstance(v, ast.Attribute) and v.attr == "stderr":
                return True
    return False


def _is_print(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "print")


def _is_silent(body) -> bool:
    """静默 = 体只有 pass / continue / return（无值）—— 连一行痕迹都不留。"""
    if not body:
        return True
    for st in body:
        if isinstance(st, (ast.Pass, ast.Continue)):
            continue
        if isinstance(st, ast.Return) and st.value is None:
            continue
        return False
    return True


def _has_log_call(body) -> bool:
    for st in body:
        for n in ast.walk(st):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "log"
                    and n.func.attr in ("debug", "info", "warning", "error",
                                        "critical", "exception")):
                return True
    return False


def _handler_source(src: str, node: ast.ExceptHandler) -> str:
    lines = src.splitlines()
    lo = node.lineno
    hi = node.body[-1].end_lineno or node.lineno if node.body else node.lineno
    return "\n".join(lines[lo - 1: hi])


def test_r1_no_stderr_print_in_a_world():
    """R1：诊断不许再走 `print(..., file=sys.stderr)` —— 它无法带 run_id、无法分级。"""
    bad = []
    for f, _src, tree in _parse_files():
        for n in ast.walk(tree):
            if _is_print(n) and _is_stderr_print(n):
                bad.append(f"{_rel(f)}:{n.lineno}")
    assert not bad, ("以下位置仍用 print(file=sys.stderr) 输出诊断，应改为 log.*：\n  "
                     + "\n  ".join(bad))


def test_r2_no_print_inside_except_in_a_world():
    """R2：except 块内的错误报告必须进日志 —— 否则它和业务输出混在同一个管道里。"""
    bad = []
    for f, _src, tree in _parse_files():
        for h in ast.walk(tree):
            if isinstance(h, ast.ExceptHandler):
                for st in h.body:
                    for n in ast.walk(st):
                        if _is_print(n):
                            bad.append(f"{_rel(f)}:{n.lineno}")
    assert not bad, ("以下 except 块内仍用 print 报告错误，应改为 log.*：\n  "
                     + "\n  ".join(bad))


def test_r3_every_silent_except_is_classified():
    """R3：静默 except 必须"要么记日志、要么写清为什么可以忽略"。"""
    bad = []
    for f, src, tree in _parse_files():
        for h in ast.walk(tree):
            if not isinstance(h, ast.ExceptHandler) or not _is_silent(h.body):
                continue
            blob = _handler_source(src, h)
            if _has_log_call(h.body):
                continue
            if any(t in blob for t in _SILENT_TAGS):
                continue
            bad.append(f"{_rel(f)}:{h.lineno}")
    assert not bad, ("以下静默 except 既没有日志、也没有分类注释（可忽略 / 必须出声）：\n  "
                     + "\n  ".join(bad))


def test_executors_and_entries_have_a_logger():
    """关键模块必须真的持有 logger —— 否则上面的迁移只是换了个说法。"""
    import generate_cases as gc
    import run_regression as rr
    import run_web as rw
    import project_manager as pm

    assert pm.log.name == "sta.project_manager"
    assert rr.log.name == "sta.regression"
    assert rw.log.name == "sta.web_testing"
    assert gc.log.name == "sta.generate_cases"


def test_console_passes_run_id_to_subprocess():
    """跨进程贯通：控制台起任务时必须把 STA_RUN_ID 传给子进程。"""
    src = (ROOT / "web_console" / "app.py").read_text(encoding="utf-8")
    assert "pm.RUN_ID_ENV" in src, "控制台没有把 run_id 传给子进程（跨进程会断线）"
    assert "set_run_id(tid)" in src, "工作线程内未设置 run_id（ContextVar 不跨线程继承）"
    pm_src = (ROOT / "project_manager.py").read_text(encoding="utf-8")
    assert "from common.obs import" in pm_src and "adopt_env_run_id" in pm_src
