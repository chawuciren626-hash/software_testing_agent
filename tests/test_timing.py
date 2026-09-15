"""效率口径的守护（审阅报告 §10 #2 / 序 8）。

守的是什么
----------
需求写着"提升测试效率"，此前**没有任何测量方式**。序 8 把口径定成：

    效率 := 一次 `run` 的**进程内墙钟耗时**，按阶段分解为明细 + 合计。

口径本身是产品决策，但**口径的诚实性**必须是机制保证的。本文件守四条最容易
被"悄悄放宽"的线：

1. **未执行 ≠ 0 秒**（同 CI 门禁三态的「未执行不是绿」）。没开启的阶段记
   `seconds=None`，绝不记 `0.0` —— 否则"没跑"在报告上看起来像"跑得飞快"。
2. **失败阶段照实计时**。抛异常的阶段也带耗时；把最慢的失败路径从统计里抹掉，
   只会让数字更好看（另一种假绿）。
3. **算不出就不猜**。一个阶段都没测到 → 合计为 `None`，不用 0 冒充。
4. **测不到的必须写出来**。"人力节省 / 质量"属**不可测**项，逐条列进载荷的
   `not_measured`；留白会被读成"这个数应该能算"。

另守接线：`cmd_run` 真的收集了 `StageOutcome`、`_stage_report` 真的**先定格口径再渲染**
（报告是持久化产物的渲染器，口径必须与别家同源）。
"""
from __future__ import annotations

import ast
import inspect
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _sub in ("", "extensions", "extensions/memory", "web_console"):
    _p = str(ROOT / _sub) if _sub else str(ROOT)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import project_manager as pm                                        # noqa: E402
from common import timing as tm                                     # noqa: E402
from common.pipeline import Stage, run_stages                       # noqa: E402


# =========================================================================== #
# 1) 口径常量：把"不承诺什么"钉在代码里
# =========================================================================== #
def test_spec_is_stable_and_versioned():
    assert tm.SPEC == "timing/1"


def test_scope_and_unit_declared():
    assert tm.SCOPE                                    # 口径范围非空（"需求 → 报告生成"）
    assert "需求" in tm.SCOPE and "报告" in tm.SCOPE


def test_not_measured_covers_human_saving_and_quality():
    """★ 不可测项必须**显式列出**：留白会被读成"这个数应该能算"。"""
    joined = "".join(tm.NOT_MEASURED)
    assert "人力" in joined, "必须声明'人力节省/人工替代率'不可测"
    assert "质量" in joined, "必须声明'耗时短不等于效率高'"
    assert tm.EXCLUDED, "口径范围内的排除项必须写出（报告渲染自身耗时）"


# =========================================================================== #
# 2) 条目构造：未执行 / 失败 / 鸭子类型容错
# =========================================================================== #
def test_stagetime_executed_property():
    assert tm.StageTime("a", 1.0).executed is True
    assert tm.StageTime("a", None).executed is False


def test_from_outcome_keeps_measured_seconds_even_when_failed():
    """★ 失败**不改**耗时：异常只贴标签，墙钟照记。"""
    e = RuntimeError("boom")
    st = tm.from_outcome("api", 2.5, e)
    assert st.seconds == 2.5 and st.executed is True
    assert st.ok is False
    assert st.error and st.error.startswith("RuntimeError: boom")


def test_from_outcome_truncates_error_detail():
    st = tm.from_outcome("api", 0.1, RuntimeError("x" * 500))
    assert st.error is not None and len(st.error) <= tm._ERROR_MAX


def test_from_outcome_none_seconds_means_not_executed():
    st = tm.from_outcome("web", None)
    assert st.seconds is None and st.executed is False and st.ok is True


def test_from_outcomes_is_duck_typed_and_tolerant():
    class _Fake:                       # 只长得像 StageOutcome，不继承它
        name = "req"
        seconds = 0.3
        error = None

    class _Broken:                     # 字段缺失：按"未执行"处理，而不是抛错
        pass

    out = tm.from_outcomes([_Fake(), _Broken()])
    assert out[0].name == "req" and out[0].seconds == 0.3
    assert out[1].name == "" and out[1].seconds is None


def test_from_outcomes_accepts_real_pipeline_outcomes():
    """与 pipeline 的鸭子类型契约必须真的对得上（防止一边改了字段名）。"""
    collected = []
    run_stages([Stage("a", lambda _c: None),
                Stage("b", lambda _c: None, enabled=lambda _c: False)],
               ctx=object(), on_stage=collected.append)
    stages = tm.from_outcomes(collected)
    assert [s.name for s in stages] == ["a", "b"]
    assert stages[0].seconds is not None and stages[1].seconds is None


# =========================================================================== #
# 3) 汇总：未执行不计入、失败照计、算不出就不猜
# =========================================================================== #
def test_total_sums_only_executed_stages():
    stages = [tm.StageTime("a", 1.0), tm.StageTime("b", None), tm.StageTime("c", 2.0)]
    assert tm.total_seconds(stages) == 3.0


def test_total_is_none_when_nothing_measured():
    """★ 算不出就不猜：**全部未执行** → None，绝不是 0.0。"""
    assert tm.total_seconds([tm.StageTime("a", None), tm.StageTime("b", None)]) is None
    assert tm.total_seconds([]) is None


def test_skipped_and_failed_name_lists():
    stages = [tm.StageTime("a", 1.0),
              tm.StageTime("b", None),
              tm.StageTime("c", 0.5, ok=False, error="RuntimeError: x")]
    assert tm.skipped(stages) == ["b"]
    assert tm.failed(stages) == ["c"]


def test_executed_excludes_unexecuted():
    stages = [tm.StageTime("a", 1.0), tm.StageTime("b", None)]
    assert [s.name for s in tm.executed(stages)] == ["a"]


# =========================================================================== #
# 4) 载荷：形状固定 + 诚实字段齐备
# =========================================================================== #
def _payload():
    stages = [tm.StageTime("requirements", 1.23456),
              tm.StageTime("perf", None),
              tm.StageTime("api", 0.5, ok=False, error="RuntimeError: x")]
    return tm.build(stages)


def test_build_shape_is_stable():
    p = _payload()
    assert p["spec"] == tm.SPEC and p["unit"] == "seconds"
    assert p["measured"]
    assert set(p) >= {"spec", "scope", "unit", "measured", "stages", "total_seconds",
                      "executed_count", "stage_count", "skipped", "failed",
                      "excluded", "not_measured"}


def test_build_marks_unexecuted_stage_with_none_seconds_not_zero():
    """★ 载荷里未执行阶段的 `seconds` 必须是 None —— 不是 0、不是 0.000。"""
    p = _payload()
    by_name = {s["name"]: s for s in p["stages"]}
    assert by_name["perf"]["seconds"] is None
    assert by_name["perf"]["executed"] is False
    assert by_name["requirements"]["seconds"] == 1.235      # 四舍五入到毫秒


def test_build_total_and_counts():
    p = _payload()
    assert p["total_seconds"] == 1.735                      # 1.23456 + 0.5
    assert p["stage_count"] == 3 and p["executed_count"] == 2
    assert p["skipped"] == ["perf"] and p["failed"] == ["api"]


def test_build_carries_honesty_fields():
    p = _payload()
    assert p["not_measured"] and p["excluded"]
    assert "api" in p["failed"]
    assert p["stages"][2].get("error")                   # 失败原因留指针（已在日志里有全栈）


# =========================================================================== #
# 5) 读取：形状不对一律 None（"缺字段不算数"）
# =========================================================================== #
@pytest.mark.parametrize("bad", [
    None, {}, "timing", 123, [],
    {"spec": "timing/0", "stages": []},                  # 版本不符
    {"spec": "timing/1"},                                # 缺 stages
    {"spec": "timing/1", "stages": "not-a-list"},
])
def test_load_rejects_wrong_shape(bad):
    assert tm.load(bad) is None


def test_load_roundtrips_built_payload():
    p = _payload()
    assert tm.load(p) is p


# =========================================================================== #
# 6) 呈现：未执行显式写出，绝不呈现成 0
# =========================================================================== #
@pytest.mark.parametrize("sec,expect", [
    (None, "未执行"),
    (0.0, "0ms"),
    (0.05, "50ms"),
    (1.5, "1.5s"),
    (75.0, "1m15s"),
])
def test_format_seconds(sec, expect):
    assert tm.format_seconds(sec) == expect


def test_format_seconds_distinguishes_none_from_zero():
    """★ 这是整套口径的分水岭：None →「未执行」，0.0 →「0ms」。二者不可混同。"""
    assert tm.format_seconds(None) != tm.format_seconds(0.0)
    assert tm.format_seconds(None) == "未执行"


def test_summary_line_reports_skipped_and_counts():
    line = tm.summary_line(_payload())
    assert "流水线耗时" in line
    assert "已执行 2/3" in line
    assert "perf" in line


def test_summary_line_handles_missing_payload():
    assert tm.summary_line(None) == "效率：未测量"


def test_render_lines_marks_unexecuted_and_undeclared():
    lines = tm.render_lines(_payload())
    text = "\n".join(lines)
    assert "perf" in text and "未执行" in text
    assert "api" in text and "异常" in text
    assert "未测量" in text and "人力" in text
    assert "已排除" in text


def test_render_lines_without_payload_says_not_measured():
    text = "\n".join(tm.render_lines(None))
    assert "无数据" in text and "不是 0 秒" in text


# =========================================================================== #
# 7) 接线：cmd_run 收耗时 / report 先定格再渲染 / 可选阶段声明开关 / 卡片占位
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


def test_cmd_run_passes_on_stage_collector_and_prints_summary():
    src = textwrap.dedent(inspect.getsource(pm.cmd_run))
    assert "on_stage" in src, "cmd_run 必须挂上耗时收集回调"
    assert "summary_line" in src, "cmd_run 收尾必须打印效率口径摘要"


def test_stage_report_finalizes_timing_before_rendering():
    """★ 报告是持久化产物的渲染器：口径必须**先落盘再渲染**（与别家卡片同源）。"""
    src = textwrap.dedent(inspect.getsource(pm._stage_report))
    assert "_finalize_timing(ctx)" in src and "_step_report(" in src, "_stage_report 两个动作都在"
    assert src.index("_finalize_timing(ctx)") < src.index("_step_report("), \
        "必须先定格效率口径，再渲染报告"


def test_finalize_timing_writes_payload_into_run_meta():
    src = textwrap.dedent(inspect.getsource(pm._finalize_timing))
    assert '"timing"' in src or "'timing'" in src
    assert "_write_run_meta" in src


def test_optional_stages_declare_their_switch_in_registry():
    """★ 开关写在**声明处**（`enabled=`），不写在阶段体内"进去先 return"。"""
    stages = {s.name: s for s in pm._run_stages()}
    for name in ("perf", "web", "agentic"):
        assert stages[name].enabled is not None, f"{name} 必须在注册表声明开关"

    class _Ctx:
        args = type("A", (), {})()          # 什么开关都不给

    for name in ("perf", "web", "agentic"):
        assert stages[name].enabled(_Ctx()) is False, f"{name} 默认应为关闭"

    class _On:
        args = type("A", (), {"perf": True, "web": True, "explore": True})()

    for name in ("perf", "web", "agentic"):
        assert stages[name].enabled(_On()) is True, f"{name} 显式开启后应为启用"


def test_optional_stage_bodies_no_longer_guard_the_switch():
    """★ 开关不得在两处各判一次（口径唯一）——阶段体里不应再有 flag 判断。"""
    for fn in (pm._stage_perf, pm._stage_web, pm._stage_agentic):
        src = textwrap.dedent(inspect.getsource(fn))
        assert 'getattr(ctx.args, "perf"' not in src
        assert 'getattr(ctx.args, "web"' not in src
        assert 'getattr(ctx.args, "explore"' not in src


def test_report_template_has_timing_slots():
    src = inspect.getsource(pm._step_report)
    assert "{timing_item}" in src and "{timing_card_html}" in src
    assert "_timing_card_html" in src and "timing_mod.load" in src


# =========================================================================== #
# 8) 卡片渲染：诚实声明不得在渲染层被静默丢掉
# =========================================================================== #
def test_timing_card_marks_unexecuted_stage_as_not_zero():
    html = pm._timing_card_html(_payload())
    assert "perf" in html and "未执行" in html
    assert "不是 0 秒" in html
    assert "0ms" not in html.split("perf")[1][:400], \
        "未执行阶段被渲染成了 0ms（与'跑得飞快'无法区分）"


def test_timing_card_states_what_is_not_measured():
    """★ 卡片必须写明"本口径不测什么"——否则留白会被读成"这个数应该能算"。"""
    html = pm._timing_card_html(_payload())
    assert "不测" in html
    assert "人力" in html and "质量" in html
    assert "已排除" in html


def test_timing_card_handles_skipped_and_failed_lists():
    html = pm._timing_card_html(_payload())
    assert "未执行：perf" in html
    assert "异常阶段：api" in html
