"""L3 评测：LLM-as-judge 的契约与解析测试（全部 mock，不依赖真实网络/key）。"""
import generate_cases as gc
import llm_judge


def _clear_llm_env(monkeypatch):
    for k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_API_BASE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "openai")


def test_disabled_without_key(monkeypatch):
    _clear_llm_env(monkeypatch)
    res = llm_judge.score_cases("# 用例\n")
    assert res["enabled"] is False
    assert res["scores"] == {}


def test_disabled_on_empty_cases(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "dummy")
    res = llm_judge.score_cases("")
    assert res["enabled"] is False


def test_score_enabled_with_mock(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "dummy")
    monkeypatch.setattr(
        gc, "llm_generate",
        lambda *a, **k: '{"coverage": 90, "executable": 80, "dedup": 70, "edge_abn": 60, "comment": "ok"}')
    res = llm_judge.score_cases("# 用例\n| a |")
    assert res["enabled"] is True
    assert res["scores"] == {"coverage": 90, "executable": 80, "dedup": 70, "edge_abn": 60}
    assert res["total"] == 75  # (90+80+70+60)/4
    assert res["comment"] == "ok"


def test_judge_parses_code_fenced_json(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "dummy")
    fenced = ('以下是评分：\n```json\n'
              '{"coverage": 100, "executable": 100, "dedup": 100, "edge_abn": 100}\n```\n')
    monkeypatch.setattr(gc, "llm_generate", lambda *a, **k: fenced)
    res = llm_judge.score_cases("x")
    assert res["enabled"] is True and res["total"] == 100


def test_judge_clamps_out_of_range(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "dummy")
    monkeypatch.setattr(
        gc, "llm_generate",
        lambda *a, **k: '{"coverage": 999, "executable": -5, "dedup": "80", "edge_abn": null}')
    res = llm_judge.score_cases("x")
    assert res["scores"]["coverage"] == 100
    assert res["scores"]["executable"] == 0
    assert res["scores"]["dedup"] == 80
    assert res["scores"]["edge_abn"] == 0


def test_judge_llm_error_returns_disabled(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "dummy")

    def boom(*a, **k):
        raise gc.LLMError("模拟失败")

    monkeypatch.setattr(gc, "llm_generate", boom)
    res = llm_judge.score_cases("x")
    assert res["enabled"] is False and "失败" in res["reason"]


def test_judge_unparseable_returns_disabled(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "dummy")
    monkeypatch.setattr(gc, "llm_generate", lambda *a, **k: "这不是 JSON")
    res = llm_judge.score_cases("x")
    assert res["enabled"] is False and "JSON" in res["reason"]
