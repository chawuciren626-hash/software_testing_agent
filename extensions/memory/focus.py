"""情景记忆回灌的**校验与缺口显形** —— 闭环的最后一公里。

分工（不要与 lessons.py 重叠）
-----------------------------
| 模块        | 负责                                              |
|-------------|---------------------------------------------------|
| `lessons`   | 失败聚类 → 重点覆盖清单 → **注入**生成提示           |
| `focus`     | 生成完之后**校验**：清单项到底覆盖到没有、缺哪些     |

为什么必须有这一步
------------------
不校验就宣称"用例已按历史易错点自优化"，等于把"提示词发出去了"当成"结果达成了"。
这是假绿的另一种形态：注入没被模型采纳、或清单里的场景跟本次需求压根无关时，
流水线照样打印"已注入历史易错点"，人看到的信号却是"优化过了"。

两个必须分清的数字（关键，别合并）
----------------------------------
- **native（生成即覆盖）**：本轮用例**自发**覆盖了几个重点项 —— 这才是"回灌起作用了"的证据。
- **backfilled（回灌补齐）**：缺失项由本模块**自动补一条骨架用例**后凑到的覆盖数。

只报"覆盖率 100%"而不说其中多少是补齐的，就是把补齐当成模型学会了，属于自我感动。
所以两者**分开落盘、分开展示**，报告里写清楚"其中 N 条为回灌补齐"。

补齐为什么不是编造
------------------
补齐的是**骨架**（标题+步骤/预期占位 + 标注来源与历史失败次数），不是凭空断言业务行为：
- 标题写明"【回灌】<场景名> —— 历史失败 N 次"；
- 步骤/预期是模板占位并注明"需人工细化"，与规则版其它用例口径一致；
- 不参与结构质量分的"具体性"加分（本来就写不出具体值，硬给反而注水）。

诚实边界
--------
- 匹配用的是与 `knowledge` 同一套**关键词 / 中文 2-gram 重合**（`terms()`），不是语义理解。
  清单项叫"登录超时"、用例写"会话过期"就匹配不上 → **保守记为未覆盖**。
- 阈值默认 0.34（约三分之一词重合）。低于此判未覆盖。
- ⚠️ **已知会偏乐观的一面**：清单项之间高度相似时会一起被同一条用例"覆盖"。
  实测 zz-fail 项目里 `错误密码A` / `错误密码B` 两个场景，被同一条含"密码"的用例同时判为已覆盖 ——
  它们的 2-gram 几乎重合，关键词匹配分不开。所以**覆盖率只能当作粗粒度信号**（重点项整体有没有被照顾到），
  不能当成"每个易错场景都有专门用例"的证据。要看后者得人工看 cases.md 或上语义匹配。
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

try:                                    # 与 knowledge 共用一套切词口径，不另起炉灶
    from knowledge import terms as _terms
except ImportError:                     # pragma: no cover - 仅在未把目录加入 sys.path 时
    from .knowledge import terms as _terms  # type: ignore

# 低于这个比例即判未覆盖。取值理由见模块 docstring。
_MIN_RATIO = 0.34
FOCUS_FILE = "focus_history.jsonl"

# 回灌补齐的骨架用例**持久化**到项目目录（与 lessons.md / knowledge.md 同级）。
# 为什么必须落盘：cases.md 每轮都从需求重新生成，补齐行会被冲掉 ——
# 不持久化的话覆盖率永远在原地打转，"重复 run 易错场景覆盖率上升"就无从谈起。
SUPPLEMENT_FILE = "focus_supplement.md"
_SUPP_NAME = re.compile(r"【回灌】(.*?)（历史失败")

GAP_HEADING = "## ⚠️ 本轮未覆盖的历史易错点"
_ROW_FIELDS = ("标题", "模块", "类型", "优先级", "前置", "步骤", "预期", "可自动化")


# --------------------------------------------------------------------------- #
# 覆盖校验
# --------------------------------------------------------------------------- #
def _row_text(row: Dict[str, str]) -> str:
    """把一条用例拼成可匹配的文本（标题/模块/步骤/预期都算，前置条件不算）。"""
    return " ".join(str(row.get(k, "")) for k in ("标题", "模块", "步骤", "预期"))


def is_covered(item_name: str, rows: Sequence[Dict[str, str]],
               min_ratio: float = _MIN_RATIO) -> bool:
    """单个重点项是否被这批用例覆盖。"""
    i_terms = set(_terms(item_name or ""))
    if not i_terms:
        # 切不出词（纯标点/单字）→ 无法判定，按未覆盖处理：算不出就不猜。
        return False
    for r in rows:
        r_terms = set(_terms(_row_text(r)))
        if not r_terms:
            continue
        if len(i_terms & r_terms) / len(i_terms) >= min_ratio:
            return True
    return False


def check(rows: Sequence[Dict[str, str]],
          items: Sequence[Dict[str, Any]],
          min_ratio: float = _MIN_RATIO) -> Dict[str, Any]:
    """校验"本轮用例"对"重点覆盖清单"的覆盖情况。

    rows  : `_parse_cases` 的用例行
    items : `lessons.cluster_failures` 的重点项（含 name / count / detail）

    返回 total / covered / missing / rate。rate 为 None 表示**无法判定**（清单为空），
    调用方要如实显示"未计分"，不能填 0 也不能填 100。
    """
    if not items:
        return {"total": 0, "covered": [], "missing": [], "rate": None}
    covered: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []
    for it in items:
        name = str(it.get("name", ""))
        rec = {"name": name, "count": int(it.get("count", 0) or 0),
               "detail": str(it.get("detail") or "")}
        (covered if is_covered(name, rows, min_ratio) else missing).append(rec)
    total = len(items)
    return {
        "total": total,
        "covered": covered,
        "missing": missing,
        "rate": round(len(covered) / total * 100, 1) if total else None,
    }


# --------------------------------------------------------------------------- #
# 缺口显形 + 回灌补齐
# --------------------------------------------------------------------------- #
def supplement_rows(missing: Sequence[Dict[str, Any]], start_id: int = 1) -> List[str]:
    """为缺失重点项生成**骨架用例行**（Markdown 表格行）。

    只补骨架、不编业务细节：步骤与预期是占位并注明需人工细化。
    """
    rows: List[str] = []
    for i, m in enumerate(missing, start=1):
        name = str(m.get("name", "")).strip()
        cnt = int(m.get("count", 0) or 0)
        cid = f"FOCUS-{start_id + i - 1:03d}"
        rows.append(
            f"| {cid} | 【回灌】{name}（历史失败 {cnt} 次） | 历史易错 | 异常 | P1 | 无 | "
            f"1. 复现「{name}」场景 2. 观察系统行为（步骤需人工细化） | "
            f"不再出现历史失败表现（预期需人工细化） | 待定 |"
        )
    return rows


def render_gap_section(result: Dict[str, Any]) -> str:
    """渲染 cases.md 末尾的「未覆盖」说明章节。

    补齐之后这一节依然存在 —— 因为它记录的是"本轮**生成时**没覆盖到"，
    这条信息不会因为后来补了骨架就消失，否则人就看不到模型其实没学会。
    """
    missing = result.get("missing") or []
    if not missing:
        return ""
    lines = ["", GAP_HEADING, "",
             f"> 本轮生成**未自发覆盖** {len(missing)} 个历史易错点，"
             "已由流水线补为骨架用例（标题带【回灌】），步骤与预期需人工细化。", ""]
    for m in missing:
        lines.append(f"- **{m['name']}** —— 历史失败 {m['count']} 次")
        if m.get("detail"):
            lines.append(f"  - 典型原因：{str(m['detail'])[:200]}")
    return "\n".join(lines) + "\n"


def apply(cases_md: str, items: Sequence[Dict[str, Any]],
          min_ratio: float = _MIN_RATIO,
          rows: Optional[Sequence[Dict[str, str]]] = None) -> Dict[str, Any]:
    """一次做完：校验 → 补齐 → 返回结论。**不改文件**，只返回新文本由调用方落盘。

    rows 可由调用方传入（避免重复解析）；不传时**无法解析**则按无用例处理（保守）。
    """
    if rows is None:
        rows = _parse_rows(cases_md)
    result = check(rows, items, min_ratio)
    result["native"] = len(result["covered"])      # 生成即覆盖
    result["backfilled"] = len(result["missing"])  # 回灌补齐
    if not result["missing"]:
        result["md"] = cases_md
        return result
    new_md = _append_rows(cases_md, supplement_rows(result["missing"]))
    result["md"] = new_md + render_gap_section(result)
    return result


# --------------------------------------------------------------------------- #
# 内部：Markdown 表格操作（只依赖格式，不依赖生成器实现）
# --------------------------------------------------------------------------- #
# 表格行解析收敛到 extensions/common/cases.parse_rows（唯一定义处）——
# 不再"本地最小实现"，避免与 project_manager / case_quality 各自跑偏。
# 保留 `_parse_rows` 名字作为别名，兼容既有内部调用与测试。
_EXTENSIONS_DIR = Path(__file__).resolve().parents[1]      # extensions/
if str(_EXTENSIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_EXTENSIONS_DIR))
from common.cases import parse_rows as _parse_rows         # noqa: E402


def _append_rows(md: str, rows: Sequence[str]) -> str:
    """把补充行插到**表格末尾**（表格后的其它章节保持在其后）。"""
    if not rows:
        return md
    lines = (md or "").splitlines()
    last = -1
    for i, ln in enumerate(lines):
        if ln.strip().startswith("|"):
            last = i
    if last < 0:
        return md.rstrip() + "\n" + "\n".join(rows) + "\n"
    lines[last + 1:last + 1] = list(rows)
    return "\n".join(lines) + "\n"


def to_summary(result: Dict[str, Any]) -> str:
    """一行摘要，供 CLI 打印 / 报告卡片使用；无法判定时如实说"未计分"。"""
    total = result.get("total") or 0
    if not total:
        return "重点覆盖：—（无历史易错点清单，未计分）"
    native = result.get("native", len(result.get("covered") or []))
    parts = [f"{native}/{total} 为生成即覆盖"]
    # 沿用上轮的与本轮新增的分开说：都叫"补齐"就看不出这轮到底新缺了多少
    persisted = int(result.get("persisted", 0) or 0)
    back = int(result.get("backfilled", 0) or 0)
    if persisted:
        parts.append(f"{persisted} 条沿用上轮回灌")
    if back:
        parts.append(f"{back} 条本轮新增补齐")
    return "重点覆盖：" + "，".join(parts)


# --------------------------------------------------------------------------- #
# 补齐结果的持久化（让覆盖真的能跨轮累积）
# --------------------------------------------------------------------------- #
def read_supplement(pdir: Path) -> List[str]:
    """读出上几轮回灌补的骨架行（Markdown 表格行原文）。"""
    fp = Path(pdir) / SUPPLEMENT_FILE
    if not fp.is_file():
        return []
    return [ln for ln in fp.read_text(encoding="utf-8").splitlines()
            if ln.strip().startswith("|") and "【回灌】" in ln]


def supplement_names(rows: Sequence[str]) -> List[str]:
    """从骨架行里取回它对应的是哪个重点项（用于与当前清单对账、剔除已过期的）。"""
    out: List[str] = []
    for r in rows:
        m = _SUPP_NAME.search(r)
        if m:
            out.append(m.group(1).strip())
    return out


def save_supplement(pdir: Path, rows: Sequence[str]) -> Path:
    """覆盖写回骨架行。整份重写而不是追加 —— 清单变了要能真的删掉旧项。"""
    fp = Path(pdir) / SUPPLEMENT_FILE
    if not rows:
        # 没有补齐内容就删掉文件：留一个空文件会让人以为"有配置但没生效"
        if fp.is_file():
            fp.unlink()
        return fp
    lines = ["# 历史易错点回灌补齐（自动生成，勿手改）", "",
             "> 这些行由流水线按历史失败补齐，每次生成用例后自动并入 cases.md。",
             "> 对应的易错点不再出现在 lessons.md 后会自动移除。", "",
             "| id | 标题 | 模块 | 类型 | 优先级 | 前置 | 步骤 | 预期 | 可自动化 |",
             "|---|---|---|---|---|---|---|---|---|",
             *rows, ""]
    fp.write_text("\n".join(lines), encoding="utf-8")
    return fp


def apply_persistent(pdir: Path, cases_md: str,
                     items: Sequence[Dict[str, Any]],
                     min_ratio: float = _MIN_RATIO) -> Dict[str, Any]:
    """带持久化的完整闭环：**生成** → 并入上轮补齐 → 校验 → 补新缺口 → 存回去。

    三个数字必须分开（合并成"覆盖率"就会把三件事混为一谈）：
      native     : 本轮**生成时**就覆盖的（模型/规则自己写出来的 —— 这才是真的学会了）
      persisted  : 靠**上几轮**补的骨架才覆盖的（说明本轮生成还是没覆盖到）
      backfilled : 本轮**新发现**缺口并补齐的

    返回 dict（含改写后的 md），**不落盘 cases.md**，由调用方决定。
    """
    # 1) 先量"生成即覆盖"：这一步必须在并入任何回灌行之前，否则会把补出来的算成模型写的
    res_native = check(_parse_rows(cases_md), items, min_ratio)
    native = len(res_native["covered"])

    # 2) 并入上轮补齐行（只保留当前清单里还存在的项：易错点不再出现就该退场）
    _all = read_supplement(pdir)
    keep = [r for r in _all
            if any(_same_item(supplement_names([r]), it.get("name", "")) for it in items)]
    md = _append_rows(cases_md, keep) if keep else cases_md
    persisted = len(keep)

    # 3) 再校验一次，剩下没覆盖的就是本轮新缺口
    res2 = check(_parse_rows(md), items, min_ratio)
    missing = res2["missing"]
    new_rows = supplement_rows(missing, start_id=persisted + 1)
    md = _append_rows(md, new_rows) if new_rows else md

    save_supplement(pdir, keep + new_rows)
    total = len(items)
    covered = native if not (keep or new_rows) else len(check(_parse_rows(md), items, min_ratio)["covered"])
    return {
        "total": total,
        "covered": res2["covered"],
        "missing": missing,
        "native": native,
        "persisted": persisted,
        "backfilled": len(new_rows),
        "rate": round(covered / total * 100, 1) if total else None,
        "md": md + render_gap_section(res2),
    }


def _same_item(names: Sequence[str], item_name: str) -> bool:
    """骨架行对应的重点项是否仍在当前清单里（按原名精确对账，不做模糊匹配）。"""
    return any(n == str(item_name).strip() for n in names)


# --------------------------------------------------------------------------- #
# 趋势（验收口径：同一项目重复 run，易错场景覆盖率不应下降）
# --------------------------------------------------------------------------- #
def record_focus(pdir: Path, result: Dict[str, Any],
                 mode: Optional[str] = None) -> Path:
    """把本次结论追加进 `artifacts/focus_history.jsonl`（与 quality 同样的落盘约定）。"""
    p = Path(pdir) / "artifacts"
    p.mkdir(parents=True, exist_ok=True)
    total = int(result.get("total") or 0)
    native = int(result.get("native", len(result.get("covered") or [])))
    rec = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total": total,
        "native": native,
        "backfilled": int(result.get("backfilled", 0)),
        "native_rate": round(native / total * 100, 1) if total else None,
        "mode": mode or "",
    }
    with (p / FOCUS_FILE).open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return p / FOCUS_FILE


def read_history(pdir: Path) -> List[Dict[str, Any]]:
    fp = Path(pdir) / "artifacts" / FOCUS_FILE
    if not fp.is_file():
        return []
    out: List[Dict[str, Any]] = []
    for ln in fp.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue        # 可忽略：坏行跳过而不是让整条历史失效（单行脏数据不该毁掉趋势）
    return out


def delta(history: Sequence[Dict[str, Any]]) -> Optional[float]:
    """最近一次相对上一次的**生成即覆盖率**变化；数据不足返回 None。

    注意区分 None（首次，无法比较）与 0.0（持平）—— 用 `if not d` 会把持平显示成"没跑过"。
    """
    rates = [h.get("native_rate") for h in history if h.get("native_rate") is not None]
    if len(rates) < 2:
        return None
    return round(rates[-1] - rates[-2], 1)
