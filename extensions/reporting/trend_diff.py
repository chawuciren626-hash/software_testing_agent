"""失败项新旧对比：把「这次新红的」从「一直红的」里挑出来。

为什么需要它
------------
只报"哪些场景红了"会让人疲劳——红灯长期挂在同样的 5 个场景上，
人就会自动忽略它们（警报疲劳）。真正该立刻处理的信号是**相比上次新增/回归的失败**；
反过来，连续红了很多次说明它一直没人管，性质是新引入的回归还是陈年老账完全不同。

分类
----
- `regressed`  **回归**：上次（或近期）通过，这次失败 —— 优先级最高，可能是刚引入的缺陷。
- `new`        新出现的失败场景：历史上没成功通过过。
- `persistent` 持续失败：历史窗口内全是失败，没有通过记录 —— 欠账，不是新的。
- `flaky`      不稳定：窗口内时红时绿 —— 可能是环境问题或竞态，别急着当产品缺陷。
- `recovered`  这次由失败转通过 —— 正向反馈，值得确认是否真的修好了。
- `unknown`    无法判定（没有可用基线），如实说明而不是硬贴标签。

三条克制（与本项目一贯口径一致）
--------------------------------
1. **没有有效基线就不做对比**。上次快照若无实质执行（`passed+failed==0`，
   典型是环境不可达导致全 SKIP），就不算"新增失败"——那等于用噪音刷注意力，
   比漏报更糟。此时全部记 `unknown` 并在 notes 说明原因。
2. **只做提示不做门禁**。 `flaky` 与 `persistent` 都不代表这次一定有缺陷，
   本模块不产生通过/不通过的判定（那是 `gate_notify` 的职责），只回答"先看哪个"。
3. **SKIP 不参与分类**。跳过本来就是环境问题的产物，把它算进胜负序列会污染 streak。

⚠️ 调用顺序（最重要的一条）
---------------------------
必须在 `run_store.insert_snapshot(本次)` **之前**取历史并调用对比。
否则历史快照里最新的一条就是本次自己，自己跟自己比，结论永远是"没有新增"。

用法::

    prev = prev_snapshot            # 上一次回归快照（dict，含 ts/results/passed/failed）
    history = list_snapshots(pid)   # 不含本次
    payload = compare(cur_results, prev=prev, history=history)
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# 分类标签（展示顺序 = 处理优先级）
STATUS_ORDER = ("regressed", "new", "persistent", "flaky", "recovered", "unknown")

STATUS_LABEL = {
    "regressed": "回归（此前通过，这次失败）",
    "new": "新出现的失败",
    "persistent": "持续失败（历史无通过记录）",
    "flaky": "不稳定（时红时绿）",
    "recovered": "已恢复（失败转通过）",
    "unknown": "无法判定（无可用基线）",
}

STATUS_DESC = {
    "regressed": "最值得先看：此前能过，这次红了，大概率是刚引入的变化。",
    "new": "历史上没有过通过记录，可能是新场景首次执行就失败，也可能是长期欠账。",
    "persistent": "历史窗口内没有通过记录，属于持续存在的问题，不是这次新引入的。",
    "flaky": "窗口内红绿交替，先怀疑环境/数据/竞态，别急着当成稳定缺陷。",
    "recovered": "这次由失败转为通过，值得确认是真的修好了还是偶然通过。",
    "unknown": "上一次执行没有留下可用的对照结果，因此不作新旧判断。",
}

_STATUS_ICON = {
    "regressed": "🔺", "new": "🆕", "persistent": "🔁",
    "flaky": "🎲", "recovered": "✅", "unknown": "❔",
}

# 参与胜负判定的结果值（其它一律不参与，避免 SKIP 污染连续计数）
_DECIDING = ("PASS", "FAIL")
_WINDOW = 6     # 回看多少次历史执行，用于 flaky / persistent 判定


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _scene_map(results: Optional[Sequence[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    """把 results 列表转成 场景名 -> 结果项；同名以最后一条为准。"""
    out: Dict[str, Dict[str, Any]] = {}
    for r in results or []:
        name = str(r.get("name") or "").strip()
        if name:
            out[name] = r
    return out


def _res(item: Optional[Dict[str, Any]]) -> Optional[str]:
    """取结果值并归一化；无该项或值为空时返回 None（区别于 '' ）。"""
    if not item:
        return None
    v = str(item.get("result") or "").strip().upper()
    return v or None


def _history_series(history: Sequence[Dict[str, Any]], name: str,
                    window: int = _WINDOW) -> List[Dict[str, Any]]:
    """某场景在历史快照里的结果序列（按时间正序，最后一项是最近一次）。"""
    seq: List[Dict[str, Any]] = []
    for snap in history[-window:]:
        item = _scene_map(snap.get("results")).get(name)
        r = _res(item)
        seq.append({
            "ts": snap.get("ts"),
            "result": r,                       # None = 那次没执行这个场景
            "detail": (item or {}).get("detail", ""),
        })
    return seq


def _tail_fail_streak(seq: Sequence[Dict[str, Any]]) -> int:
    """历史尾部连续 FAIL 的次数（遇到非 FAIL 即停；None 也算中断）。"""
    n = 0
    for row in reversed(list(seq)):
        if row.get("result") == "FAIL":
            n += 1
        else:
            break
    return n


def _fmt_ts(ts: Optional[int]) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")
    except Exception:
        return "—"


def _rank(status: str) -> int:
    try:
        return STATUS_ORDER.index(status)
    except ValueError:
        return len(STATUS_ORDER)


# --------------------------------------------------------------------------- #
# 核心对比
# --------------------------------------------------------------------------- #
def compare(cur_results: Sequence[Dict[str, Any]],
            prev: Optional[Dict[str, Any]] = None,
            history: Optional[Sequence[Dict[str, Any]]] = None,
            window: int = _WINDOW) -> Dict[str, Any]:
    """把本次结果与历史快照对比，给每个失败/转好项贴上「新旧」标签。

    cur_results: 本次 regression 的 results 列表。
    prev:        上一次快照（dict）。**不能是本次自己**。
    history:     按时间正序的历史快照列表（不含本次）。
    """
    cur_map = _scene_map(cur_results)
    history = list(history or [])
    prev_map = _scene_map((prev or {}).get("results"))

    # --- 基线有效性：决定能不能做新旧判断 ---
    baseline: Dict[str, Any] = {
        "available": False,
        "reason": "",
        "ts": (prev or {}).get("ts"),
        "ts_text": _fmt_ts((prev or {}).get("ts")),
    }
    prev_executed = int((prev or {}).get("passed") or 0) + int((prev or {}).get("failed") or 0)
    if prev is None:
        baseline["reason"] = "没有上一次执行记录（本次是该项目的第一次），无可对照"
    elif prev_executed == 0:
        baseline["reason"] = (
            "上一次没有实质执行（全部跳过，通常是环境不可达），"
            "拿它当基线会把全部失败都算成「新增」，故不作新旧判断")
    else:
        baseline["available"] = True

    items: List[Dict[str, Any]] = []
    for name, item in cur_map.items():
        result = _res(item)
        if result not in _DECIDING:
            continue                                  # SKIP 等不参与

        prev_item = prev_map.get(name)
        prev_result = _res(prev_item)
        seq = _history_series(history, name, window=window)
        window_results = [r["result"] for r in seq if r["result"] in _DECIDING]

        # 连续失败次数 = 历史尾部连续 FAIL + 本次（若本次 FAIL）
        streak = _tail_fail_streak(seq) + (1 if result == "FAIL" else 0)

        if result == "FAIL":
            has_pass = "PASS" in window_results
            has_fail = "FAIL" in window_results
            if not baseline["available"]:
                status = "unknown"
            elif prev_result == "PASS":
                # 上一次明确通过 → 最强的回归信号
                status = "regressed"
            elif prev_result == "FAIL":
                # 上一次也失败：窗口红绿交替说明是抖动，全红才是长期欠账。
                # ⚠️ 这个分支必须排在"窗口有 PASS 就判回归"之前 —— 否则
                #    flaky 永远进不来，把抖动误报成刚引入的回归（最伤信任的误判）。
                status = "flaky" if has_pass else "persistent"
            elif prev_result is None:
                # 上一次没执行这个场景，只能看窗口
                if has_pass and has_fail:
                    status = "flaky"
                elif has_pass:
                    status = "regressed"
                else:
                    status = "new"
            else:
                status = "unknown"
        else:  # PASS
            if not baseline["available"]:
                continue                              # 无基线时连"已恢复"也认不出来
            if prev_result == "FAIL" or "FAIL" in window_results:
                status = "recovered"
            else:
                continue                              # 一直通过的不用占版面

        last_pass = last_fail = None
        for row in seq:
            if row.get("result") == "PASS":
                last_pass = row.get("ts")
            elif row.get("result") == "FAIL":
                last_fail = row.get("ts")

        items.append({
            "name": name,
            "status": status,
            "result": result,
            "type": str(item.get("type") or ""),
            "streak": streak,
            "window_runs": len(window_results),
            "window_fails": sum(1 for r in window_results if r == "FAIL"),
            "last_pass_ts": last_pass,
            "last_fail_ts": last_fail,
            "last_pass_text": _fmt_ts(last_pass),
            "last_fail_text": _fmt_ts(last_fail),
            "detail": str(item.get("detail") or "")[:400],
        })

    items.sort(key=lambda x: (_rank(x["status"]), -x["streak"], x["name"]))

    counts = {k: sum(1 for i in items if i["status"] == k) for k in STATUS_ORDER}
    notes: List[str] = []
    # 基线不可用的原因由 baseline.reason 单独承载，这里不重复一遍
    # （重复会让 CLI 输出和 UI 都显得啰嗦，且读者会以为是两个不同的问题）
    if not items:
        notes.append("本次没有需要关注的失败项或转好项")
    if counts["flaky"]:
        notes.append(f"有 {counts['flaky']} 项呈红绿交替，建议先排查环境/数据稳定性，"
                     "不要直接当成产品缺陷")
    if counts["persistent"]:
        notes.append(f"有 {counts['persistent']} 项是历史一直失败（非本次新增），"
                     "是否需要处理请按优先级另行排期")

    # 一句话结论：优先报"新增/回归"
    focus = counts["regressed"] + counts["new"]
    if focus:
        headline = f"本次新增失败 {focus} 项（回归 {counts['regressed']} / 新出现 {counts['new']}）"
    elif not baseline["available"]:
        headline = "无可对照基线，未作新旧判断"
    elif counts["persistent"] or counts["flaky"]:
        headline = (f"没有新增失败，但有历史遗留 "
                    f"失败 {counts['persistent'] + counts['flaky']} 项")
    else:
        headline = "没有失败项"

    return {
        "baseline": baseline,
        "items": items,
        "counts": counts,
        "notes": notes,
        "headline": headline,
        "focus_new": focus,
        "window": window,
        "labels": STATUS_LABEL,
        "descs": STATUS_DESC,
        "compared_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def render_text(payload: Dict[str, Any], title: str = "") -> str:
    """纯文本摘要，供 CLI / 控制台文本框 / 通知复用（与 JSON 同源，避免两处文案漂移）。"""
    lines: List[str] = []
    if title:
        lines.append(title)
    p = payload or {}
    lines.append(p.get("headline") or "")
    base = p.get("baseline") or {}
    if base.get("available"):
        lines.append(f"对照基线：上一次执行 {base.get('ts_text', '—')}")
    else:
        lines.append(f"对照基线：不可用 —— {base.get('reason', '')}")

    for note in p.get("notes") or []:
        lines.append("  · " + note)

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for it in p.get("items") or []:
        groups.setdefault(it["status"], []).append(it)

    for status in STATUS_ORDER:
        rows = groups.get(status) or []
        if not rows:
            continue
        lines.append("")
        lines.append(f"{_STATUS_ICON.get(status, '')} {STATUS_LABEL.get(status, status)}"
                     f"（{len(rows)}）")
        if status != "unknown":
            lines.append("   " + STATUS_DESC.get(status, ""))
        for it in rows:
            # 一律用 .get()：这份 payload 会落盘再读回，字段可能来自旧版本或被手工构造，
            # 少一个 key 就让整段摘要变空，比少显示一行信息糟糕得多。
            extra = []
            if int(it.get("streak") or 0) > 1:
                extra.append(f"连续失败 {it['streak']} 次")
            if it.get("window_runs"):
                extra.append(f"近 {it['window_runs']} 次中失败 {it.get('window_fails', 0)}")
            if str(it.get("last_pass_text") or "—") != "—":
                extra.append(f"上次通过 {it['last_pass_text']}")
            tail = ("  [" + "；".join(extra) + "]") if extra else ""
            lines.append(f"   - {it.get('name', '')}{tail}")
            if it.get("detail"):
                lines.append(f"     {it['detail']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 落盘 / 读取
# --------------------------------------------------------------------------- #
DIFF_FILE = "diff.json"


def write_diff(pdir: Path, payload: Dict[str, Any]) -> Path:
    outdir = pdir / "artifacts"
    outdir.mkdir(parents=True, exist_ok=True)
    f = outdir / DIFF_FILE
    f.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return f


def read_diff(pdir: Path) -> Optional[Dict[str, Any]]:
    f = pdir / "artifacts" / DIFF_FILE
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def main() -> None:  # pragma: no cover - CLI 薄封装
    import argparse
    ap = argparse.ArgumentParser(description="按最近一次结果重算失败项新旧对比（不重跑）")
    ap.add_argument("project", help="项目目录，如 projects/mall-admin")
    ap.add_argument("--prev", help="上一次结果 JSON（默认用 regression.json 的上一份快照）")
    ap.add_argument("--cur", help="本次结果 JSON（默认 artifacts/regression.json）")
    args = ap.parse_args()

    pdir = Path(args.project)
    cur_f = Path(args.cur) if args.cur else pdir / "artifacts" / "regression.json"
    if not cur_f.is_file():
        raise SystemExit(f"找不到本次结果：{cur_f}")
    cur = json.loads(cur_f.read_text(encoding="utf-8"))
    prev = None
    if args.prev:
        prev = json.loads(Path(args.prev).read_text(encoding="utf-8"))
    print(render_text(compare(cur.get("results") or [], prev=prev),
                      f"{pdir.name} 失败项新旧对比"))


if __name__ == "__main__":  # pragma: no cover
    main()
