"""生成溯源（provenance）单测。

最重要的一条：**「没配 key 走规则版」和「LLM 失败降级成规则版」必须在产物上可区分。**
这两者生成的用例表格完全一样，区别只在于"这次本该更好" ——
如果标记分不开，降级就会静默发生，读用例的人会拿着模板占位当成 LLM 质量的产物。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "extensions" / "requirements_to_cases"))

import generate_cases as gc  # noqa: E402
import project_manager as pm  # noqa: E402
import provenance as pv  # noqa: E402

_REQ = "1. 用户可登录\n2. 用户可查看列表\n"


def _table():
    return ("| id | 标题 | 模块 | 类型 | 优先级 | 前置 | 步骤 | 预期 | 可自动化 |\n"
            "|---|---|---|---|---|---|---|---|---|\n"
            "| REQ-001-F | 登录（功能） | 待定 | 功能 | P1 | 已部署 | 1. 登录 | 成功 | 可 |\n")


# ---------------------------------------------------------------------------
# 模式与置信度
# ---------------------------------------------------------------------------
def test_mode_of():
    assert pv.mode_of(False, False) == pv.MODE_RULE
    assert pv.mode_of(True, False) == pv.MODE_LLM
    assert pv.mode_of(True, True) == pv.MODE_AGENTIC


def test_confidence_per_mode():
    assert pv.build(mode=pv.MODE_RULE)["confidence"] == "低"
    assert pv.build(mode=pv.MODE_LLM)["confidence"] == "中"
    assert pv.build(mode=pv.MODE_AGENTIC)["confidence"] == "中高"


def test_degraded_overrides_confidence_and_mode_stays_honest():
    """降级只改可信度，**不改模式** —— 产物确实是规则版写的，不能标成 llm。"""
    m = pv.build(mode=pv.MODE_RULE, degraded=True, degrade_reason="timeout")
    assert m["confidence"] == pv.DEGRADED_CONFIDENCE
    assert m["mode"] == pv.MODE_RULE
    assert m["degraded"] is True


def test_quality_is_none_when_not_measured():
    """没有结构质量分就记 None，**不拿别的数凑** —— 凑出来的分会让人误以为测过了。"""
    assert pv.build(mode=pv.MODE_RULE)["quality"] is None


# ---------------------------------------------------------------------------
# 盖章 / 回读
# ---------------------------------------------------------------------------
def test_stamp_roundtrip():
    meta = pv.build(mode=pv.MODE_LLM, provider="openai", model="qwen-max",
                    source="requirements.md", case_count=9, req_count=3,
                    injected={"lessons": 2, "knowledge": 1})
    md = pv.stamp(_table(), meta)
    got = pv.parse(md)
    assert got is not None
    assert got["mode"] == pv.MODE_LLM
    assert got["model"] == "qwen-max"
    assert got["case_count"] == 9
    assert got["injected"] == {"lessons": 2, "knowledge": 1}
    assert got.get("partial") is None


def test_stamp_is_idempotent():
    """重复盖章不能越叠越高（否则每次重跑头部就多一坨）。"""
    meta = pv.build(mode=pv.MODE_RULE, source="r.md", case_count=3)
    once = pv.stamp(_table(), meta)
    twice = pv.stamp(once, meta)
    assert twice.count("- 生成方式：") == 1
    assert twice.count("# 测试用例") == 1
    assert twice.count(pv.MARK) == 1
    assert "| REQ-001-F |" in twice


def test_stamped_md_still_parseable_by_project_manager():
    """回归防线：``_parse_cases`` 只看前 8 行，溯源头部不能把它挤出去。"""
    meta = pv.build(mode=pv.MODE_RULE, source="sample_requirements.md",
                    case_count=1, injected={"lessons": 1, "knowledge": 1},
                    degraded=True, degrade_reason="boom")
    md = pv.stamp(_table(), meta)
    m, rows = pm._parse_cases(md)
    assert m["source"] == "sample_requirements.md"
    assert m["count"] == 1
    assert len(rows) == 1
    assert rows[0]["id"] == "REQ-001-F"


def test_parse_returns_none_for_unstamped_legacy():
    """旧产物（没有标记）必须返回 None，不能编一个"未知规则版"出来。"""
    assert pv.parse(_table()) is None
    assert pv.parse("") is None


def test_parse_falls_back_to_text_and_marks_partial():
    """标记被渲染器/复制粘贴吃掉时，从文本行回读 —— 但必须自曝不完整。"""
    meta = pv.build(mode=pv.MODE_LLM, provider="openai", model="qwen-max",
                    case_count=9, degraded=True, degrade_reason="timeout")
    md = pv.stamp(_table(), meta)
    stripped = "\n".join(l for l in md.splitlines() if pv.MARK not in l)
    got = pv.parse(stripped)
    assert got is not None                  # 没彻底丢
    assert got["partial"] is True           # 但说清自己残缺
    assert got["degraded"] is True
    assert got["case_count"] == 9
    assert got.get("model", "") == ""       # 模型名确实丢了，不猜


def test_reason_with_comment_close_does_not_truncate_mark():
    """自由文本里有 '-->' 会提前闭合 HTML 注释；必须转义而不是让标记废掉。"""
    meta = pv.build(mode=pv.MODE_RULE, degraded=True, degrade_reason="bad --> x")
    md = pv.stamp(_table(), meta)
    got = pv.parse(md)
    assert got is not None
    assert got["degraded"] is True
    assert got["degrade_reason"] == "bad -> x"


# ---------------------------------------------------------------------------
# 核心：降级 vs 正常规则版
# ---------------------------------------------------------------------------
def test_degraded_and_plain_rule_are_distinguishable_in_artifact(monkeypatch):
    """本模块存在的理由：两种规则版产物在文件里必须看得出差别。

    必须显式让 LLM 失败 —— 否则本机若配了真实 key，LLM 会真的成功，
    测试就变成"环境依赖"，在 CI / 别人机器上行为不一致。
    """
    plain, _ = gc.generate_with_meta(_REQ, use_llm=False, source="r.md")
    monkeypatch.setattr(gc, "llm_generate",
                        lambda *a, **k: (_ for _ in ()).throw(
                            gc.LLMError("403 余额不足")))
    broken, meta = gc.generate_with_meta(_REQ, use_llm=True, source="r.md")
    assert meta["degraded"] is True

    assert "| REQ-001-F |" in plain and "| REQ-001-F |" in broken   # 表格长得一样
    assert pv.is_degraded(plain) is False
    assert pv.is_degraded(broken) is True
    assert "⚠️ 降级产出" in broken
    assert "⚠️ 降级产出" not in plain


def test_degrade_reason_comes_from_real_exception():
    """降级原因要写**真实异常**，不能笼统写"LLM 失败"。"""
    def boom(*a, **k):
        raise gc.LLMError("403 forbidden: 余额不足")
    orig = gc.llm_generate
    gc.llm_generate = boom
    try:
        md, meta = gc.generate_with_meta(_REQ, use_llm=True, source="r.md")
    finally:
        gc.llm_generate = orig
    assert meta["degraded"] is True
    assert "余额不足" in meta["degrade_reason"]
    assert "余额不足" in md


def test_non_llmerror_also_degrades_with_type_name():
    """非 LLMError 的意外（如代码 bug）同样要降级，且记下异常类型便于排查。"""
    def boom(*a, **k):
        raise RuntimeError("boom")
    orig = gc.llm_generate
    gc.llm_generate = boom
    try:
        _, meta = gc.generate_with_meta(_REQ, use_llm=True, source="r.md")
    finally:
        gc.llm_generate = orig
    assert meta["degraded"] is True
    assert "RuntimeError" in meta["degrade_reason"]


def test_llm_success_is_not_marked_degraded(monkeypatch):
    monkeypatch.setattr(gc, "llm_generate", lambda *a, **k: _table())
    _, meta = gc.generate_with_meta(_REQ, use_llm=True, source="r.md")
    assert meta["degraded"] is False
    assert meta["mode"] == pv.MODE_LLM
    assert meta["case_count"] == 1


def test_degraded_meta_keeps_model_when_llm_was_attempted():
    """降级时要保留"本来想用哪个模型" —— 否则查问题时不知道该查哪个 key/额度。"""
    def boom(*a, **k):
        raise gc.LLMError("timeout")
    orig = gc.llm_generate
    gc.llm_generate = boom
    try:
        _, meta = gc.generate_with_meta(_REQ, use_llm=True, source="r.md", model="qwen-max")
    finally:
        gc.llm_generate = orig
    assert meta["model"] == "qwen-max"
    assert meta["degraded"] is True


# ---------------------------------------------------------------------------
# 与生成入口的一致性
# ---------------------------------------------------------------------------
def test_generate_from_text_and_with_meta_produce_same_markdown():
    """两个入口必须同源：各写一份降级判定，迟早一处记得标、一处忘了。"""
    a = gc.generate_from_text(_REQ, use_llm=False, source="r.md")
    b, _ = gc.generate_with_meta(_REQ, use_llm=False, source="r.md")
    # 时间戳在秒级内相同；只比对结构，避免踩到跨分钟边界
    assert pv.parse(a)["mode"] == pv.parse(b)["mode"]
    assert a.count("| REQ-00") == b.count("| REQ-00")


def test_count_rows_ignores_prose_and_separator():
    assert gc._count_rows(_table()) == 1
    assert gc._count_rows("说明文字\n\n" + _table() + "\n尾部说明") == 1
    assert gc._count_rows("") == 0


# ---------------------------------------------------------------------------
# 质量分回写
# ---------------------------------------------------------------------------
def test_attach_quality_writes_score_into_stamp():
    meta = pv.build(mode=pv.MODE_RULE, case_count=1)
    md = pv.attach_quality(pv.stamp(_table(), meta), 78)
    got = pv.parse(md)
    assert got["quality"] == 78
    assert "- 结构质量分：78/100" in md


def test_attach_quality_leaves_unstamped_untouched():
    """没有标记的旧产物不给它硬盖章 —— 来源未知就说未知。"""
    raw = _table()
    assert pv.attach_quality(raw, 78) == raw


def test_attach_quality_is_idempotent():
    md = pv.attach_quality(pv.stamp(_table(), pv.build(mode=pv.MODE_RULE)), 70)
    md2 = pv.attach_quality(md, 80)
    assert md2.count("- 结构质量分：") == 1
    assert pv.parse(md2)["quality"] == 80


# ---------------------------------------------------------------------------
# 展示层
# ---------------------------------------------------------------------------
def test_badge_degraded_is_red_and_has_reason():
    b = pv.badge(pv.build(mode=pv.MODE_RULE, degraded=True, degrade_reason="timeout"))
    assert b["icon"] == "⚠️"
    assert b["color"] == "#c62828"
    assert "降级" in b["label"]
    assert pv.badge(None)["label"] == "未知来源"


def test_render_text_includes_reason():
    txt = pv.render_text(pv.build(mode=pv.MODE_RULE, degraded=True,
                                  degrade_reason="timeout", quality=55))
    assert "timeout" in txt
    assert "55/100" in txt


def test_describe_shows_model_and_degrade():
    assert "qwen-max" in pv.describe(pv.build(mode=pv.MODE_LLM, provider="openai",
                                              model="qwen-max"))
    assert "降级" in pv.describe(pv.build(mode=pv.MODE_RULE, degraded=True))


def test_injected_counts_rendered():
    md = pv.stamp(_table(), pv.build(mode=pv.MODE_RULE,
                                     injected={"lessons": 3, "knowledge": 2}))
    assert "情景记忆 3 条" in md
    assert "项目知识 2 段" in md


def test_empty_injected_not_rendered():
    md = pv.stamp(_table(), pv.build(mode=pv.MODE_RULE, injected={"lessons": 0}))
    assert "上下文注入" not in md


# ---------------------------------------------------------------------------
# 报告：降级必须在报告里出声
# ---------------------------------------------------------------------------
def _report(tmp_path, *, degraded, meta_extra=None):
    pdir = tmp_path / "p"
    (pdir / "artifacts").mkdir(parents=True)
    (pdir / "project.yaml").write_text("project_id: demo\n", encoding="utf-8")
    meta = pv.build(mode=pv.MODE_RULE, degraded=degraded,
                    **(meta_extra or {}))
    pm._write_run_meta(pdir, mode="规则版", cases=6, degraded=degraded,
                       provenance=meta)
    out = pm._step_report("demo", pdir, None,
                          {"results": [], "total": 0, "passed": 0,
                           "failed": 0, "skipped": 0})
    return out.read_text(encoding="utf-8")


def test_report_marks_degraded_run(tmp_path):
    """报告摘要里「生成模式」要标出降级 —— 用例看着是好的，其实是模板占位。"""
    html = _report(tmp_path, degraded=True,
                   meta_extra={"degrade_reason": "LLM 超时"})
    assert "降级产出" in html
    assert "LLM 超时" in html        # 原因要能直接看到，不用回头翻日志


def test_report_does_not_cry_wolf_when_not_degraded(tmp_path):
    """没降级就不能出现降级字样 —— 天天喊狼来了，真降级时也没人看。"""
    html = _report(tmp_path, degraded=False)
    assert "降级产出" not in html
    assert "可信度 低" in html          # 规则版可信度照常展示（低，但不是降级）
