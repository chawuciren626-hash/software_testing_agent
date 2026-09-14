"""用例**结构**质量分：确定性、零依赖、每次运行都能算。

它是什么
--------
对生成出来的用例做**结构性体检**，回答"这批用例在形式上是否完整"：
每条需求是否都覆盖到了、功能/边界/异常三类是否齐、步骤与预期是否写到可执行的程度、
有没有具体数据、有没有重复。

它**不是**什么（重要）
----------------------
- **不是"用例好不好"的判定**。它看的是形式，不是语义正确性：
  "输入错误密码"这条用例，写对了边界值可以得高分，写错成"输入正确密码"照样得高分。
  语义质量只有 LLM-as-judge（`tests/eval/llm_judge.py`）能评，两者**互补、不可互相替代**。
- **因此默认不做硬门禁**。结构分是可以被"注水"刷高的（多写几个数字、多凑几条重复用例），
  拿它卡流水线等于鼓励刷分。它的正确用法是**看趋势**：
  同样的需求与模式，分数突然掉下来 → 说明生成环节出了问题，值得查。
  真要卡阈值用 `--quality-min`（由调用方按需开启）。

为什么必须"每次运行都能算"
--------------------------
LLM judge 需要 key、要花钱、单次判分还有方差（同一样本能打出 100 和 90），
注定只能是**抽样评测**手段。而"这次 run 的用例比上次差了"这种信号，
只有在**每次都算**的情况下才拿得到 —— 这就是本模块存在的理由。

评分维度（0-100，越高越好）
--------------------------
===========  =====  ==================================================
维度         权重   含义
===========  =====  ==================================================
coverage     0.30   需求覆盖率：被用例覆盖到的需求 / 需求总数
types        0.25   三类齐备：每条需求是否 功能 / 边界 / 异常 都有
executable   0.25   可执行性：步骤有动作、预期可判定
specificity  0.10   具体性：是否写到具体数据（数值 / 边界词 / 特殊值）
dedup        0.10   去重：用例之间不重复的程度
===========  =====  ==================================================

**无法计分的维度不猜分**：例如需求条数未知时算不出覆盖率，该维度记为 ``None``
并在 ``notes`` 里说明，总分按剩余维度**重新归一化**权重 ——
宁可少一个维度，也不编一个数把总分凑出来（凑出来的分会让人误以为测过了）。
"""
from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

DIMENSIONS: Tuple[str, ...] = ("coverage", "types", "executable", "specificity", "dedup")
WEIGHTS: Dict[str, float] = {"coverage": 0.30, "types": 0.25, "executable": 0.25,
                             "specificity": 0.10, "dedup": 0.10}
LABELS: Dict[str, str] = {
    "coverage": "需求覆盖",
    "types": "三类齐备",
    "executable": "可执行性",
    "specificity": "具体性",
    "dedup": "去重",
}
HINTS: Dict[str, str] = {
    "coverage": "被用例覆盖到的需求占比",
    "types": "每条需求是否 功能/边界/异常 三类都有",
    "executable": "步骤有动作、预期可判定",
    "specificity": "是否写到具体数据（数值/边界词/特殊值）",
    "dedup": "用例之间不重复的程度",
}

QUALITY_FILE = "quality.json"
QUALITY_HISTORY_FILE = "quality_history.jsonl"
HISTORY_KEEP = 200  # 每个项目最多保留的历史点数（追加时截断，避免无限膨胀）

_REQ_ID = re.compile(r"REQ-(\d+)", re.IGNORECASE)
# 类型列的常见写法归一到三类
_TYPE_ALIASES: Dict[str, str] = {
    "功能": "功能", "正常": "功能", "正向": "功能", "正例": "功能",
    "边界": "边界", "边界值": "边界",
    "异常": "异常", "负向": "异常", "反例": "异常", "错误": "异常", "失败": "异常",
}
_ACTION = re.compile(r"(输入|点击|选择|提交|调用|请求|访问|打开|上传|下载|删除|新增|修改|"
                     r"查询|登录|登出|退出|执行|设置|勾选|取消|等待|断言|校验|刷新|返回)")
_EXPECT = re.compile(r"(应|预期|返回|提示|显示|成功|失败|状态码|报错|拒绝|拦截|为空|一致|正确|不出现|无)")
_STEP_NO = re.compile(r"(^|[;；]\s*)\d+[.)、]\s*")
# 具体性 = 给出了**可直接照做的数据**，不是"最小值"这类占位描述。
# ⚠️ 不能直接用 `\d` 判断：步骤编号（"1. 打开登录页"）本身就带数字，
#    那样规则版模板也能拿满分 → 分数饱和、趋势失去意义。先把编号剥掉再判。
_QUOTED = re.compile(r"[\"'“”「『][^\"'“”」』]{1,30}[\"'“”」』]")   # 引号里的具体值
_NUM_UNIT = re.compile(r"\d+\s*(个字符|字符|位|次|秒|毫秒|ms|条|个|项|页|MB|KB|GB|%|分钟|天|小时)")
_STATUS = re.compile(r"\b[1-5]\d{2}\b")                            # HTTP 状态码
_RANGE = re.compile(r"[\d.]+\s*(~|～|-|—|至|到)\s*[\d.]+")         # 数值区间
_SPECIAL = re.compile(r"null|None|空字符串|为空|空白|负数|超长|超界|越界|溢出|超时|"
                      r"特殊字符|非法字符|最大长度|最小长度|长度\s*\d+|上限|下限|临界|并发")


# ---------------------------------------------------------------- 解析


# 表格行解析收敛到 extensions/common/cases.parse_rows（唯一定义处）——
# 直接复用共享实现，不反向依赖 project_manager，也不再维护本地副本。
# 保留 `parse_rows` 名字作为别名，兼容既有调用与 tests/test_case_quality.py。
_EXTENSIONS_DIR = Path(__file__).resolve().parents[1]      # extensions/
if str(_EXTENSIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_EXTENSIONS_DIR))
from common.cases import parse_rows                        # noqa: E402


def _req_of(row: Dict[str, str]) -> Optional[int]:
    """用例所属需求序号；识别不出返回 None（不猜）。"""
    for key in ("id", "ID", "编号"):
        m = _REQ_ID.search(str(row.get(key, "")))
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
    return None


def _type_of(row: Dict[str, str]) -> Optional[str]:
    raw = str(row.get("类型", row.get("type", ""))).strip()
    if not raw:
        return None
    if raw in _TYPE_ALIASES:
        return _TYPE_ALIASES[raw]
    for k, v in _TYPE_ALIASES.items():
        if k in raw:
            return v
    return None


def _strip_step_no(text: str) -> str:
    """剥掉步骤编号（`1. ` / `2) ` / `3、`），避免把编号当成具体数据。"""
    out = _STEP_NO.sub("", text or "")
    return re.sub(r"[①-⑨]", "", out)


def has_specific(row: Dict[str, str]) -> bool:
    """这条用例是否给出了可直接照做的具体数据。"""
    blob = _strip_step_no(" ".join(str(row.get(k, "")) for k in
                                   ("步骤", "预期", "预期结果", "前置", "标题")))
    return bool(_QUOTED.search(blob) or _NUM_UNIT.search(blob) or _STATUS.search(blob)
                or _RANGE.search(blob) or _SPECIAL.search(blob))


def _field(row: Dict[str, str], *names: str) -> str:
    for n in names:
        v = str(row.get(n, "")).strip()
        if v:
            return v
    return ""


# ---------------------------------------------------------------- 打分


def score_rows(rows: Sequence[Dict[str, str]],
               requirement_count: Optional[int] = None) -> Dict[str, Any]:
    """对用例行打结构分。

    返回：::

        {"total": 87, "dims": {"coverage": 100, ...}, "notes": [...],
         "counts": {"cases": 9, "requirements": 3, "covered": 3, "dup": 0},
         "scored_at": "2026-09-11 18:00"}

    维度算不出来时置 ``None``（不猜分），并在 ``notes`` 说明；
    ``total`` 按剩余维度重新归一化权重；一个维度都算不出 → ``total`` 为 ``None``。
    """
    dims: Dict[str, Optional[int]] = {d: None for d in DIMENSIONS}
    notes: List[str] = []
    rows = list(rows or [])
    counts = {"cases": len(rows), "requirements": requirement_count, "covered": 0, "dup": 0}

    if not rows:
        return {"total": None, "dims": {}, "notes": ["没有解析到任何用例，无法打分"],
                "counts": counts,
                "scored_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}

    reqs = {r for r in (_req_of(row) for row in rows) if r is not None}
    identified = len(reqs) > 0

    # --- coverage：需求覆盖 ---
    if requirement_count and requirement_count > 0:
        if identified:
            counts["covered"] = len(reqs)
            dims["coverage"] = _pct(len(reqs) / requirement_count)
        else:
            notes.append("用例 id 不符合 REQ-NNN-X 规范，无法定位所属需求：需求覆盖未计分")
    else:
        notes.append("未提供需求条数：需求覆盖未计分")

    # --- types：三类齐备（按需求分组） ---
    if identified:
        groups: Dict[int, set] = {}
        for row in rows:
            r = _req_of(row)
            t = _type_of(row)
            if r is not None and t:
                groups.setdefault(r, set()).add(t)
        if groups:
            dims["types"] = _pct(sum(len(v) for v in groups.values()) / (3 * len(groups)))
        else:
            notes.append("识别不到用例类型列：三类齐备未计分")
    else:
        notes.append("用例 id 不符合 REQ-NNN-X 规范：三类齐备未计分")

    # --- executable：步骤有动作 + 预期可判定 ---
    ok_exec = 0
    for row in rows:
        steps = _field(row, "步骤", "steps")
        expected = _field(row, "预期", "预期结果", "expected")
        if steps and expected and _ACTION.search(steps) and _EXPECT.search(expected):
            ok_exec += 1
    dims["executable"] = _pct(ok_exec / len(rows))

    # --- specificity：写到了具体数据 ---
    ok_spec = sum(1 for row in rows if has_specific(row))
    dims["specificity"] = _pct(ok_spec / len(rows))

    # --- dedup：标题+步骤归一化后的重复率 ---
    seen: Dict[str, int] = {}
    for row in rows:
        key = re.sub(r"\s+", "", f"{_field(row, '标题', 'title')}|{_field(row, '步骤', 'steps')}")
        seen[key] = seen.get(key, 0) + 1
    dup = sum(v - 1 for v in seen.values() if v > 1)
    counts["dup"] = dup
    dims["dedup"] = _pct(1 - dup / len(rows))
    if dup:
        notes.append(f"检测到 {dup} 条重复用例（标题+步骤完全相同）")

    scored = {d: v for d, v in dims.items() if v is not None}
    total: Optional[int] = None
    if scored:
        wsum = sum(WEIGHTS[d] for d in scored)
        total = int(round(sum(WEIGHTS[d] * v for d, v in scored.items()) / wsum))
    if len(scored) < len(DIMENSIONS):
        missing = [LABELS[d] for d in DIMENSIONS if dims.get(d) is None]
        notes.append("未计分维度：" + "、".join(missing) + f"（总分按剩余 {len(scored)} 维归一化）")
    if scored and all(v >= 100 for v in scored.values()):
        # 满分饱和必须说出来：否则用户会把"形式完整"误读成"用例质量好"。
        notes.append("所有已计分维度均为满分：结构分已饱和，只能说明「形式完整」，"
                     "不能说明用例好（语义质量请用 LLM judge 抽样评测）")

    return {"total": total, "dims": {d: dims[d] for d in DIMENSIONS},
            "notes": notes, "counts": counts,
            "scored_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}


def score_cases_md(md_text: str, requirement_count: Optional[int] = None) -> Dict[str, Any]:
    """直接对 cases.md 文本打分（便捷入口）。"""
    return score_rows(parse_rows(md_text), requirement_count=requirement_count)


def check_min(result: Dict[str, Any], min_score: Optional[int]) -> Tuple[bool, str]:
    """可选阈值判定（默认不启用）：返回 ``(是否达标, 说明)``。

    **算不出分数时不判达标** —— 把"没算出来"当"达到要求"是典型的假绿，
    和"环境不可达不判绿"是同一条原则。
    """
    if min_score is None:
        return True, ""
    total = result.get("total")
    if total is None:
        return False, "无法计分，不能按达标处理（请先看报告确认用例是否真的生成了）"
    if int(total) < int(min_score):
        return False, f"结构质量分 {total} < {min_score}"
    return True, f"结构质量分 {total} ≥ {min_score}"


def _pct(x: float) -> int:
    """比例 → 0-100 整数（夹紧，避免浮点越界）。"""
    return int(round(max(0.0, min(1.0, x)) * 100))


# ---------------------------------------------------------------- 展示


def render_text(result: Dict[str, Any], title: str = "用例结构质量分") -> str:
    """纯文本摘要（可直接粘到群里 / 打印到 CI 日志）。"""
    total = result.get("total")
    lines = [f"== {title} =="]
    lines.append(f"总分：{total if total is not None else '—'}"
                 f"（0-100，越高越好；结构分，非语义判定）")
    dims = result.get("dims") or {}
    for d in DIMENSIONS:
        v = dims.get(d)
        if v is None:
            continue
        lines.append(f"  {LABELS[d]:<6}{v:>4}  {_bar(v)}  · {HINTS[d]}")
    counts = result.get("counts") or {}
    lines.append(f"用例 {counts.get('cases', 0)} 条"
                 + (f" / 需求 {counts.get('requirements')} 条"
                    f"（覆盖 {counts.get('covered', 0)}）" if counts.get("requirements") else "")
                 + (f" / 重复 {counts.get('dup', 0)} 条" if counts.get("dup") else ""))
    for n in result.get("notes", []):
        lines.append(f"  · {n}")
    return "\n".join(lines)


def _bar(v: int, width: int = 20) -> str:
    """文本进度条，便于在纯文本环境里一眼看出强弱。"""
    n = int(round(v / 100 * width))
    return "█" * n + "·" * (width - n)


def sparkline(values: Sequence[int], width: int = 132, height: int = 30) -> str:
    """把历史总分渲染成内联 SVG 折线（无外部依赖、无 JS）。

    少于 2 个点画不出趋势 → 返回空串（不画一条自欺欺人的"平线"）。
    """
    vals = [int(v) for v in values if isinstance(v, int)]
    if len(vals) < 2:
        return ""
    n = len(vals)
    step = width / (n - 1)
    pts = [(i * step, height - (v / 100) * height) for i, v in enumerate(vals)]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    first, last = vals[0], vals[-1]
    color = "#34d399" if last > first else ("#f87171" if last < first else "#94a3b8")
    dots = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="1.8" fill="{color}"/>' for x, y in pts)
    return (f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="质量分趋势">'
            f'<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="1.6"/>'
            f"{dots}</svg>")


# ---------------------------------------------------------------- 历史


def history_path(pdir: Path) -> Path:
    return Path(pdir) / "artifacts" / QUALITY_HISTORY_FILE


def append_history(pdir: Path, result: Dict[str, Any], keep: int = HISTORY_KEEP,
                   mode: Optional[str] = None) -> Path:
    """把本次分数追加到 `artifacts/quality_history.jsonl`（趋势数据源）。

    只存**结论数字**不存用例全文：历史文件要能长期留存，塞全文会膨胀到没法看。
    """
    p = history_path(pdir)
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": result.get("scored_at") or datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total": result.get("total"),
        "dims": {k: v for k, v in (result.get("dims") or {}).items() if v is not None},
        "cases": (result.get("counts") or {}).get("cases", 0),
        "mode": mode,
    }
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if len(lines) > keep:
        p.write_text("\n".join(lines[-keep:]) + "\n", encoding="utf-8")
    return p


def read_history(pdir: Path, limit: int = HISTORY_KEEP) -> List[Dict[str, Any]]:
    """读取历史（坏行跳过，不因一行脏数据让整个趋势挂掉）。"""
    p = history_path(pdir)
    if not p.is_file():
        return []
    out: List[Dict[str, Any]] = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            obj = json.loads(ln)
            if isinstance(obj, dict):
                out.append(obj)
        except ValueError:
            continue
    return out[-limit:]


def delta(history: Sequence[Dict[str, Any]]) -> Optional[int]:
    """最近一次相对上一次的总分变化；数据不足返回 None（不编造"持平"）。"""
    totals = [h.get("total") for h in history if isinstance(h.get("total"), int)]
    if len(totals) < 2:
        return None
    return int(totals[-1]) - int(totals[-2])


def trend_series(history: Sequence[Dict[str, Any]]) -> List[int]:
    return [int(h["total"]) for h in history if isinstance(h.get("total"), int)]


def record_quality(pdir: Path, result: Dict[str, Any], mode: Optional[str] = None,
                   keep: int = HISTORY_KEEP) -> Dict[str, Any]:
    """**推荐入口**：追加历史 + 写 `quality.json`，一次做完。

    为什么要有这么个一步到位的函数：`append_history` 与 `write_quality` 分开调用时存在
    顺序坑——先 write 后 append，则本次分数还没进历史、环比算成 `None`；
    先 append 后 write 又要求调用方记得把新历史传进去。把顺序固定在这里，调用方不会踩。
    """
    append_history(pdir, result, keep=keep, mode=mode)
    payload = write_quality(pdir, result, history=read_history(pdir))
    return {"path": str(payload), "delta": result.get("delta"), **result}


def write_quality(pdir: Path, result: Dict[str, Any],
                  history: Optional[Sequence[Dict[str, Any]]] = None) -> Path:
    """把打分结果（含趋势）写到 `artifacts/quality.json`，供报告/看板/控制台读取。"""
    art = Path(pdir) / "artifacts"
    art.mkdir(parents=True, exist_ok=True)
    hist = list(history if history is not None else read_history(pdir))
    payload = dict(result)
    payload["delta"] = delta(hist)
    payload["history"] = hist
    f = art / QUALITY_FILE
    f.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return f


def read_quality(pdir: Path) -> Optional[Dict[str, Any]]:
    f = Path(pdir) / "artifacts" / QUALITY_FILE
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def main() -> None:
    """独立使用：`python case_quality.py cases.md --requirements 3`"""
    import argparse

    ap = argparse.ArgumentParser(description="用例结构质量分（确定性、零依赖）")
    ap.add_argument("cases", help="cases.md 路径")
    ap.add_argument("--requirements", "-r", type=int, default=None, help="需求条数")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--min", type=int, default=None, metavar="N",
                    help="可选：总分低于 N 时以退出码 1 结束（默认不卡，见模块文档）")
    args = ap.parse_args()

    md = Path(args.cases).read_text(encoding="utf-8")
    res = score_cases_md(md, requirement_count=args.requirements)
    print(json.dumps(res, ensure_ascii=False, indent=2) if args.json
          else render_text(res))
    ok, msg = check_min(res, args.min)
    if args.min is not None:
        print(("\n✅ " if ok else "\n❌ ") + msg)
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
