"""L3 评测：模式对比编排的纯逻辑测试（mock 生成与打分，不依赖真实 LLM）。"""
import generate_cases as gc
import llm_judge
import quality_bench as qb


def _scores(v):
    return {"enabled": True, "scores": {d: v for d in llm_judge.DIMENSIONS},
            "total": v, "comment": ""}


def test_compare_modes_ranks_best(monkeypatch):
    def fake_gen(text, use_llm=False, agentic=False, **k):
        if agentic:
            return "| id | 标题 |\n|---|---|\n| REQ-001-F | a |\n| REQ-001-B | b |"
        return "| id | 标题 |\n|---|---|\n| REQ-001-F | a |"

    monkeypatch.setattr(gc, "generate_from_text", fake_gen)
    # agentic 产物（含边界行）给高分，其余给低分
    monkeypatch.setattr(llm_judge, "score_cases",
                        lambda md, **k: _scores(90 if "REQ-001-B" in md else 50))

    c = qb.compare_modes("1. x")
    assert len(c["results"]) == 3
    assert c["best"] == "agentic"
    assert {r["key"] for r in c["results"]} == {"rule", "llm", "agentic"}


def test_compare_modes_respects_mode_filter(monkeypatch):
    monkeypatch.setattr(gc, "generate_from_text",
                        lambda *a, **k: "| id | 标题 |\n|---|---|\n| REQ-001-F | a |")
    monkeypatch.setattr(llm_judge, "score_cases", lambda md, **k: _scores(60))
    c = qb.compare_modes("1. x", modes=["rule", "agentic"])
    assert {r["key"] for r in c["results"]} == {"rule", "agentic"}


def test_compare_modes_handles_disabled_judge(monkeypatch):
    monkeypatch.setattr(gc, "generate_from_text",
                        lambda *a, **k: "| id | 标题 |\n|---|---|\n| REQ-001-F | a |")
    monkeypatch.setattr(llm_judge, "score_cases",
                        lambda *a, **k: {"enabled": False, "reason": "no LLM key", "scores": {}})
    c = qb.compare_modes("1. x")
    assert c["best"] is None
    assert "未启用" in qb.render_table(c)


def test_compare_modes_repeat_averaging(monkeypatch):
    monkeypatch.setattr(gc, "generate_from_text",
                        lambda *a, **k: "| id | 标题 |\n|---|---|\n| REQ-001-F | a |")
    scores = iter([80, 100])
    monkeypatch.setattr(llm_judge, "score_cases", lambda md, **k: _scores(next(scores)))
    c = qb.compare_modes("1. x", modes=["rule"], repeat=2)
    assert c["repeat"] == 2
    r = c["results"][0]
    assert len(r["trials"]) == 2
    assert r["total_mean"] == 90.0
    assert r["total_min"] == 80 and r["total_max"] == 100


def test_render_table_repeat_shows_mean_and_range(monkeypatch):
    monkeypatch.setattr(gc, "generate_from_text",
                        lambda *a, **k: "| id | 标题 |\n|---|---|\n| REQ-001-F | a |")
    scores = iter([80, 100])
    monkeypatch.setattr(llm_judge, "score_cases", lambda md, **k: _scores(next(scores)))
    txt = qb.render_table(qb.compare_modes("1. x", modes=["rule"], repeat=2))
    assert "均分" in txt and "90" in txt and "80~100" in txt


def test_render_table_contains_labels(monkeypatch):
    monkeypatch.setattr(gc, "generate_from_text",
                        lambda *a, **k: "| id | 标题 |\n|---|---|\n| REQ-001-F | a |")
    monkeypatch.setattr(llm_judge, "score_cases", lambda md, **k: _scores(70))
    txt = qb.render_table(qb.compare_modes("1. x"))
    assert "规则版" in txt and "单步 LLM" in txt and "多步自审编排" in txt
    assert "最优" in txt
