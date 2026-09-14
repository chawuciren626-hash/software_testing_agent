"""B 世界护栏的**纯逻辑**守护测试（审阅报告 §3.2 / §7 序 5）。

这个文件刻意**零第三方依赖**：`guardrails.py` 只 import 标准库，所以它能在
CI 硬门禁（`requirements-dev.txt`，不装 langgraph/langchain）里直接跑。
真正需要基座依赖的接线测试另见 `tests/test_agentic_guardrails_wiring.py`。

守护的三条验收判据（§7 序 5）：
1. **硬 `max_turns` 是上限**（不是像 `max_steps` 那样的重置触发器）→ 用 `>=` 而非 `>` 来钉住；
2. **结束原因是枚举**（`completed/blocked/max-turns/budget-exhausted/error`）；
3. **目标达成由程序判定**，且**算不出就不猜**（`unknown` 是一等公民）。
"""
from __future__ import annotations

import pytest

from agentic_explorer.orchestration import guardrails as g


# --------------------------------------------------------------------------- #
# 枚举：结束原因必须是"可枚举"的（§3.2 第 2 条）
# --------------------------------------------------------------------------- #
def test_end_reason_is_the_five_state_enum():
    assert {r.value for r in g.EndReason} == {
        "completed", "blocked", "max-turns", "budget-exhausted", "error",
    }


def test_goal_verdict_has_unknown_as_first_class():
    assert {v.value for v in g.GoalVerdict} == {"achieved", "unachieved", "unknown"}


# --------------------------------------------------------------------------- #
# Limits：从环境变量解析（写错要退回默认，而不是把护栏关掉）
# --------------------------------------------------------------------------- #
def test_limits_defaults_scale_with_soft_max_steps():
    assert g.Limits.from_env({}, max_steps=30).max_turns == 120          # 4 个软周期
    assert g.Limits.from_env({}, max_steps=10).max_turns == 40           # 下限 40
    assert g.Limits.from_env({}).max_turns == g.DEFAULT_MAX_TURNS


def test_limits_env_overrides():
    lim = g.Limits.from_env({"AGENT_MAX_TURNS": "7", "AGENT_TOKEN_BUDGET": "1000",
                             "AGENT_STEP_TIMEOUT": "2.5"})
    assert (lim.max_turns, lim.token_budget, lim.step_timeout) == (7, 1000, 2.5)


@pytest.mark.parametrize("raw", ["abc", "", "  ", "0", "-3", "none"])
def test_limits_invalid_max_turns_falls_back_to_finite_default(raw):
    """打错的数字**绝不能**变成"无限" —— 那等于悄悄撤掉护栏。"""
    lim = g.Limits.from_env({"AGENT_MAX_TURNS": raw}, max_steps=30)
    assert lim.max_turns == 120 and lim.max_turns > 0


def test_limits_zero_means_unlimited_only_where_documented():
    lim = g.Limits.from_env({"AGENT_TOKEN_BUDGET": "-5", "AGENT_STEP_TIMEOUT": "nope"})
    assert lim.token_budget == 0 and lim.step_timeout == 0.0     # 0 = 显式不限 / 不启用


# --------------------------------------------------------------------------- #
# 硬上限判定：`>=` 是"上限"，`>` 才是"重置触发器"（软硬分离的落点）
# --------------------------------------------------------------------------- #
def test_check_limits_uses_inclusive_boundary():
    lim = g.Limits(max_turns=3)
    assert g.check_limits(lim, turns=2, tokens=0) is None
    assert g.check_limits(lim, turns=3, tokens=0) is g.EndReason.MAX_TURNS   # 恰好到顶即停


def test_check_limits_token_budget_takes_priority():
    lim = g.Limits(max_turns=100, token_budget=50)
    assert g.check_limits(lim, turns=1, tokens=49) is None
    assert g.check_limits(lim, turns=1, tokens=50) is g.EndReason.BUDGET_EXHAUSTED


def test_check_limits_zero_budget_never_fires():
    lim = g.Limits(max_turns=100, token_budget=0)
    assert g.check_limits(lim, turns=1, tokens=10 ** 9) is None


# --------------------------------------------------------------------------- #
# token 统计
# --------------------------------------------------------------------------- #
class _Msg:
    def __init__(self, usage=None, mid=None):
        if usage is not None:
            self.usage_metadata = usage
        self.id = mid


def test_count_tokens_duck_typed_and_defensive():
    msgs = [_Msg({"total_tokens": 10}), _Msg(None), _Msg({"total_tokens": "x"}),
            _Msg({"input_tokens": 5}), object()]
    assert g.count_tokens(msgs) == 10      # 缺 usage / 非 int / 没有该属性 → 一律不计


def test_count_new_tokens_excludes_history_by_id():
    """内层 agent 返回的是**完整**消息列表；不减历史就会把 token 严重高估。"""
    hist = [_Msg({"total_tokens": 100}, mid="h1")]
    full = hist + [_Msg({"total_tokens": 7}, mid="n1")]
    assert g.count_tokens(full) == 107                 # 全量
    assert g.count_new_tokens(full, hist) == 7         # 只算新增


def test_count_new_tokens_falls_back_to_slicing_when_no_ids():
    hist = [_Msg({"total_tokens": 100})]               # 没有 id
    full = hist + [_Msg({"total_tokens": 3})]
    assert g.count_new_tokens(full, hist) == 3


# --------------------------------------------------------------------------- #
# 目标判定：确定性断言 + 「算不出就不猜」
# --------------------------------------------------------------------------- #
_OK_NAV = {"action": "navigate", "ok": True, "page_url": "http://h/admin",
           "params": {"url": "http://h/admin"}}
_UNREACHABLE = {"action": "navigate", "ok": False, "error": "net::ERR_CONNECTION_REFUSED"}
_FORM_ERROR = {"action": "click", "ok": False, "error": "STATUS: ERROR / element not found"}


def test_judge_default_goal_fails_with_no_actions():
    v, why = g.judge_mission(g.MissionGoal(), tape=[])
    assert v is g.GoalVerdict.UNACHIEVED and any("成功动作" in w for w in why)


def test_judge_default_goal_passes_with_one_successful_action():
    v, why = g.judge_mission(g.MissionGoal(), tape=[_OK_NAV])
    assert v is g.GoalVerdict.ACHIEVED and why


def test_judge_flags_unreachable_but_not_ordinary_failures():
    """连接级失败 = 不可达；业务出错（元素找不到/表单报错）**不算** —— 那是产品缺陷。"""
    v, why = g.judge_mission(g.MissionGoal(), tape=[_UNREACHABLE])
    assert v is g.GoalVerdict.UNACHIEVED and any("不可达" in w for w in why)

    v2, _ = g.judge_mission(g.MissionGoal(), tape=[_FORM_ERROR, _OK_NAV])
    assert v2 is g.GoalVerdict.ACHIEVED, "普通动作失败不应被误判成环境不可达"


def test_judge_must_visit_hit_and_miss():
    goal = g.MissionGoal(must_visit=("/admin/products",))
    v, why = g.judge_mission(goal, explored_paths=["/admin"], tape=[_OK_NAV])
    assert v is g.GoalVerdict.UNACHIEVED and any("/admin/products" in w for w in why)

    v2, _ = g.judge_mission(g.MissionGoal(must_visit=("/admin",)),
                            explored_paths=["/admin/list"], tape=[_OK_NAV])
    assert v2 is g.GoalVerdict.ACHIEVED        # 子路径满足父路径要求


def test_judge_unknown_when_no_assertion_can_be_evaluated():
    """关掉全部断言 = "我不要求程序判" —— 此时必须如实说 unknown，不冒充通过。"""
    v, why = g.judge_mission(g.MissionGoal(require_actions=0, forbid_unreachable=False))
    assert v is g.GoalVerdict.UNKNOWN and why


def test_judge_unknown_when_required_path_has_no_evidence_at_all():
    """声明了目标路径但**没有任何访问轨迹** → 判不了，记 unknown（不猜成"没访问"）。"""
    goal = g.MissionGoal(must_visit=("/admin",), require_actions=0, forbid_unreachable=False)
    v, why = g.judge_mission(goal, explored_paths=[], tape=[])
    assert v is g.GoalVerdict.UNKNOWN and any("无法判断" in w for w in why)


def test_mission_goal_from_spec_is_defensive():
    assert g.MissionGoal.from_spec(None) == g.MissionGoal()
    assert g.MissionGoal.from_spec("oops") == g.MissionGoal()
    assert g.MissionGoal.from_spec({"must_visit": "/a"}).must_visit == ("/a",)   # 单个字符串也认
    spec = g.MissionGoal.from_spec({"must_visit": ["/a", "", None, "/b"],
                                    "require_actions": "3", "forbid_unreachable": False})
    assert spec.must_visit == ("/a", "/b") and spec.require_actions == 3
    assert spec.forbid_unreachable is False
    assert g.MissionGoal.from_spec({"require_actions": "垃圾"}).require_actions == 1  # 退回默认


# --------------------------------------------------------------------------- #
# 结束原因归类
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kwargs,expected", [
    ({"error": True}, g.EndReason.ERROR),
    ({"forced": g.EndReason.MAX_TURNS, "verdict": g.GoalVerdict.ACHIEVED}, g.EndReason.MAX_TURNS),
    ({"forced": g.EndReason.BUDGET_EXHAUSTED}, g.EndReason.BUDGET_EXHAUSTED),
    ({"verdict": g.GoalVerdict.ACHIEVED}, g.EndReason.COMPLETED),
    ({"verdict": g.GoalVerdict.UNACHIEVED}, g.EndReason.BLOCKED),
    ({"verdict": g.GoalVerdict.UNKNOWN}, g.EndReason.BLOCKED),
    ({}, g.EndReason.BLOCKED),
])
def test_classify_end_reason_mapping(kwargs, expected):
    """硬上限优先于 verdict：被硬停时目标根本没跑完，"断言恰好都过"也不该记 completed。"""
    assert g.classify_end_reason(**kwargs) is expected


def test_parse_end_reason_tolerates_junk():
    assert g.parse_end_reason("max-turns") is g.EndReason.MAX_TURNS
    assert g.parse_end_reason("") is None
    assert g.parse_end_reason(None) is None
    assert g.parse_end_reason("不认识的值") is None      # 认不出 → 当作"未设置"


# --------------------------------------------------------------------------- #
# 落档与渲染
# --------------------------------------------------------------------------- #
def test_build_result_is_json_serializable_and_complete():
    import json
    r = g.build_result(thread_id="t1", end_reason=g.EndReason.MAX_TURNS,
                       goal_verdict=g.GoalVerdict.UNACHIEVED, reasons=["x"],
                       turns=120, tokens=5, actions=3, bugs=1,
                       limits=g.Limits(max_turns=120))
    assert r["end_reason"] == "max-turns" and r["goal_verdict"] == "unachieved"
    assert r["limits"]["max_turns"] == 120
    assert json.loads(json.dumps(r)) == r               # 必须能直接落 result.json


def test_build_result_carries_bug_items_for_downstream():
    """序 6 契约要求 result.json 输出 "bugs" —— 只给计数时下游无法展示发现内容。

    加字段不改字段：`bugs`（计数）保持不变，`bug_items` 是新增的原文列表。
    空/纯空白项要丢掉（否则报告里会渲染出空条目）。
    """
    import json
    r = g.build_result(thread_id="t1", end_reason=g.EndReason.COMPLETED,
                       goal_verdict=g.GoalVerdict.ACHIEVED, bugs=2,
                       bug_items=["按钮无响应", "   ", "", "表单未校验", "x" * 900])
    assert r["bugs"] == 2
    assert r["bug_items"][:2] == ["按钮无响应", "表单未校验"]
    assert len(r["bug_items"]) == 3 and len(r["bug_items"][-1]) == 500    # 只截断，不改写
    assert json.loads(json.dumps(r)) == r


def test_build_result_bug_items_defaults_to_empty():
    r = g.build_result(thread_id="t1", end_reason=g.EndReason.ERROR,
                       goal_verdict=g.GoalVerdict.UNKNOWN)
    assert r["bug_items"] == []


def test_render_verdict_markdown_marks_authority():
    r = g.build_result(thread_id="t1", end_reason=g.EndReason.BLOCKED,
                       goal_verdict=g.GoalVerdict.UNKNOWN, reasons=["算不出"])
    md = g.render_verdict_markdown(r)
    assert "程序判定（权威" in md and "blocked" in md and "算不出" in md
    assert "算不出就不猜" in md or "unknown" in md
