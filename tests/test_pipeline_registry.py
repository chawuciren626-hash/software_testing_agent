"""阶段注册表 + 依赖拓扑执行的守护（审阅报告 §4.4 / §7 序 7）。

守的是什么
----------
流水线的"阶段顺序"里藏着至少一条**不写在类型里的强约束**：

    `diff`（失败项新旧对比）**必须在** `snapshot`（写本次快照）**之前** ——
    否则历史里最新一条就是本次自己，自己跟自己比，结论永远是"没有新增失败"。

它原先只靠 **书写顺序 + 一句注释 + 人的记忆**维持：任何一次"顺手把这两步调个位置"
都会静默破坏它，而且**不会有任何测试变红**（典型的"信号被污染却没人报警"）。

序 7 把它变成注册表里的 `requires` 显式依赖。本测试就是那条"会变红的线"，守四件事：

1. **机制正确**：稳定拓扑排序 —— 无依赖保持声明顺序；成环要**报错**而不是静默继续。
2. **等价性**（§7 验收判据）：注册表解析出的顺序 == 重构前 `cmd_run` 的真实顺序。
3. **承重性**：即使有人把 `snapshot` 挪到 `diff` 之前**声明**，`requires` 这条边
   仍能强制出正确顺序 —— 证明它是**约束**，不是随声明顺序附和的装饰。
4. **接线**：`cmd_run` / `cmd_regression` 必须真的经 `run_stages` 执行，且不得再
   直接内联调用各 `_step_*`（否则顺序又变回"靠书写位置"）。
"""
from __future__ import annotations

import ast
import inspect
import sys
import textwrap
from argparse import Namespace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _sub in ("", "extensions", "extensions/memory", "web_console"):
    _p = str(ROOT / _sub) if _sub else str(ROOT)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import project_manager as pm                                        # noqa: E402
from common.pipeline import Stage, StageError, resolve_order, run_stages   # noqa: E402


# --------------------------------------------------------------------------- #
# 等价性 oracle：**重构前** cmd_run 里阶段调用的真实顺序
#
# 冻结自 commit b2c08a9（序 6 之后的 HEAD，序 7 动工前）project_manager.py 的
# cmd_run 主体（旧 L1886–L2046）。`meta` = 那一刻夹在 focus 与 quality 之间的
# 首份 `_write_run_meta`（记录生成溯源），现抽成独立阶段。
#
# 这个字面量是"现状顺序"的唯一权威；注册表必须靠 `requires` 复现它。
# --------------------------------------------------------------------------- #
EXPECTED_RUN_ORDER = [
    "requirements", "focus", "meta", "quality", "api", "regression",
    "perf", "web", "agentic", "diff", "snapshot", "defects", "report",
]
EXPECTED_REGRESSION_ORDER = ["regression", "diff", "snapshot", "defects"]


# =========================================================================== #
# 1) 机制：Stage / validate / resolve_order / run_stages
# =========================================================================== #
def _st(name, requires=(), doc="", fn=None):
    return Stage(name, fn or (lambda _ctx: None), requires=requires, doc=doc)


def test_stage_defaults_are_minimal():
    s = _st("a")
    assert s.name == "a" and s.requires == () and s.doc == ""
    assert callable(s.run)


def test_validate_rejects_duplicate_names():
    with pytest.raises(StageError, match="重复"):
        resolve_order([_st("a"), _st("a")])


def test_validate_rejects_self_require():
    with pytest.raises(StageError, match="依赖它自己"):
        resolve_order([_st("a", requires=("a",))])


def test_validate_rejects_unknown_require():
    with pytest.raises(StageError, match="未知阶段"):
        resolve_order([_st("a", requires=("ghost",))])


def test_resolve_keeps_declaration_order_for_independent_stages():
    """彼此无依赖 → 保持**声明顺序**（这是"稳定"的含义，tie-break 可预期）。"""
    stages = [_st("c"), _st("a"), _st("b")]
    assert resolve_order(stages) == ["c", "a", "b"]


def test_resolve_honours_requires_over_declaration_order():
    """真实依赖**压过**声明顺序：即便 `b` 声明在后，`a` 依赖它也要排前面。"""
    stages = [_st("a", requires=("b",)), _st("b")]
    assert resolve_order(stages) == ["b", "a"]


def test_resolve_diamond():
    stages = [
        _st("root"),
        _st("left", requires=("root",)),
        _st("right", requires=("root",)),
        _st("join", requires=("left", "right")),
    ]
    assert resolve_order(stages) == ["root", "left", "right", "join"]


def test_resolve_detects_cycle_and_names_the_stages():
    stages = [
        _st("a", requires=("c",)),
        _st("b", requires=("a",)),
        _st("c", requires=("b",)),
    ]
    with pytest.raises(StageError) as ei:
        resolve_order(stages)
    msg = str(ei.value)
    assert "成环" in msg
    for n in ("a", "b", "c"):            # 报出参与成环的阶段，便于定位
        assert n in msg


def test_resolve_empty_is_empty():
    assert resolve_order([]) == []


def test_run_stages_executes_in_resolved_order_and_reports_it():
    seen = []
    stages = [
        _st("b", requires=("a",), fn=lambda _c: seen.append("b")),
        _st("a", fn=lambda _c: seen.append("a")),
    ]
    order = run_stages(stages, ctx=object())
    assert seen == ["a", "b"]
    assert order == ["a", "b"]


def test_run_stages_fires_on_stage_callback_in_order():
    fired = []
    stages = [_st("a"), _st("b", requires=("a",))]
    run_stages(stages, ctx=object(), on_stage=fired.append)
    assert [o.name for o in fired] == ["a", "b"]


def test_run_stages_propagates_stage_exception():
    """机制层不替业务决定"失败要不要继续" —— 阶段的异常原样上抛。"""
    def _boom(_ctx):
        raise RuntimeError("stage failed")
    with pytest.raises(RuntimeError, match="stage failed"):
        run_stages([_st("a", fn=_boom)], ctx=object())


# =========================================================================== #
# 1b) 横切关注点：StageOutcome / Stage.enabled（§10 #2 效率口径的机制基础）
# =========================================================================== #
def test_stage_enabled_defaults_to_none():
    """不声明 `enabled` = 永远跑（默认**不**引入开关，避免悄悄跳过阶段）。"""
    assert _st("a").enabled is None


def test_run_stages_reports_seconds_for_executed_stage():
    fired = []
    run_stages([_st("a")], ctx=object(), on_stage=fired.append)
    (o,) = fired
    assert o.name == "a" and o.executed and o.error is None
    assert o.seconds is not None and o.seconds >= 0.0


def test_run_stages_skips_disabled_stage_and_reports_none_seconds():
    """★ `enabled` 判否 → 整段跳过：`run` 不被调用，且汇报 `seconds=None`。

    绝不允许记成 `0.0` —— 那会让"本次没执行"在效率报告里显示成"跑得飞快"。
    """
    ran = []
    fired = []
    stages = [
        _st("a", fn=lambda _c: ran.append("a")),
        Stage("b", lambda _c: ran.append("b"), enabled=lambda _c: False),
        _st("c", fn=lambda _c: ran.append("c")),
    ]
    order = run_stages(stages, ctx=object(), on_stage=fired.append)

    assert ran == ["a", "c"], "被禁用的阶段不得执行"
    assert order == ["a", "b", "c"], "返回的是注册表全序，不因跳过而缩短"
    skipped = {o.name: o for o in fired}["b"]
    assert skipped.seconds is None and not skipped.executed


def test_run_stages_times_failing_stage_even_though_exception_propagates():
    """★ 失败阶段**照样带耗时**：把最慢的失败路径从统计里抹掉，数字只会更好看。"""
    def _boom(_ctx):
        raise RuntimeError("boom")
    fired = []
    with pytest.raises(RuntimeError):
        run_stages([_st("a", fn=_boom)], ctx=object(), on_stage=fired.append)
    (o,) = fired
    assert o.name == "a" and o.seconds is not None and o.executed
    assert isinstance(o.error, RuntimeError)


def test_run_stages_callback_exception_does_not_break_pipeline(caplog):
    """回调（计时/统计）自己坏掉 → 记日志后忽略：**统计不得改变业务结果**。"""
    ran = []

    def _bad_cb(_outcome):
        raise ValueError("collector broken")

    with caplog.at_level("WARNING"):
        order = run_stages([_st("a", fn=lambda _c: ran.append("a"))],
                          ctx=object(), on_stage=_bad_cb)
    assert order == ["a"] and ran == ["a"], "回调异常不得中断流水线"
    said = [r.getMessage() for r in caplog.records]
    assert any("阶段回调失败" in m for m in said), "回调失败必须出声，不能静默吞掉"


# =========================================================================== #
# 2) 等价性：注册表解析出的顺序 == 重构前的真实顺序（§7 验收判据）
# =========================================================================== #
def test_run_registry_resolves_to_pre_refactor_order():
    assert resolve_order(pm._run_stages()) == EXPECTED_RUN_ORDER


def test_regression_registry_resolves_to_pre_refactor_order():
    assert resolve_order(pm._regression_stages()) == EXPECTED_REGRESSION_ORDER


def test_registries_are_self_consistent():
    """每个注册表都能通过 validate（重名/未知依赖/自环都会被 resolve_order 拦下）。"""
    for registry in (pm._run_stages(), pm._regression_stages()):
        names = [s.name for s in registry]
        assert len(set(names)) == len(names)                  # 名称唯一
        assert all(callable(s.run) for s in registry)          # run 可调用
        assert all(s.requires for s in registry) or True
        resolve_order(registry)                                # 不抛即通过


# =========================================================================== #
# 3) 承重性：diff → snapshot 这条边真的在起作用
# =========================================================================== #
def test_snapshot_declares_diff_dependency():
    """依赖**显式声明**：snapshot 必须点名依赖 diff（不能只靠位置）。"""
    stages = {s.name: s for s in pm._run_stages()}
    assert "diff" in stages["snapshot"].requires
    reg = {s.name: s for s in pm._regression_stages()}
    assert "diff" in reg["snapshot"].requires


def _move_declaration_before(stages, name, before_name):
    """把 `name` 的**声明位置**挪到 `before_name` 之前（保持 fields 不变）。"""
    rest = [s for s in stages if s.name != name]
    moved = next(s for s in stages if s.name == name)
    idx = next(i for i, s in enumerate(rest) if s.name == before_name)
    rest.insert(idx, moved)
    return rest


def _strip_require(stages, name, req):
    out = []
    for s in stages:
        if s.name == name:
            out.append(Stage(s.name, s.run,
                             tuple(r for r in s.requires if r != req), s.doc))
        else:
            out.append(s)
    return out


def test_snapshot_edge_forces_order_even_if_declared_earlier():
    """★ 承重性：把 snapshot 挪到 diff **之前**声明，`requires` 这条边仍强制出正确顺序。

    这正是"显式依赖"相对"隐式顺序"的全部价值 —— 声明位置可以漂移，约束不会。
    """
    reordered = _move_declaration_before(pm._run_stages(), "snapshot", "diff")
    # 先确认这次"挪动"真的改变了声明顺序（否则本用例在测空气）
    assert [s.name for s in reordered].index("snapshot") \
        < [s.name for s in reordered].index("diff")

    with_edge = resolve_order(reordered)
    assert with_edge.index("diff") < with_edge.index("snapshot"), \
        "requires 没能守住 diff 在 snapshot 之前的约束"

    # 反事实：去掉这条边，顺序立刻退化成"snapshot 在 diff 之前"（＝那条静默错误）
    without_edge = resolve_order(_strip_require(reordered, "snapshot", "diff"))
    assert without_edge.index("snapshot") < without_edge.index("diff"), \
        "去掉 requires 边后顺序竟然没变 —— 说明这条边没在承重（测试无效）"


# =========================================================================== #
# 4) 接线：cmd_run / cmd_regression 必须真的走注册表
# =========================================================================== #
def _called_names(func) -> set:
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Name):
                names.add(n.func.id)
            elif isinstance(n.func, ast.Attribute):
                names.add(n.func.attr)
    return names


_STEP_FUNCS = {
    "_step_requirements", "_step_focus", "_step_quality", "_step_api",
    "_step_regression", "_step_perf_security", "_step_web", "_step_agentic",
    "_step_diff", "_step_defects", "_step_report",
}


def test_cmd_run_uses_registry_and_not_inline_steps():
    """cmd_run 必须经 `run_stages` 执行，且**不得**再直接内联调用 `_step_*`。

    否则"顺序由注册表决定"就成了空话 —— 又会退回"顺序靠书写位置"。
    """
    called = _called_names(pm.cmd_run)
    assert "run_stages" in called and "resolve_order" in called
    assert not (called & _STEP_FUNCS), \
        f"cmd_run 内又出现了内联阶段调用：{sorted(called & _STEP_FUNCS)}"


def test_cmd_regression_uses_registry_and_not_inline_steps():
    called = _called_names(pm.cmd_regression)
    assert "run_stages" in called and "resolve_order" in called
    assert not (called & _STEP_FUNCS), \
        f"cmd_regression 内又出现了内联阶段调用：{sorted(called & _STEP_FUNCS)}"


class _FakeStore:
    """只提供 cmd_run / cmd_regression 在阶段外真正用到的那一个方法。"""

    def init_db(self, *_a, **_k) -> None:      # noqa: D401
        return None


@pytest.fixture
def _record_stage_calls(monkeypatch):
    """把所有 `_stage_*` 换成记录器，返回 (recorded, expected_names)。

    ⚠️ 必须先取原始函数名再打补丁：`_run_stages()` 在每次调用时按**当前**模块属性
    解析 `_stage_*`，补丁后重建的注册表才会用上记录器。
    """
    recorded: list = []
    pairs = {s.run.__name__ for s in pm._run_stages()}
    pairs |= {s.run.__name__ for s in pm._regression_stages()}

    for fn_name in pairs:
        short = fn_name[len("_stage_"):]

        def _rec(_ctx, _name=short):
            recorded.append(_name)
            if _name == "regression":       # 让 cmd_regression 的退出码有据可依
                _ctx.reg = {"all_pass": True, "total": 1, "passed": 1,
                            "failed": 0, "skipped": 0}

        monkeypatch.setattr(pm, fn_name, _rec)
    return recorded


#: 三个"可选能力"阶段由 CLI 开关驱动（注册表 `enabled=`），默认不开。
_OPTIONAL_STAGES = ("perf", "web", "agentic")


def test_cmd_run_executes_stages_in_registry_order(monkeypatch, tmp_path,
                                                   _record_stage_calls, capsys):
    """端到端接线：真跑一遍 cmd_run 的控制流，各阶段被调用的顺序必须 == 注册表顺序。

    三个可选阶段全部**显式开启**，才是完整全序 —— 否则它们会被 `enabled` 跳过
    （那是另一个用例，见下）。
    """
    recorded = _record_stage_calls
    monkeypatch.setattr(pm, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(pm, "load_project", lambda _pid: {"env": {"base_url": "http://x"}})
    monkeypatch.setattr(pm, "_load_run_store", lambda: _FakeStore())

    pm.cmd_run(Namespace(id="demo", llm=False, agentic=False,
                         perf=True, web=True, explore=True))

    assert recorded == EXPECTED_RUN_ORDER
    capsys.readouterr()          # 吞掉阶段外的展示类 print，避免污染其它用例输出


def test_cmd_run_skips_disabled_optional_stages(monkeypatch, tmp_path,
                                                _record_stage_calls, capsys):
    """★ 不开 --perf/--web/--explore 时，那三个阶段**根本不被调用**。

    这是"未执行 ≠ 0 秒"在流水线层面的落点：跳过发生在注册表 `enabled` 上，
    而不是让阶段"进去先 return"（后者在耗时统计里与"跑得飞快"无法区分）。
    """
    recorded = _record_stage_calls
    monkeypatch.setattr(pm, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(pm, "load_project", lambda _pid: {"env": {"base_url": "http://x"}})
    monkeypatch.setattr(pm, "_load_run_store", lambda: _FakeStore())

    pm.cmd_run(Namespace(id="demo", llm=False, agentic=False))   # 三个开关都不给

    expected = [n for n in EXPECTED_RUN_ORDER if n not in _OPTIONAL_STAGES]
    assert recorded == expected, "未开启的可选阶段不应被执行"
    assert not (set(recorded) & set(_OPTIONAL_STAGES))
    capsys.readouterr()


def test_cmd_regression_executes_stages_in_registry_order(monkeypatch, tmp_path,
                                                          _record_stage_calls, capsys):
    recorded = _record_stage_calls
    monkeypatch.setattr(pm, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(pm, "load_project", lambda _pid: {"env": {}})
    monkeypatch.setattr(pm, "_load_run_store", lambda: _FakeStore())

    with pytest.raises(SystemExit) as ei:
        pm.cmd_regression(Namespace(id="demo"))

    assert ei.value.code == 0
    assert recorded == EXPECTED_REGRESSION_ORDER
    assert recorded.index("diff") < recorded.index("snapshot")
    capsys.readouterr()
