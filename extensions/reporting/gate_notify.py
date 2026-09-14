"""门禁结果摘要与失败通知（O3）。

把散落在 `projects/<id>/artifacts/` 下的三道门禁结论（核心回归 / 性能安全 / Web 冒烟）
汇总成一段人能直接读的摘要，然后复用 `notify_dingtalk` / `notify_email` 发出去。

设计要点（与项目其余模块同一套防误判口径）：

1. **未执行 ≠ 通过。** 门禁没跑过就当绿，是流水线里最危险的一种假绿。
   本模块区分三态：通过 / 未通过 / **未执行**，后两者都算「门禁未达成」。
2. **未配置 ≠ 未通过。** 项目没有 `web.yaml` 却判它红，是无谓的噪音，
   会让人习惯性忽略红色。所以「未配置」单独一档，不参与判定。
3. **摘要优先复用上游产物。** CI 的通知作业是全新 checkout，`projects/` 下没有
   产物，重算只会得到「无项目」这种废话 —— 所以支持 `--text-file` 直接发上游摘要。
4. **无凭据可跑。** `--dry-run` 只打印不发送，本地预览与单测都靠它
   （没有钉钉/邮箱 Secrets 也能验证逻辑，否则这段代码只能靠"发一次看看"来测）。

用法：
    python extensions/reporting/gate_notify.py --dry-run                 # 只看摘要
    python extensions/reporting/gate_notify.py --dry-run --fail-on-gate  # 作 CI 硬门禁
    python extensions/reporting/gate_notify.py --dry-run --out s.txt     # 摘要落盘
    python extensions/reporting/gate_notify.py --text-file s.txt         # 发送既有摘要
    python extensions/reporting/gate_notify.py --project mall-admin ...  # 只看这一个项目
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:  # 通知脚本与本模块同目录；被单测/其他目录导入时也能找到
    sys.path.insert(0, str(Path(__file__).resolve().parent))
except Exception:  # pragma: no cover
    pass  # 可忽略：纯防御（同一路径重复 insert 不会抛），失败也不影响通知逻辑

# 统一日志出口（共享实现层）。extensions/ 挂进 sys.path 后再 import。
_EXTENSIONS_DIR = str(Path(__file__).resolve().parents[1])
if _EXTENSIONS_DIR not in sys.path:
    sys.path.insert(0, _EXTENSIONS_DIR)
from common.obs import get_logger  # noqa: E402

log = get_logger("gate_notify")

import notify_dingtalk  # type: ignore
import notify_email  # type: ignore

# (门禁 key, 中文名, 产物文件名, 判"是否已配置"的声明文件；None 表示看 project.yaml)
GATES: Tuple[Tuple[str, str, str, Optional[str]], ...] = (
    ("regression", "核心业务回归", "regression.json", "regression.yaml"),
    ("perf_security", "性能与安全", "perf_security.json", None),
    ("web", "Web UI 冒烟", "web.json", "web.yaml"),
)

STATUS_LABEL = {
    "pass": "✅ 通过",
    "fail": "❌ 未通过",
    "not_run": "⏭ 未执行",
    "not_configured": "— 未配置",
}


# --------------------------------------------------------------------------- #
# 采集
# --------------------------------------------------------------------------- #
def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    """读产物 JSON；缺失或损坏一律返回 None（= 未执行，交给上层判定）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _load_project_yaml(path: Path) -> Dict[str, Any]:
    """读 project.yaml；取不到库就退回极简解析（只取展示用的 name）。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        name = ""
        for line in text.splitlines():
            if line.startswith("name:"):
                name = line.split(":", 1)[1].strip().strip("'\"")
                break
        return {"name": name}


def _is_configured(pdir: Path, decl_file: Optional[str], key: str) -> bool:
    """门禁是否已在这个项目里声明。未声明 → 不参与判定（避免无谓红）。"""
    if decl_file:
        return (pdir / decl_file).is_file()
    # perf_security 没有独立声明文件：project.yaml 里显式写了，或能靠 regression.yaml 推导目标
    if (pdir / "regression.yaml").is_file():
        return True
    try:
        text = (pdir / "project.yaml").read_text(encoding="utf-8")
    except OSError:
        return False
    return any(line.startswith(key + ":") for line in text.splitlines())


def _gate_record(pdir: Path, label: str, art: str) -> Dict[str, Any]:
    data = _read_json(pdir / "artifacts" / art)
    if data is None:
        return {"label": label, "status": "not_run", "summary": ""}
    ok = bool(data.get("all_pass"))
    return {
        "label": label,
        "status": "pass" if ok else "fail",
        "summary": str(data.get("summary") or ""),
    }


def _verdict(gates: Dict[str, Dict[str, Any]]) -> Tuple[str, str]:
    """项目级结论：未通过 > 未执行 > 通过；全未配置则整体记未执行。"""
    live = [g["status"] for g in gates.values() if g["status"] != "not_configured"]
    if not live:
        return ("not_run", "没有已配置的门禁")
    if "fail" in live:
        return ("fail", "存在未通过的门禁")
    if "not_run" in live:
        return ("not_run", "有门禁尚未执行（未执行 ≠ 通过）")
    return ("pass", "")


def collect_gates(projects_dir: Path,
                  only: Optional[List[str]] = None,
                  include_disabled: bool = False) -> List[Dict[str, Any]]:
    """扫描项目登记目录，逐项目汇总三道门禁的状态。

    `only` 用于**只看本次参与门禁的项目**：CI 跑的是 `STA_PROJECT_ID` 指定的那一个，
    若把仓库里其余从未跑过的项目也算进来，它们全是「未执行」→ 门禁永远红。

    `include_disabled`：默认**跳过已停用的项目**（目录下有 `.disabled` 标记）——
    与 `project_manager.load_projects()` / 跨项目看板 / 控制台项目页保持一致。
    不一致的后果很隐蔽：停用项目如果还留着上次的失败产物，会持续把门禁拖红，
    而界面上又看不到它（因为项目页同样把它隐藏了）→ 变成无法解释的"幽灵红"。
    跳过的数量会写进摘要（`disabled`），**不静默**。
    """
    root = Path(projects_dir)
    rows: List[Dict[str, Any]] = []
    if not root.is_dir():
        return rows
    disabled: List[str] = []
    for pdir in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name):
        if not (pdir / "project.yaml").is_file():
            continue
        if not include_disabled and (pdir / ".disabled").is_file():
            disabled.append(pdir.name)
            continue
        meta = _load_project_yaml(pdir / "project.yaml")
        pid = str(meta.get("project_id") or pdir.name)
        if only and not (pdir.name in only or pid in only):
            continue
        gates: Dict[str, Dict[str, Any]] = {}
        for key, label, art, decl in GATES:
            if not _is_configured(pdir, decl, key):
                gates[key] = {"label": label, "status": "not_configured", "summary": ""}
            else:
                gates[key] = _gate_record(pdir, label, art)
        vcode, vreason = _verdict(gates)
        rows.append({
            "pid": pid,
            "name": str(meta.get("name") or pdir.name),
            "gates": gates,
            "verdict": vcode,
            "verdict_reason": vreason,
        })
    return rows


def disabled_projects(projects_dir: Path) -> List[str]:
    """列出被停用（目录下有 `.disabled`）的项目名，用于在摘要里如实交代跳过了谁。"""
    root = Path(projects_dir)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and (p / "project.yaml").is_file()
                  and (p / ".disabled").is_file())


def summarize(rows: List[Dict[str, Any]], disabled: int = 0) -> Dict[str, int]:
    out = {"projects": len(rows), "pass": 0, "fail": 0, "not_run": 0,
           "disabled": disabled, "all_pass": 0}
    for r in rows:
        out[r["verdict"]] = out.get(r["verdict"], 0) + 1
    # 一个项目都没有 → 谈不上"通过"（多半是流水线跑错了目录）
    out["all_pass"] = bool(rows) and out["pass"] == len(rows)
    return out


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def render_text(rows: List[Dict[str, Any]], title: str = "",
                disabled: Optional[List[str]] = None) -> str:
    """渲染成人类可读摘要（钉钉/邮件正文直接用它）。"""
    disabled = list(disabled or [])
    summ = summarize(rows, disabled=len(disabled))
    lines: List[str] = []
    if title:
        lines.append(title)
    if not rows:
        lines.append("未发现任何已接入项目（projects/ 下没有 project.yaml）。")
        lines.append("门禁未执行 ≠ 通过，请确认被测项目是否已登记、--project 是否写对。")
        if disabled:
            lines.append(f"（另有 {len(disabled)} 个已停用项目未计入：{'、'.join(disabled)}）")
        return "\n".join(lines)

    lines.append(
        f"项目 {summ['projects']} 个 · 通过 {summ['pass']} · "
        f"未通过 {summ['fail']} · 未执行 {summ['not_run']}"
    )
    if disabled:
        # 如实交代跳过了谁：静默隐藏会让人以为"全都算过了"
        lines.append(f"（已停用、未计入：{'、'.join(disabled)}）")
    lines.append("")
    for r in rows:
        mark = {"pass": "✅", "fail": "❌", "not_run": "⏭"}.get(r["verdict"], "?")
        head = f"{mark} {r['pid']} {r['name']}".rstrip()
        if r["verdict_reason"]:
            head += f"（{r['verdict_reason']}）"
        lines.append(head)
        for key, _label, _art, _decl in GATES:
            g = r["gates"].get(key)
            if not g:
                continue
            line = f"    {g['label']}：{STATUS_LABEL.get(g['status'], g['status'])}"
            if g["status"] == "fail" and g["summary"]:
                line += f" —— {g['summary']}"
            lines.append(line)
        lines.append("")

    if summ["all_pass"]:
        lines.append("结论：全部门禁通过 ✅")
    else:
        lines.append("结论：门禁未达成 ❌（未通过 或 尚有门禁未执行）")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# 发送
# --------------------------------------------------------------------------- #
def _send(text: str, dry_run: bool, subject: str = "软件测试智能体 · 门禁结果") -> None:
    if dry_run:
        print("[dry-run] 以下内容将发送（未实际发送）：")
        print(text)
        return
    notify_dingtalk.send(text, os.getenv("DINGTALK_WEBHOOK", ""), os.getenv("DINGTALK_SECRET", ""))
    notify_email.send(
        subject, text,
        os.getenv("MAIL_USERNAME", ""), os.getenv("MAIL_PASSWORD", ""), os.getenv("MAIL_TO", ""),
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="门禁结果摘要与通知")
    ap.add_argument("--projects-dir", default="projects", help="项目登记目录（默认 projects）")
    ap.add_argument("--project", action="append", default=[], metavar="PID",
                    help="只看指定项目（可重复）。CI 只跑了一个项目时必须加，"
                         "否则其余从未跑过的项目会全记「未执行」→ 门禁永远红")
    ap.add_argument("--include-disabled", action="store_true",
                    help="把已停用的项目也算进来（默认跳过，与看板/控制台一致）")
    ap.add_argument("--dry-run", action="store_true", help="只打印摘要，不发送（无凭据/本地预览用）")
    ap.add_argument("--out", default="", help="把摘要写入指定文件（供下游作业复用）")
    ap.add_argument("--text-file", default="", help="直接发送该文件内容，不重算（CI 通知作业用）")
    ap.add_argument("--fail-on-gate", action="store_true", help="存在未达成门禁时退出码 1（CI 硬门禁）")
    ap.add_argument("--title", default="软件测试智能体 · 门禁结果", help="摘要首行标题")
    args = ap.parse_args(argv)

    if args.text_file:
        # 上游已经算好结论：直接发，避免在新 checkout 上重算出「无项目」的废话
        try:
            text = Path(args.text_file).read_text(encoding="utf-8")
        except OSError as e:
            log.error("读取摘要文件失败：%s", e)
            return 1
        _send(text, args.dry_run, subject="软件测试智能体 · CI 门禁结果")
        return 0

    pdir = Path(args.projects_dir)
    rows = collect_gates(pdir, only=args.project or None,
                         include_disabled=args.include_disabled)
    disabled = [] if args.include_disabled else disabled_projects(pdir)
    summ = summarize(rows, disabled=len(disabled))
    text = render_text(rows, title=args.title, disabled=disabled)

    if args.out:
        try:
            Path(args.out).write_text(text, encoding="utf-8")
        except OSError as e:
            log.error("写入摘要文件失败：%s", e)

    _send(text, args.dry_run, subject=args.title)

    if args.fail_on_gate and not summ["all_pass"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
