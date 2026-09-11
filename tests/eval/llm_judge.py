"""L3 评测：LLM-as-judge 用例质量打分（多 provider，失败安全）。

在规则版（L1）与流水线冒烟（L2）之上，用 LLM 对生成用例的**质量**做主观打分，
用于横向比较「规则版 / 单步 LLM / 多步自审编排」的产出优劣。

评分维度（整数 0-100，越高越好）：
- coverage   覆盖度：需求条目是否都被覆盖
- executable 可执行性：步骤是否具体明确、预期是否可判定
- dedup      去重：用例之间是否高度重复（越不重复分越高）
- edge_abn   边界/异常覆盖：是否对每条需求都给出边界与异常用例

设计：
- 复用 `generate_cases.llm_generate`（provider 由 LLM_PROVIDER 决定），零额外依赖。
- 未配置 LLM（且未显式传 key）时返回 ``enabled=False``——保证无 key 的 CI 也能跑。
- LLM 调用或 JSON 解析失败一律返回 ``enabled=False + reason``，绝不抛异常打断评测。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "extensions" / "requirements_to_cases"))
import generate_cases as gc  # noqa: E402

DIMENSIONS = ("coverage", "executable", "dedup", "edge_abn")

JUDGE_PROMPT = """你是资深测试专家，请对下面这批测试用例的质量打分。
严格只输出一个 JSON 对象，不要任何额外文字、解释或代码块围栏，格式如下：
{{"coverage": 0, "executable": 0, "dedup": 0, "edge_abn": 0, "comment": "一句话点评"}}

评分维度（整数 0-100，越高越好）：
- coverage   覆盖度：需求条目是否都被覆盖
- executable 可执行性：步骤是否具体明确、预期是否可判定
- dedup      去重：用例之间是否高度重复（越不重复分越高）
- edge_abn   边界 / 异常覆盖：是否对每条需求都给出了边界与异常用例

用例（Markdown 表格）：
{cases_md}"""


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从 LLM 输出里抽取第一个 JSON 对象（容忍代码块围栏与前后说明文字）。"""
    if not text:
        return None
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.S)
    if m:
        t = m.group(1)
    else:
        i, j = t.find("{"), t.rfind("}")
        if i == -1 or j == -1 or j <= i:
            return None
        t = t[i:j + 1]
    try:
        obj = json.loads(t)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _clamp(v: Any) -> int:
    """把任意取值收敛为 0-100 的整数。"""
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, n))


def score_cases(cases_md: str, api_key: Optional[str] = None,
                provider: Optional[str] = None, base_url: Optional[str] = None,
                model: Optional[str] = None) -> Dict[str, Any]:
    """对生成的用例打质量分。

    返回两种形态：::

        {"enabled": True,  "scores": {...}, "total": 87, "comment": "...", "raw": "..."}
        {"enabled": False, "reason": "...", "scores": {}}
    """
    if not cases_md or not cases_md.strip():
        return {"enabled": False, "reason": "empty cases", "scores": {}}
    if not api_key and not gc.llm_configured():
        return {"enabled": False, "reason": "no LLM key", "scores": {}}
    try:
        raw = gc.llm_generate(JUDGE_PROMPT.format(cases_md=cases_md),
                              provider=provider, api_key=api_key,
                              base_url=base_url, model=model)
    except gc.LLMError as e:
        return {"enabled": False, "reason": f"judge LLM 调用失败：{e}", "scores": {}}
    obj = _extract_json(raw)
    if not obj:
        return {"enabled": False, "reason": "judge 输出无法解析为 JSON",
                "scores": {}, "raw": raw[:500]}
    scores = {d: _clamp(obj.get(d, 0)) for d in DIMENSIONS}
    total = int(round(sum(scores.values()) / len(DIMENSIONS)))
    return {"enabled": True, "scores": scores, "total": total,
            "comment": str(obj.get("comment", ""))[:300], "raw": raw[:1000]}
