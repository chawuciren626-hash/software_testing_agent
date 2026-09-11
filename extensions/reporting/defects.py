"""失败项 → **缺陷草稿**（把"流水线上的红"变成"能提交给开发的缺陷单"）。

为什么需要它
------------
到此为止，流水线的输出止于"哪些场景红了"。但红只是信号，最终要有人**提交缺陷、
跟进、验证关闭**——这一步一直是人工的，而且每次都要从报告里手抄接口、实际值、
期望值、截图路径，抄错或漏抄很常见。本模块把这一步自动化到"复制即提交"。

它**不**做什么（重要）
----------------------
- **不自动创建 Issue / 提单**。自动建单会产生大量噪音与误报，
  且创建容易、删除难；这里只产出草稿，**由人确认后再提交**。
- **严重程度只是建议**。规则推断的 S1~S4 一律标注为"建议"，
  真正的定级要人根据业务影响判断（同一现象在支付链路和后台配置页上级别完全不同）。
- **不把环境问题当缺陷**。连接失败 / 服务不可达 / 基线未通过导致的 SKIP，
  单列到「环境问题」，**绝不混入缺陷清单**——把环境问题报成缺陷，
  是比漏报更伤信任的事（开发查半天发现是环境没起）。
- **不把配置问题当缺陷**。Web 场景写了非法定位器之类，单列「配置问题」。

严重程度建议规则（可查、可改，不是黑盒）
----------------------------------------
====  ===========================================  ======
建议   触发条件                                     理由
====  ===========================================  ======
S1    安全检查 FAIL（未授权访问 / 注入 / 泄露）      安全漏洞影响面不可控
S2    核心业务回归 FAIL、Web UI 场景 FAIL           主流程不可用
S3    性能未达阈值、Web 场景抖动（flaky）           影响体验但不阻断
S4    其余失败（含断言外的异常）                    需人工复核后定级
====  ===========================================  ======

用法::

    python extensions/reporting/defects.py <项目目录> [--json]
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFECTS_MD = "defects.md"
DEFECTS_JSON = "defects.json"

_SEV_ORDER = ("S1", "S2", "S3", "S4")
_SEV_LABEL = {"S1": "致命", "S2": "严重", "S3": "一般", "S4": "轻微"}


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def _sev_rank(s: str) -> int:
    return _SEV_ORDER.index(s) if s in _SEV_ORDER else len(_SEV_ORDER)


def _get(d: Any, key: str, default: Any = None) -> Any:
    return (d or {}).get(key, default) if isinstance(d, dict) else default


def _from_regression(reg: Optional[Dict[str, Any]], base_url: str) -> List[Dict[str, Any]]:
    """核心回归：只有 FAIL 才是缺陷；SKIP 归环境问题。"""
    out: List[Dict[str, Any]] = []
    for r in _get(reg, "results", []) or []:
        result = str(_get(r, "result", "")).upper()
        if result != "FAIL":
            continue
        detail = str(_get(r, "detail", "") or "")
        # 5xx 与超时比普通断言失败更严重一点，但仍属 S2 —— 定级权在人
        sev = "S2"
        why = "核心业务场景失败（主流程不可用）"
        if "timeout" in detail.lower() or "超时" in detail:
            why = "核心业务场景失败（超时，可能涉及稳定性）"
        out.append({
            "source": "核心回归",
            "title": f"[回归] {_get(r, 'name', '(未命名场景)')}",
            "severity": sev,
            "severity_reason": why,
            "steps": [f"{_get(r, 'method', '')} {_get(r, 'url', '')}".strip(),
                      f"环境：{base_url or '(未配置)'}"],
            "expected": str(_get(r, "expect", "") or ""),
            "actual": f"HTTP {_get(r, 'status_code', '?')}",
            "evidence": detail,
        })
    return out


def _from_perf_security(ps: Optional[Dict[str, Any]], base_url: str) -> List[Dict[str, Any]]:
    """性能与安全：安全 FAIL = S1；性能未达阈值 = S3；WARN 不算缺陷。"""
    out: List[Dict[str, Any]] = []
    if not ps:
        return out
    sec = _get(ps, "security", {}) or {}
    for c in _get(sec, "checks", []) or []:
        if str(_get(c, "status", "")).upper() != "FAIL":
            continue
        out.append({
            "source": "性能与安全",
            "title": f"[安全] {_get(c, 'name', '(未命名检查)')}",
            "severity": "S1",
            "severity_reason": "安全检查未通过（影响面不可控，建议优先处理）",
            "steps": [f"环境：{base_url or '(未配置)'}"],
            "expected": "该检查项应通过",
            "actual": str(_get(c, "detail", "") or ""),
            "evidence": str(_get(c, "evidence", "") or ""),
        })
    for t in _get(_get(ps, "perf", {}), "targets", []) or []:
        fails = _get(t, "threshold_fails", []) or []
        if not fails:
            continue
        out.append({
            "source": "性能与安全",
            "title": f"[性能] {_get(t, 'name', '(未命名目标)')} 未达阈值",
            "severity": "S3",
            "severity_reason": "性能指标未达阈值（不阻断功能，影响体验/容量）",
            "steps": [f"{_get(t, 'method', '')} {_get(t, 'path', '')}".strip(),
                      f"并发 {_get(t, 'concurrency', '?')} × {_get(t, 'requests', '?')} 次",
                      f"环境：{base_url or '(未配置)'}"],
            "expected": "；".join(str(f) for f in fails),
            "actual": (f"P95 {_get(t, 'p95_ms', '?')}ms · P99 {_get(t, 'p99_ms', '?')}ms"
                       f" · 错误率 {float(_get(t, 'error_rate', 0) or 0) * 100:.2f}%"
                       f" · 吞吐 {_get(t, 'rps', '?')} rps"),
            "evidence": "",
        })
    return out


def _from_web(web: Optional[Dict[str, Any]], base_url: str) -> List[Dict[str, Any]]:
    """Web UI：FAIL = S2，抖动 = S3；SKIP/配置问题不算缺陷。"""
    out: List[Dict[str, Any]] = []
    if not web:
        return out
    for s in _get(web, "scenarios", []) or []:
        result = str(_get(s, "result", "")).upper()
        if result != "FAIL":
            continue
        flaky = bool(_get(s, "flaky", False))
        fs = _get(s, "failed_step", {}) or {}
        step_txt = ""
        if fs:
            step_txt = f"第{_get(fs, 'index', '?')}步 {_get(fs, 'action', '')} → {_get(fs, 'target', '')}"
        shots = _get(s, "screenshots", []) or []
        out.append({
            "source": "Web UI",
            "title": f"[Web] {_get(s, 'name', '(未命名场景)')}",
            "severity": "S3" if flaky else "S2",
            "severity_reason": ("Web 场景失败（重试后通过，疑似抖动/不稳定）" if flaky
                                else "Web 关键路径场景失败（用户可见流程不可用）"),
            "steps": [x for x in [step_txt, f"环境：{_get(web, 'base_url', base_url) or '(未配置)'}",
                                  f"浏览器：{_get(web, 'browser', '?')}"] if x],
            "expected": str(_get(s, "name", "") or ""),
            "actual": str(_get(s, "reason", "") or ""),
            "evidence": "、".join(str(p).replace("\\", "/").split("/")[-1] for p in shots),
            "flaky": flaky,
        })
    return out


def build_defects(reg: Optional[Dict[str, Any]] = None,
                  ps: Optional[Dict[str, Any]] = None,
                  web: Optional[Dict[str, Any]] = None,
                  pid: str = "", base_url: str = "") -> Dict[str, Any]:
    """汇总三道门禁的失败项，产出缺陷草稿 + 环境问题 + 配置问题。

    返回：::

        {"items": [...], "env_issues": [...], "config_issues": [...],
         "counts": {"total": n, "by_severity": {...}}, "generated_at": "..."}
    """
    items = (_from_regression(reg, base_url) + _from_perf_security(ps, base_url)
             + _from_web(web, base_url))
    # 按严重程度排序，同类保持原顺序（稳定的输出便于 diff）
    items.sort(key=lambda d: _sev_rank(d.get("severity", "S4")))
    for i, d in enumerate(items, 1):
        d["id"] = f"DEF-{i:03d}"

    env_issues: List[str] = []
    for r in _get(reg, "results", []) or []:
        if str(_get(r, "result", "")).upper() == "SKIP":
            env_issues.append(f"[回归] {_get(r, 'name', '')}：{_get(r, 'detail', '') or '跳过'}")
    for s in _get(web, "scenarios", []) or []:
        if str(_get(s, "result", "")).upper() == "SKIP":
            env_issues.append(f"[Web] {_get(s, 'name', '')}：{_get(s, 'reason', '') or '跳过'}")
    baseline = _get(ps, "baseline", {}) or {}
    if ps and baseline.get("ok") is False:
        env_issues.append(f"[性能与安全] 基线未通过：{baseline.get('reason', '')}"
                          "（此状态下的安全结论不可信，已按未通过处理）")

    config_issues = [str(x) for x in (_get(web, "config_issues", []) or [])]

    by_sev = {s: sum(1 for d in items if d.get("severity") == s) for s in _SEV_ORDER}
    return {"pid": pid, "base_url": base_url, "items": items,
            "env_issues": env_issues, "config_issues": config_issues,
            "counts": {"total": len(items), "by_severity": by_sev,
                       "env": len(env_issues), "config": len(config_issues)},
            "generated_at": _now()}


def render_markdown(payload: Dict[str, Any]) -> str:
    """渲染成可直接粘进缺陷系统的 Markdown。"""
    items = payload.get("items") or []
    counts = payload.get("counts") or {}
    L: List[str] = [f"# 待提交缺陷（草稿）· {payload.get('pid', '')}",
                    "",
                    f"- 生成时间：{payload.get('generated_at', '')}",
                    f"- 测试环境：`{payload.get('base_url') or '(未配置)'}`",
                    f"- 缺陷数：{counts.get('total', 0)}"
                    + "".join(f" · {s} {v}" for s, v in
                              (counts.get("by_severity") or {}).items() if v),
                    "",
                    "> 严重程度为**规则推断的建议值**，提交前请按业务影响人工复核。",
                    "> 本文件由流水线生成，**不会自动提单**。",
                    ""]
    if not items:
        L.append("## 结论")
        L.append("")
        L.append("本次执行**没有发现需要提交的产品缺陷**。")
    else:
        L.append("## 缺陷清单")
        L.append("")
        L.append("| 编号 | 建议级别 | 来源 | 标题 | 实际 | 期望 |")
        L.append("|---|---|---|---|---|---|")
        for d in items:
            L.append(f"| {d.get('id', '')} | {d.get('severity', '')}"
                     f"（{_SEV_LABEL.get(d.get('severity', ''), '')}） | {d.get('source', '')} "
                     f"| {_md_cell(d.get('title', ''))} | {_md_cell(d.get('actual', ''))} "
                     f"| {_md_cell(d.get('expected', ''))} |")
        L.append("")
        for d in items:
            L.append(f"### {d.get('id', '')} · {d.get('title', '')}")
            L.append("")
            L.append(f"- 建议级别：**{d.get('severity', '')}**"
                     f"（{_SEV_LABEL.get(d.get('severity', ''), '')}）—— {d.get('severity_reason', '')}")
            steps = d.get("steps") or []
            if steps:
                L.append("- 复现步骤：")
                L += [f"  {i}. {s}" for i, s in enumerate(steps, 1)]
            L.append(f"- 实际结果：{d.get('actual', '')}")
            L.append(f"- 期望结果：{d.get('expected', '') or '（见场景声明）'}")
            if d.get("evidence"):
                L.append(f"- 证据：{d.get('evidence')}")
            L.append("")

    if payload.get("env_issues"):
        L.append("## 环境问题（**不是缺陷**，别提单）")
        L.append("")
        L += [f"- {x}" for x in payload["env_issues"]]
        L.append("")
    if payload.get("config_issues"):
        L.append("## 配置问题（**不是缺陷**，改配置即可）")
        L.append("")
        L += [f"- {x}" for x in payload["config_issues"]]
        L.append("")
    return "\n".join(L).rstrip() + "\n"


def _md_cell(s: str) -> str:
    """表格单元格：去掉换行与竖线，避免把 Markdown 表格撑坏。"""
    return str(s or "—").replace("|", "\\|").replace("\n", " ")


def write_defects(pdir: Path, payload: Dict[str, Any]) -> Path:
    """落盘 `artifacts/defects.md`（人读）+ `artifacts/defects.json`（程序读）。"""
    art = Path(pdir) / "artifacts"
    art.mkdir(parents=True, exist_ok=True)
    (art / DEFECTS_JSON).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    f = art / DEFECTS_MD
    f.write_text(render_markdown(payload), encoding="utf-8")
    return f


def read_defects(pdir: Path) -> Optional[Dict[str, Any]]:
    f = Path(pdir) / "artifacts" / DEFECTS_JSON
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def main() -> None:
    """独立使用：`python defects.py projects/<id>`（读该项目的三份门禁产物）。"""
    import argparse

    ap = argparse.ArgumentParser(description="把失败的门禁项整理成可提交的缺陷草稿")
    ap.add_argument("project_dir", help="项目目录（projects/<id>）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非 Markdown")
    args = ap.parse_args()

    pdir = Path(args.project_dir)
    art = pdir / "artifacts"

    def _load(name: str) -> Optional[Dict[str, Any]]:
        f = art / name
        if not f.is_file():
            return None
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            return None

    reg = _load("regression.json")
    ps = _load("perf_security.json")
    web = _load("web.json")
    # 环境地址在三份产物里都可能有，取第一个非空值
    base_url = ""
    for src in (ps, web, reg):
        base_url = str((src or {}).get("base_url") or "")
        if base_url:
            break
    payload = build_defects(reg, ps, web, pid=pdir.name, base_url=base_url)
    print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json
          else render_markdown(payload))


if __name__ == "__main__":
    main()
