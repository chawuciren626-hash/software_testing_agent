"""B 世界护栏的**接线**测试（审阅报告 §3.2 / §7 序 5）。

与 `tests/test_guardrails.py`（纯逻辑，进 CI 硬门禁）互补：这里验证**真的接上了**——
supervisor 到硬上限时会终止且**不调用路由 LLM**、软重置不再顺手把硬计数也归零、
agent 节点会把 token 记进 state。

⚠️ 需要基座依赖（langgraph / langchain / playwright）。CI 的**硬门禁**作业不装这些
（见 `requirements-dev.txt` 的说明），所以本文件用 `importorskip` 保护：装了就跑、
没装就 SKIP —— 而不是让门禁因为"装不动的第三方库"变红。
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest

pytest.importorskip("langchain_core", reason="需要基座依赖（CI 硬门禁不装）")
gb = pytest.importorskip("agentic_explorer.orchestration.graph_base",
                         reason="需要基座依赖（langgraph / playwright）")

from agentic_explorer.orchestration.guardrails import EndReason, Limits  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "agentic_explorer"


class _FakeStructured:
    def __init__(self, nxt, counter):
        self._nxt, self._counter = nxt, counter

    async def ainvoke(self, messages):
        self._counter[0] += 1
        return {"next": self._nxt}


class _FakeLLM:
    def __init__(self, nxt="agent_a"):
        self._nxt = nxt
        self.calls = [0]

    def with_structured_output(self, schema=None, method=None):
        return _FakeStructured(self._nxt, self.calls)


def _supervisor(nxt="agent_a", **limit_kw):
    llm = _FakeLLM(nxt)
    node = gb.make_supervisor_node(llm, ("agent_a", "agent_b"), "http://app",
                                   3, None, app_url_hash="", limits=Limits(**limit_kw))
    return node, llm


def _state(**over):
    st = {"step_count": 0, "turns": 0, "tokens_used": 0, "bugs_found": [], "explored_paths": []}
    st.update(over)
    return st


# --------------------------------------------------------------------------- #
# 硬护栏：到顶即终止，且**不调用路由 LLM**
# --------------------------------------------------------------------------- #
def test_hard_max_turns_terminates_without_calling_llm():
    node, llm = _supervisor(max_turns=3)
    out = asyncio.run(node(_state(turns=2)))
    assert out["next_agent"] == "FINISH"
    assert out["end_reason"] == EndReason.MAX_TURNS.value
    assert llm.calls[0] == 0, "硬停时**不应**再花一次 LLM 调用去问'接下来干嘛'"


def test_budget_exhaustion_terminates_without_calling_llm():
    node, llm = _supervisor(max_turns=99, token_budget=100)
    out = asyncio.run(node(_state(turns=1, tokens_used=100)))
    assert out["next_agent"] == "FINISH"
    assert out["end_reason"] == EndReason.BUDGET_EXHAUSTED.value
    assert llm.calls[0] == 0


def test_normal_turn_routes_and_bumps_both_counters():
    node, llm = _supervisor(max_turns=10)
    out = asyncio.run(node(_state(step_count=1, turns=1)))
    assert out["next_agent"] == "agent_a" and llm.calls[0] == 1
    assert out["turns"] == 2 and out["step_count"] == 2
    assert "end_reason" not in out          # 正常路由不写结束原因


# --------------------------------------------------------------------------- #
# 软硬分离：软重置照旧，但**绝不**触碰 turns
# --------------------------------------------------------------------------- #
def test_soft_reset_resets_step_count_but_never_turns():
    node, llm = _supervisor(max_turns=99)
    out = asyncio.run(node(_state(step_count=3, turns=7)))    # 3+1 > max_steps(3)
    assert out["step_count"] == 1, "软重置应把 step_count 归 1"
    assert out["turns"] == 8, "硬计数只增不减 —— 软重置绝不能把它也归零（否则等于无限循环）"
    assert llm.calls[0] == 1, "软重置仍要继续探索，所以要照常路由"


# --------------------------------------------------------------------------- #
# agent 节点：把本轮 token 记进 state
# --------------------------------------------------------------------------- #
def test_agent_node_reports_new_tokens():
    from langchain_core.messages import AIMessage

    class _FakeAgent:
        async def astream(self, state, config=None):
            yield {"messages": state["messages"] + [
                AIMessage(content="done", id="new-1",
                          usage_metadata={"input_tokens": 30, "output_tokens": 5,
                                          "total_tokens": 35})
            ]}

    node = gb.make_agent_node(_FakeAgent(), name="agent_a")
    hist = [AIMessage(content="old", id="old-1",
                      usage_metadata={"input_tokens": 999, "output_tokens": 1,
                                      "total_tokens": 1000})]
    out = asyncio.run(node({"messages": hist}))
    assert out["tokens_used"] == 35, "只应计本轮新增，历史（1000）不能重复计入"


# --------------------------------------------------------------------------- #
# 静态接线断言：确实把 limits 传下去了（这两个模块因 custom_tools 依赖无法导入）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fname", ["standard_graph.py", "advanced_graph.py"])
def test_graph_builders_thread_limits_into_supervisor(fname):
    src = (SRC / "orchestration" / fname).read_text(encoding="utf-8")
    assert "limits=None" in src, f"{fname} 的 build_* 应接受 limits 参数"
    assert "make_supervisor_node(" in src and "limits=limits" in src, \
        f"{fname} 必须把 limits 透传给 make_supervisor_node"


def test_main_wires_programmatic_verdict():
    src = (SRC / "main.py").read_text(encoding="utf-8")
    for needle in ("guardrails.Limits.from_env", "MissionGoal.from_spec",
                   "guardrails.judge_mission", "guardrails.classify_end_reason",
                   "guardrails.build_result", "render_verdict_markdown",
                   "result.json"):
        assert needle in src, f"main.py 缺少接线点：{needle}"
    # 报告提示词里不能再让模型自称最终结论
    assert "Final Status" not in src, "报告的 PASS/FAIL 不能仍由模型拍板"
