"""快照重复写入的守护测试（HARNESS_ARCHITECTURE_REVIEW.md §3.1）。

**这个缺陷长什么样**：控制台起任务时执行的是普通 `project_manager.py` 子进程：
  - `run` / `regression`：子进程内部**自己会写一条**快照（`project_manager.py:1796` / `:1837`）；
  - 子进程退出后，控制台**又写一条**（`app.py` 的 `_spawn_task` finally 段）。
于是从控制台触发的 run / regression **每次留两份同内容快照** → `lessons` 的
"历史失败次数"约虚高 2×、趋势点数翻倍，而且**没有任何断言会变红**（典型的假绿：
信号被污染但无人报警）。

**修法**（§8 D1=③ 保留两处 + 幂等去重）：控制台这条改成"兜底"——子进程已写过就跳过，
只有子进程被 1800s 超时 kill / 提前崩溃没写成时才补一条。`rerun` 子进程不写快照，
控制台是唯一来源，必须写，不能一起跳过。

**变异验证**（写完之后验证这些测试真的会红）：
  - 把 `_should_record_snapshot` 的 `kind == "rerun"` 分支删掉 → `test_rerun_*` 变红；
  - 让它无条件 `return True` → `test_run_task_writes_single_snapshot` 变红（delta 变 2）；
  - 让它对 run 无条件 `return False` → `test_run_fallback_when_subprocess_wrote_nothing` 变红。
"""
from __future__ import annotations

import io
import json
import subprocess
import threading
import types
from pathlib import Path

import pytest

import app as web_app


# 一份"有内容"的回归结果（insert_snapshot 对 total 为空的结果不留档）
_REG = {
    "total": 3, "passed": 2, "failed": 1, "skipped": 0, "all_pass": False,
    "results": [
        {"name": "登录成功", "result": "PASS", "detail": ""},
        {"name": "登录失败锁定", "result": "FAIL", "detail": "断言失败"},
        {"name": "查询订单", "result": "PASS", "detail": ""},
    ],
}


class _FakePopen:
    """模拟 project_manager 子进程：可选地在退出前**自行写一条快照**。

    stdout 用 StringIO（可迭代 + 有 close），对齐 app._spawn_task 的逐行读取。
    """

    def __init__(self, args, *, self_writes: bool, pid: str) -> None:
        self.args = args
        self.stdout = io.StringIO("== 核心业务回归 ==\n✅ 完成\n")
        self._self_writes = self_writes
        self._pid = pid
        self.killed = False

    def wait(self) -> int:
        if self._self_writes:      # 子进程在退出前留档（真实 project_manager 的行为）
            web_app.run_store.insert_snapshot(self._pid, _REG, trigger="run")
        return 0

    def kill(self) -> None:
        self.killed = True


@pytest.fixture
def store(tmp_path: Path):
    """把 run_store 指向临时库，测试结束还原到真实库路径，避免污染其它测试。"""
    web_app.run_store.init_db(tmp_path / "runs.db")
    yield web_app.run_store
    web_app.run_store.init_db(web_app.DATA_ROOT / "runs.db")


@pytest.fixture
def projects_dir(tmp_path: Path) -> Path:
    """准备一个带 artifacts/regression.json 的假项目目录。"""
    root = tmp_path / "projects"
    art = root / "demo" / "artifacts"
    art.mkdir(parents=True)
    (art / "regression.json").write_text(
        json.dumps(_REG, ensure_ascii=False), encoding="utf-8")
    return root


def _run_task(monkeypatch, *, kind: str, pid: str, projects_dir: Path,
              self_writes: bool) -> str:
    """跑一次控制台任务（子进程被替换为 _FakePopen），返回 tid。

    ⚠️ 必须 **join 工作线程** 而不是轮询 task["status"]：`_spawn_task` 在 `try` 里就把
    status 置为 success，而快照写入在随后的 `finally` —— 轮询 status 会在写快照**之前**
    就返回（测试自身的竞态）。join 线程才能保证 finally 已执行完（写入已完成）。
    """
    def _factory(*args, **kwargs):
        return _FakePopen(args, self_writes=self_writes, pid=pid)

    shim = types.SimpleNamespace(
        PIPE=subprocess.PIPE, STDOUT=subprocess.STDOUT, Popen=_factory)
    monkeypatch.setattr(web_app, "subprocess", shim)
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", projects_dir)

    alive_before = set(threading.enumerate())
    tid = web_app._spawn_task(kind, pid, [kind, pid])
    workers = [t for t in threading.enumerate() if t not in alive_before]
    for t in workers:
        t.join(timeout=5)
    assert workers and not any(t.is_alive() for t in workers), \
        "控制台任务线程未在 5s 内结束"
    return tid


# ---------------------------------------------------------------------------
# 单元：去重决策本身
# ---------------------------------------------------------------------------
def test_should_record_snapshot_rules(store):
    """决策表：哪些情况控制台该补写、哪些该跳过。"""
    pid = "demo"
    store.insert_snapshot(pid, _REG, trigger="run")      # 库里已有 1 条
    assert store.count_snapshots(pid) == 1

    # run / regression：子进程写了（条数增加）→ 控制台跳过
    assert web_app._should_record_snapshot("run", pid, snaps_before=0) is False
    assert web_app._should_record_snapshot("regression", pid, snaps_before=0) is False

    # run / regression：子进程没写（条数没变）→ 控制台兜底补一条
    assert web_app._should_record_snapshot("run", pid, snaps_before=1) is True
    assert web_app._should_record_snapshot("regression", pid, snaps_before=1) is True

    # rerun：子进程不写快照，控制台是唯一来源 → 无论基数如何都必须写
    assert web_app._should_record_snapshot("rerun", pid, snaps_before=1) is True
    assert web_app._should_record_snapshot("rerun", pid, snaps_before=0) is True

    # 探针失败（snaps_before=None）→ 宁可多写兜底，也不静默丢档
    assert web_app._should_record_snapshot("run", pid, snaps_before=None) is True

    # 非快照类任务 / 看板（pid="*"）→ 一律不写
    assert web_app._should_record_snapshot("perf-security", pid, 0) is False
    assert web_app._should_record_snapshot("web", pid, 0) is False
    assert web_app._should_record_snapshot("dashboard", "*", 0) is False


# ---------------------------------------------------------------------------
# 集成：整条控制台任务链路，单任务快照增量必须 ≤ 1
# ---------------------------------------------------------------------------
def test_run_task_writes_single_snapshot(store, projects_dir, monkeypatch):
    """子进程自己写了快照时，控制台不得再写 → 增量恰为 1（缺陷修复前是 2）。"""
    before = store.count_snapshots("demo")
    _run_task(monkeypatch, kind="run", pid="demo",
              projects_dir=projects_dir, self_writes=True)
    assert store.count_snapshots("demo") - before == 1


def test_regression_task_writes_single_snapshot(store, projects_dir, monkeypatch):
    """regression 同理：单任务增量恰为 1。"""
    before = store.count_snapshots("demo")
    _run_task(monkeypatch, kind="regression", pid="demo",
              projects_dir=projects_dir, self_writes=True)
    assert store.count_snapshots("demo") - before == 1


def test_run_fallback_when_subprocess_wrote_nothing(store, projects_dir, monkeypatch):
    """子进程没写成（超时 kill / 提前崩溃）→ 控制台兜底补一条，仍为 1。"""
    before = store.count_snapshots("demo")
    _run_task(monkeypatch, kind="run", pid="demo",
              projects_dir=projects_dir, self_writes=False)
    assert store.count_snapshots("demo") - before == 1


def test_rerun_still_records_snapshot(store, projects_dir, monkeypatch):
    """rerun 子进程不写快照 → 控制台必须写，增量恰为 1（去重不能把它一起跳过）。"""
    before = store.count_snapshots("demo")
    _run_task(monkeypatch, kind="rerun", pid="demo",
              projects_dir=projects_dir, self_writes=False)
    assert store.count_snapshots("demo") - before == 1


def test_non_snapshot_task_records_nothing(store, projects_dir, monkeypatch):
    """perf-security 等非回归类任务不留快照 → 增量为 0。"""
    before = store.count_snapshots("demo")
    _run_task(monkeypatch, kind="perf-security", pid="demo",
              projects_dir=projects_dir, self_writes=False)
    assert store.count_snapshots("demo") - before == 0
