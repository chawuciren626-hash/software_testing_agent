"""L3 评测：三种生成模式的用例质量对比（规则版 / 单步 LLM / 多步自审编排）。

用途：量化验证「多步自审编排（agentic）是否真的比单步 LLM / 规则版更好」，
为评测基线提供可复现的数据。

用法（需真实 LLM 配置；无 key 时 judge 自动跳过，只输出用例条数）::

    python tests/eval/quality_bench.py --input extensions/requirements_to_cases/sample_requirements.md
    python tests/eval/quality_bench.py --input req.md --llm-model qwen-max --modes rule,llm,agentic
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
sys.path.insert(0, str(_HERE))                                              # tests/eval -> llm_judge
sys.path.insert(0, str(_ROOT))                                              # 仓库根 -> project_manager
sys.path.insert(0, str(_ROOT / "extensions" / "requirements_to_cases"))     # -> generate_cases
import generate_cases as gc  # noqa: E402
import project_manager as pm  # noqa: E402
import llm_judge  # noqa: E402

# (key, 展示名, use_llm, agentic)
MODES: List[Tuple[str, str, bool, bool]] = [
    ("rule", "规则版", False, False),
    ("llm", "单步 LLM", True, False),
    ("agentic", "多步自审编排", True, True),
]


def count_cases(md: str) -> int:
    """按项目规范解析用例条数（与报告渲染同源）。"""
    try:
        return len(pm._parse_cases(md)[1])
    except Exception:
        return 0


def compare_modes(requirements_text: str,
                  modes: Optional[List[str]] = None) -> Dict[str, Any]:
    """对同一份需求分别用各模式生成用例并打分，返回对比结果。

    judge 不可用（无 key / 调用失败）时，judge 字段为 ``{"enabled": False, ...}``，
    仍会返回各模式生成的用例条数，便于无 key 环境做冒烟。
    """
    results: List[Dict[str, Any]] = []
    for key, label, use_llm, agentic in MODES:
        if modes and key not in modes:
            continue
        md = gc.generate_from_text(requirements_text, use_llm=use_llm, agentic=agentic)
        judged = llm_judge.score_cases(md)
        results.append({
            "key": key,
            "label": label,
            "cases": count_cases(md),
            "judge": judged,
            "total": judged.get("total"),
        })
    scored = [r for r in results if isinstance(r.get("total"), int)]
    best = max(scored, key=lambda r: r["total"])["key"] if scored else None
    return {"results": results, "best": best}


def render_table(comparison: Dict[str, Any]) -> str:
    """把对比结果渲染成人类可读的表格文本。"""
    lines = [
        f"{'模式':<14}{'用例数':>6}{'总分':>8}",
        "-" * 30,
    ]
    for r in comparison["results"]:
        total = r["total"]
        lines.append(f"{r['label']:<14}{r['cases']:>6}{(total if total is not None else '—'):>8}")
    if comparison.get("best"):
        lines.append("-" * 30)
        lines.append(f"最优：{comparison['best']}")
    # 分维度明细
    detail = [r for r in comparison["results"] if r["judge"].get("enabled")]
    if detail:
        lines.append("")
        lines.append("分维度：" + " | ".join(llm_judge.DIMENSIONS))
        for r in detail:
            s = r["judge"]["scores"]
            lines.append(f"  {r['label']:<12}" + " | ".join(str(s.get(d, 0)) for d in llm_judge.DIMENSIONS))
    else:
        lines.append("")
        lines.append("（judge 未启用：未配置 LLM 或调用失败，仅比较用例条数）")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="三种生成模式的用例质量对比评测")
    ap.add_argument("--input", "-i", required=True, help="需求文件路径")
    ap.add_argument("--modes", default="rule,llm,agentic",
                    help="参与对比的模式，逗号分隔（默认全部）")
    ap.add_argument("--llm-model", default=None, help="覆盖 LLM_MODEL（便于换快模型跑 agentic）")
    ap.add_argument("--llm-timeout", default=None, help="覆盖 LLM_TIMEOUT（秒）")
    args = ap.parse_args()

    if args.llm_model:
        os.environ["LLM_MODEL"] = args.llm_model
    if args.llm_timeout:
        os.environ["LLM_TIMEOUT"] = args.llm_timeout

    text = Path(args.input).read_text(encoding="utf-8")
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    print(f"== 用例质量对比评测（模型 {os.environ.get('LLM_MODEL', '默认')}）==")
    comparison = compare_modes(text, modes=modes)
    print(render_table(comparison))


if __name__ == "__main__":
    main()
