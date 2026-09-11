"""L3 评测：多种生成模式的用例质量对比（规则版 / 单步 LLM / 多步自审编排）。

用途：量化验证「多步自审编排（agentic）是否真的比单步 LLM / 规则版更好」，
为评测基线提供**可复现、可重复采样**的数据。

⚠️ judge 单次判分方差较大（同一产物可打出 100 与 90），因此**比较质量请用
``--repeat N`` 多次独立生成+打分取均值**，不要只看单次结果。

用法（需真实 LLM 配置；无 key 时 judge 自动跳过，只输出用例条数）::

    # 单次快速对比
    python tests/eval/quality_bench.py --input req.md --llm-model qwen-max

    # 多次采样取均值（推荐；注意 agentic 每次 4 次调用，N 大时较慢）
    python tests/eval/quality_bench.py --input req.md --llm-model qwen-max --repeat 3
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


def _mean(vals: List[float], nd: int = 1) -> Optional[float]:
    return round(sum(vals) / len(vals), nd) if vals else None


def compare_modes(requirements_text: str,
                  modes: Optional[List[str]] = None,
                  repeat: int = 1) -> Dict[str, Any]:
    """对同一份需求分别用各模式生成用例并打分，返回对比结果。

    repeat > 1 时每个模式做多次**独立**生成 + 打分（衡量端到端方差），
    汇总 ``total_mean / total_min / total_max / dim_means``。

    judge 不可用（无 key / 调用失败）时，judge 字段为 ``{"enabled": False, ...}``，
    仍会返回各模式生成的用例条数，便于无 key 环境做冒烟。
    """
    repeat = max(1, int(repeat))
    results: List[Dict[str, Any]] = []
    for key, label, use_llm, agentic in MODES:
        if modes and key not in modes:
            continue
        trials: List[Dict[str, Any]] = []
        for _ in range(repeat):
            md = gc.generate_from_text(requirements_text, use_llm=use_llm, agentic=agentic)
            judged = llm_judge.score_cases(md)
            trials.append({"cases": count_cases(md), "judge": judged,
                           "total": judged.get("total")})
        totals = [t["total"] for t in trials if isinstance(t["total"], int)]
        dim_means: Dict[str, float] = {}
        for d in llm_judge.DIMENSIONS:
            vals = [t["judge"]["scores"].get(d) for t in trials
                    if t["judge"].get("enabled") and isinstance(t["judge"]["scores"].get(d), int)]
            if vals:
                dim_means[d] = _mean(vals)
        results.append({
            "key": key,
            "label": label,
            "trials": trials,
            "cases": trials[-1]["cases"],                 # 末次（兼容旧字段）
            "cases_mean": _mean([t["cases"] for t in trials]),
            "total": trials[-1]["total"],                 # 末次（兼容旧字段）
            "total_mean": _mean(totals),
            "total_min": min(totals) if totals else None,
            "total_max": max(totals) if totals else None,
            "dim_means": dim_means,
            "judge": trials[-1]["judge"],                 # 末次（兼容旧字段）
        })
    scored = [r for r in results if isinstance(r.get("total_mean"), float)]
    best = max(scored, key=lambda r: r["total_mean"])["key"] if scored else None
    return {"results": results, "best": best, "repeat": repeat}


def render_table(comparison: Dict[str, Any]) -> str:
    """把对比结果渲染成人类可读的表格文本。"""
    repeat = int(comparison.get("repeat", 1) or 1)
    lines: List[str] = []
    if repeat > 1:
        lines.append(f"{'模式':<14}{'用例数':>7}{'均分':>7}{'区间':>11}   （{repeat} 次独立采样均值）")
    else:
        lines.append(f"{'模式':<14}{'用例数':>6}{'总分':>8}")
    lines.append("-" * 44)
    for r in comparison["results"]:
        if repeat > 1:
            rng = (f"{r['total_min']}~{r['total_max']}"
                   if r.get("total_min") is not None else "—")
            m = r["total_mean"] if r["total_mean"] is not None else "—"
            cm = r["cases_mean"] if r["cases_mean"] is not None else r["cases"]
            lines.append(f"{r['label']:<14}{cm:>7}{m:>8}{rng:>12}")
        else:
            t = r["total"]
            lines.append(f"{r['label']:<14}{r['cases']:>6}{(t if t is not None else '—'):>8}")
    if comparison.get("best"):
        lines.append("-" * 44)
        suffix = f"（{repeat} 次均值）" if repeat > 1 else ""
        lines.append(f"最优：{comparison['best']}{suffix}")

    detail = [r for r in comparison["results"] if r.get("dim_means")]
    if detail:
        lines.append("")
        lines.append("分维度（均值）：" + " | ".join(llm_judge.DIMENSIONS))
        for r in detail:
            dm = r["dim_means"]
            lines.append(f"  {r['label']:<12}" + " | ".join(str(dm.get(d, "—")) for d in llm_judge.DIMENSIONS))
    else:
        lines.append("")
        lines.append("（judge 未启用：未配置 LLM 或调用失败，仅比较用例条数）")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="多种生成模式的用例质量对比评测")
    ap.add_argument("--input", "-i", required=True, help="需求文件路径")
    ap.add_argument("--modes", default="rule,llm,agentic",
                    help="参与对比的模式，逗号分隔（默认全部）")
    ap.add_argument("--repeat", type=int, default=1,
                    help="每个模式独立采样次数（>1 取均值；agentic 每次 4 次调用，注意耗时）")
    ap.add_argument("--llm-model", default=None, help="覆盖 LLM_MODEL（便于换快模型跑 agentic）")
    ap.add_argument("--llm-timeout", default=None, help="覆盖 LLM_TIMEOUT（秒）")
    args = ap.parse_args()

    if args.llm_model:
        os.environ["LLM_MODEL"] = args.llm_model
    if args.llm_timeout:
        os.environ["LLM_TIMEOUT"] = args.llm_timeout

    text = Path(args.input).read_text(encoding="utf-8")
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    print(f"== 用例质量对比评测（模型 {os.environ.get('LLM_MODEL', '默认')}，"
          f"采样 {max(1, args.repeat)} 次）==")
    comparison = compare_modes(text, modes=modes, repeat=max(1, args.repeat))
    print(render_table(comparison))


if __name__ == "__main__":
    main()
