"""L3 评测：LLM-as-judge 用例质量打分（P1 接入 key 后启用）。

评分维度（合同）：
- coverage   覆盖度：需求条目是否都被覆盖（功能/边界/异常三类）
- executable 可执行性：步骤是否具体、预期是否可判定
- dedup      去重：是否存在高度重复的用例
- edge_abn   边界/异常覆盖：是否每需求都含边界与异常用例

本文件是 P1 的接口占位，当前未实现（无 key 时直接返回 disabled）。
"""

from typing import Dict, Any, Optional


def score_cases(cases_md: str, api_key: Optional[str] = None) -> Dict[str, Any]:
    """对生成的用例打质量分。无 key 时返回未启用状态。"""
    if not api_key:
        return {"enabled": False, "reason": "no LLM key", "scores": {}}
    # TODO(P1): 接入所选 LLM（Claude/Gemini，可切换），按维度评分并返回 0-100。
    raise NotImplementedError("P1: implement LLM judge scoring")
