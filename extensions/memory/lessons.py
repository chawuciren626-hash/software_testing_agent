"""情景记忆闭环：失败根因聚类 -> 重点覆盖清单 -> 注入下次 run。

数据源：run_store.snapshots（每次 run/regression 的回归结果），由调用方读取后传入，
本模块保持纯函数，便于单测、不依赖 web_console 与 Flask。

闭环：
  1. 每次 run / regression 完成后把结果写快照（insert_snapshot）。
  2. 从该项目历史快照提取 FAIL 项，按场景名聚类高频易错点。
  3. 生成 projects/<id>/lessons.md（情景记忆 + 重点覆盖清单）。
  4. 下次 run 的「需求→用例」阶段读取 lessons.md，作为历史易错点注入提示，
     让 LLM 版直接加强对这些点的覆盖，规则版则追加建议段供人工参考。

向后兼容：无快照 / 无 lessons 时，行为与现在完全一致（inject 返回 None）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

LESSONS_FILE = "lessons.md"
_INJECT_TMPL = (
    "根据本项目历史回归失败根因，以下场景需**重点覆盖**"
    "（请在生成用例时优先 / 加强这些点）：\n\n"
)


def extract_failures(snaps: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """从快照序列提取所有 FAIL 项（name + 典型原因）。"""
    out: List[Dict[str, str]] = []
    for s in snaps:
        for r in s.get("results") or []:
            if str(r.get("result", "")).upper() == "FAIL":
                out.append({
                    "name": str(r.get("name", "")),
                    "detail": str(r.get("detail") or r.get("snippet") or ""),
                    "ts": s.get("ts"),
                    "scene": s.get("scene") or "",
                })
    return out


def cluster_failures(failed: List[Dict[str, str]], top_n: int = 10) -> List[Dict[str, Any]]:
    """按场景名聚类失败频次，返回高频易错点（降序）。"""
    freq: Dict[str, int] = {}
    sample: Dict[str, str] = {}
    for f in failed:
        n = f["name"]
        freq[n] = freq.get(n, 0) + 1
        if n not in sample:
            sample[n] = f.get("detail", "")[:300]
    ranked = sorted(freq.items(), key=lambda x: x[1], reverse=True)[:top_n]
    return [{"name": n, "count": c, "detail": sample.get(n, "")} for n, c in ranked]


def render_lessons_md(clusters: List[Dict[str, Any]]) -> str:
    if not clusters:
        return ""
    lines = [
        "# 情景记忆 · 历史失败根因与重点覆盖清单",
        "",
        f"> 由回归快照自动聚类生成（{len(clusters)} 个高频易错点）。"
        "下次「需求→用例」会自动加强对这些点的覆盖。",
        "",
    ]
    for i, c in enumerate(clusters, 1):
        lines.append(f"{i}. **{c['name']}** —— 历史失败 {c['count']} 次")
        if c.get("detail"):
            lines.append(f"   - 典型原因：{c['detail'][:200]}")
    lines.append("")
    return "\n".join(lines)


def save_lessons(pdir: Path, md: str) -> Path:
    lp = pdir / LESSONS_FILE
    lp.write_text(md, encoding="utf-8")
    return lp


def load_lessons(pdir: Path) -> Optional[str]:
    lp = pdir / LESSONS_FILE
    if lp.is_file():
        return lp.read_text(encoding="utf-8")
    return None


def to_inject_prompt(pdir: Path) -> Optional[str]:
    """读取 lessons.md，转成可拼到需求文本里的注入段落；无则返回 None。

    标题以 ``#`` 开头——规则版 parse_requirements 会跳过标题行，不会污染需求解析；
    LLM 版则把整段作为上下文加强覆盖。
    """
    md = load_lessons(pdir)
    if not md:
        return None
    return "\n\n# 历史易错点（重点覆盖）\n" + _INJECT_TMPL + md


def rebuild_from_snapshots(snaps: List[Dict[str, Any]], pdir: Path) -> Optional[str]:
    """从快照聚类失败，更新 lessons.md；返回 lessons 文本或 None（无失败则清空旧文件）。"""
    pdir = Path(pdir)
    failed = extract_failures(snaps)
    if not failed:
        lp = pdir / LESSONS_FILE
        if lp.is_file():
            lp.unlink()
        return None
    clusters = cluster_failures(failed)
    md = render_lessons_md(clusters)
    save_lessons(pdir, md)
    return md
