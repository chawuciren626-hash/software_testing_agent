"""AI 探索接入契约的**纯逻辑**守护测试（审阅报告 §3.3 / §7 序 6）。

刻意**零第三方依赖**：`extensions/agentic/agentic_contract.py` 只 import 标准库，
所以它能在 CI 硬门禁（`requirements-dev.txt`，不装 langgraph/langchain）里直接跑。
探索入口本身（需要基座依赖）的接线测试另见 `tests/test_agentic_entry.py` /
`tests/test_agentic_wiring.py`。

守护的验收判据（§7 序 6）：**无 key / 超时 / 异常任一情况下降级，且降级原因出声**。
因此这里必须钉死三件事：
1. 降级原因**是枚举**（不能是自由文本，"其它"也要有归处）；
2. 降级负载**必带原因**（没有原因的降级 = 静默跳过，本项目的一票否决项）；
3. 只有显式 `status == "ok"` 才算"跑了"（缺字段一律不算——防假绿）。
"""
from __future__ import annotations

import json

import pytest

import agentic_contract as C


# --------------------------------------------------------------------------- #
# 枚举：状态与降级原因必须"可枚举"
# --------------------------------------------------------------------------- #
def test_status_is_exactly_two_states():
    """探索不是门禁：不存在 "failed"，只有 ok / degraded。"""
    assert {s.value for s in C.Status} == {"ok", "degraded"}


def test_degrade_reasons_cover_the_acceptance_criteria():
    """验收判据里的三类（无 key / 超时 / 异常）+ 配置问题，一个都不能少。"""
    assert {r.value for r in C.DegradeReason} == {
        "no_llm_key", "not_configured", "timeout", "error",
    }


@pytest.mark.parametrize("reason", list(C.DegradeReason))
def test_every_reason_has_human_label(reason):
    """每个原因都要有中文标签——否则报告里会露出裸枚举值，等于没说清原因。"""
    assert C.DEGRADE_LABEL.get(reason.value)


# --------------------------------------------------------------------------- #
# decide_degrade：动手之前的纯判定
# --------------------------------------------------------------------------- #
def test_decide_degrade_when_mission_missing(tmp_path):
    got = C.decide_degrade(provider="gemini", mission_path=tmp_path / "nope.yaml")
    assert got is not None
    reason, msg = got
    assert reason == C.DegradeReason.NOT_CONFIGURED
    # 报出原因还不够，得带上"找的是哪个文件"，否则使用者无从下手
    assert "nope.yaml" in msg


def test_decide_degrade_when_mission_not_given():
    got = C.decide_degrade(provider="gemini", mission_path=None)
    assert got is not None and got[0] == C.DegradeReason.NOT_CONFIGURED


@pytest.mark.parametrize("provider", [None, "", "  ", "unknown", "UNKNOWN", "none"])
def test_decide_degrade_no_llm_key(provider, tmp_path):
    m = tmp_path / "mission.yaml"
    m.write_text("missions: []\n", encoding="utf-8")
    got = C.decide_degrade(provider=provider, mission_path=m)
    assert got is not None
    assert got[0] == C.DegradeReason.NO_LLM_KEY
    # 必须告诉使用者"怎么修"，否则只报"没有 key"没有可操作性
    assert "ANTHROPIC_API_KEY" in got[1] or "GOOGLE_API_KEY" in got[1]


@pytest.mark.parametrize("provider", ["gemini", "claude", "Gemini"])
def test_decide_degrade_returns_none_when_ready(provider, tmp_path):
    m = tmp_path / "mission.yaml"
    m.write_text("missions: []\n", encoding="utf-8")
    assert C.decide_degrade(provider=provider, mission_path=m) is None


def test_decide_degrade_prefers_config_over_key(tmp_path):
    """两者都缺时先报**配置**：那是使用者自己能修、且与"有没有 key"无关的问题。"""
    got = C.decide_degrade(provider="unknown", mission_path=tmp_path / "nope.yaml")
    assert got is not None and got[0] == C.DegradeReason.NOT_CONFIGURED


# --------------------------------------------------------------------------- #
# 负载构造
# --------------------------------------------------------------------------- #
def test_build_degraded_always_carries_reason():
    p = C.build_degraded(C.DegradeReason.TIMEOUT, "超了")
    assert p["status"] == "degraded" and p["reason"] == "timeout"
    assert p["degraded_to"] == C.DEGRADED_TO_DETERMINISTIC
    assert p["message"] == "超了"
    assert C.is_ok(p) is False


def test_build_degraded_string_reason_is_accepted_but_bad_value_becomes_error():
    assert C.build_degraded("timeout", "x")["reason"] == "timeout"
    # 认不出来的原因归到 error，**绝不**悄悄放过
    assert C.build_degraded("wat", "x")["reason"] == "error"


def test_build_degraded_message_falls_back_to_label():
    p = C.build_degraded(C.DegradeReason.NO_LLM_KEY, "")
    assert p["message"] == C.DEGRADE_LABEL["no_llm_key"]


def test_build_ok_fields_and_bug_truncation():
    p = C.build_ok(provider="claude", thread_id="t1", end_reason="completed",
                   goal_verdict="achieved", reasons=["r1"], turns=5, tokens=100,
                   actions=7, bugs=["a" * 900, "b"], report_dir="r", action_tape="t",
                   log="l", elapsed_s=1.234)
    assert p["status"] == "ok" and C.is_ok(p) is True
    assert p["thread_id"] == "t1" and p["end_reason"] == "completed"
    assert p["bug_count"] == 2
    assert max(len(b) for b in p["bugs"]) == 500        # 原文只截断，不改写
    assert p["elapsed_s"] == 1.234
    assert json.loads(json.dumps(p)) == p               # 必须能直接落盘


def test_build_ok_ignores_blank_bugs():
    p = C.build_ok(provider="claude", bugs=["ok", "   ", ""])
    assert p["bugs"] == ["ok"] and p["bug_count"] == 1


# --------------------------------------------------------------------------- #
# end_reason 归一（认不出来就不猜）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,expect", [
    ("completed", "completed"), ("max-turns", "max-turns"),
    ("budget-exhausted", "budget-exhausted"), ("error", "error"), ("blocked", "blocked"),
    ("weird", None), ("", None), (None, None), (123, None),
])
def test_normalize_end_reason(raw, expect):
    assert C.normalize_end_reason(raw) == expect


# --------------------------------------------------------------------------- #
# 读写：原子、坏文件不炸
# --------------------------------------------------------------------------- #
def test_write_load_roundtrip_and_atomic(tmp_path):
    f = tmp_path / "artifacts" / "agentic.json"
    p = C.build_ok(provider="gemini", thread_id="t", bugs=["x"])
    C.write_result(f, p)
    assert C.load_result(f)["thread_id"] == "t"
    # 临时文件不能残留（否则下游 glob 会读到半截）
    assert list(f.parent.glob("*.tmp")) == []


def test_load_result_missing_or_broken_returns_none(tmp_path):
    assert C.load_result(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert C.load_result(bad) is None
    arr = tmp_path / "arr.json"
    arr.write_text("[1,2]", encoding="utf-8")
    assert C.load_result(arr) is None        # 非对象也当"没拿到"


@pytest.mark.parametrize("payload", [None, {}, {"status": "degraded"}, {"status": "OK"},
                                     {"status": None}])
def test_is_ok_is_strict(payload):
    """只有显式 "ok" 才算跑了 —— 缺字段/大小写不符一律不算（防假绿）。"""
    assert C.is_ok(payload) is False


# --------------------------------------------------------------------------- #
# 呈现：降级必须"出声"，且不许说成通过
# --------------------------------------------------------------------------- #
def test_render_summary_line_for_ok_reports_counts():
    line = C.render_summary_line(C.build_ok(provider="claude", actions=3, bugs=["a", "b"]))
    assert "已执行" in line and "2" in line and "3" in line


def test_render_summary_line_for_degraded_names_the_reason():
    line = C.render_summary_line(C.build_degraded(C.DegradeReason.NO_LLM_KEY, "x"))
    assert "降级" in line and "LLM 凭据" in line


def test_render_degrade_warning_forbids_reading_it_as_pass():
    """验收判据的落点：降级要出声，并且明确"未执行 ≠ 通过"。"""
    w = C.render_degrade_warning(C.build_degraded(C.DegradeReason.TIMEOUT, "超了"))
    assert "未执行" in w and "降级" in w
    assert "不得把本阶段当作已通过" in w


def test_render_markdown_ok_and_degraded_branches():
    ok = C.render_markdown(C.build_ok(provider="claude", thread_id="t", end_reason="completed",
                                      goal_verdict="achieved", actions=2, bugs=["发现1"]))
    assert "AI 探索测试" in ok and "completed" in ok and "发现1" in ok
    deg = C.render_markdown(C.build_degraded(C.DegradeReason.NO_LLM_KEY, "缺 key"))
    assert "降级原因" in deg and "no_llm_key" in deg
    assert "不等于" in deg and "确定性链路" in deg


# --------------------------------------------------------------------------- #
# 超时口径：默认必须**有限**
# --------------------------------------------------------------------------- #
def test_default_timeout_is_finite_by_default():
    """序 5 的结论同样适用于阶段级：没有上限 = 把成本控制权交给模型。"""
    assert C.default_timeout({}) == 1800.0
    assert C.default_timeout({}) > 0


def test_default_timeout_env_override_and_zero_means_unlimited():
    assert C.default_timeout({"STA_EXPLORE_TIMEOUT": "600"}) == 600.0
    assert C.default_timeout({"STA_EXPLORE_TIMEOUT": "0"}) == 0.0


@pytest.mark.parametrize("raw", ["abc", "", "  ", "-1", "none"])
def test_default_timeout_invalid_falls_back_to_finite(raw):
    """写错的数字**绝不能**变成"不限"。"""
    assert C.default_timeout({"STA_EXPLORE_TIMEOUT": raw}) == 1800.0


# --------------------------------------------------------------------------- #
# run_meta 摘要：没产出也要记一笔
# --------------------------------------------------------------------------- #
def test_summarize_for_meta_marks_missing_artifact():
    s = C.summarize_for_meta(None)
    assert s["executed"] is False and s["reason"] == "no_artifact"


def test_summarize_for_meta_distinguishes_executed_from_degraded():
    ok = C.summarize_for_meta(C.build_ok(provider="claude", bugs=["a"]))
    deg = C.summarize_for_meta(C.build_degraded(C.DegradeReason.TIMEOUT, "x"))
    assert ok["executed"] is True and ok["bug_count"] == 1
    assert deg["executed"] is False and deg["reason"] == "timeout"
