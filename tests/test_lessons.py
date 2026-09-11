"""P2 情景记忆闭环：失败根因聚类 -> lessons.md -> 注入下次 run 生成。

纯逻辑测试，不依赖 web_console / Flask / 真实 LLM。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "extensions" / "memory"))
import lessons as ls  # noqa: E402


def _snaps():
    """模拟 run_store.list_snapshots 的输出结构。"""
    return [
        {
            "pid": "demo", "ts": 1, "trigger": "run", "scene": None,
            "total": 3, "passed": 1, "failed": 2, "skipped": 0, "all_pass": False,
            "results": [
                {"name": "登录失败锁定", "result": "FAIL", "detail": "密码错误未锁定", "snippet": "..."},
                {"name": "登录失败锁定", "result": "FAIL", "detail": "密码错误未锁定", "snippet": "..."},
                {"name": "注册手机号", "result": "PASS", "detail": ""},
            ],
        },
        {
            "pid": "demo", "ts": 2, "trigger": "run", "scene": None,
            "total": 3, "passed": 2, "failed": 1, "skipped": 0, "all_pass": False,
            "results": [
                {"name": "登录失败锁定", "result": "FAIL", "detail": "锁定超时", "snippet": "..."},
                {"name": "注册手机号", "result": "PASS", "detail": ""},
                {"name": "token 过期", "result": "PASS", "detail": ""},
            ],
        },
    ]


def test_extract_failures():
    f = ls.extract_failures(_snaps())
    assert len(f) == 3
    names = [x["name"] for x in f]
    assert names.count("登录失败锁定") == 3
    assert all(x["detail"] for x in f)


def test_cluster_failures_ranks_by_frequency():
    clusters = ls.cluster_failures(ls.extract_failures(_snaps()))
    assert clusters[0]["name"] == "登录失败锁定"
    assert clusters[0]["count"] == 3
    for c in clusters:
        assert c["detail"]


def test_rebuild_writes_lessons_and_inject(tmp_path):
    md = ls.rebuild_from_snapshots(_snaps(), tmp_path)
    assert md and "登录失败锁定" in md
    assert (tmp_path / "lessons.md").is_file()
    inj = ls.to_inject_prompt(tmp_path)
    assert inj and "历史易错点" in inj and "登录失败锁定" in inj


def test_rebuild_no_failures_clears_stale(tmp_path):
    (tmp_path / "lessons.md").write_text("旧的易错点", encoding="utf-8")
    snaps = [{"pid": "d", "ts": 1, "results": [{"name": "x", "result": "PASS", "detail": ""}]}]
    assert ls.rebuild_from_snapshots(snaps, tmp_path) is None
    assert not (tmp_path / "lessons.md").exists()


def test_rule_generate_does_not_pollute_parsing():
    sys.path.insert(0, str(ROOT / "extensions" / "requirements_to_cases"))
    import generate_cases as gc  # noqa: E402
    text = "1. 用户可使用手机号注册账号"
    ctx = "# 历史易错点（重点覆盖）\n登录失败锁定 —— 历史失败 3 次"
    out = gc.generate_from_text(text, use_llm=False, extra_context=ctx)
    # 规则版：用例只来自原始需求（1 条 -> 3 用例），注入不进解析
    assert "| REQ-001-F |" in out
    assert "| REQ-002" not in out
    # 注入作为建议段追加
    assert "历史易错点重点覆盖建议" in out


def test_llm_generate_receives_injected_context(monkeypatch):
    sys.path.insert(0, str(ROOT / "extensions" / "requirements_to_cases"))
    import generate_cases as gc  # noqa: E402
    captured = {}

    def fake_llm(text, **kw):
        captured["text"] = text
        return "| id | 标题 | 模块 | 类型 | 优先级 | 前置 | 步骤 | 预期 | 可自动化 |\n|---|---|---|---|---|---|---|---|---|\n| REQ-001-F | 注册 | 注册 | 功能 | P1 | 无 | 1. 注册 | 成功 | 可 |"

    monkeypatch.setattr(gc, "llm_generate", fake_llm)
    ctx = "登录失败锁定 —— 历史失败 3 次"
    gc.generate_from_text("1. 注册", use_llm=True, extra_context=ctx)
    assert "历史易错点" in captured["text"]
    assert "登录失败锁定" in captured["text"]
