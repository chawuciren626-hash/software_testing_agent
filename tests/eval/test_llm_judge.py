"""L3 评测：LLM 质量打分接入前的契约测试（无 key 走 disabled，有 key 进入实现路径）。"""
import pytest

import llm_judge


def test_disabled_without_key():
    res = llm_judge.score_cases("# 用例\n")
    assert res["enabled"] is False


def test_enabled_raises_with_key():
    # 有 key 时应进入实现路径（目前 P1 未实现，标注 NotImplementedError）
    with pytest.raises(NotImplementedError):
        llm_judge.score_cases("# 用例\n", api_key="dummy")
