"""S1「AI 探索测试」接入契约 —— **零第三方依赖**的唯一实现层。

为什么单独一个文件（而不是写进 `run_agentic.py`）：
CI 的**产品单测（硬门禁）**只装 `requirements-dev.txt`（不含 langgraph/langchain），
而探索入口天然要 import 重依赖。把"契约 / 降级判定 / 渲染"抽成零依赖模块后，
这部分就能在硬门禁里被**直接测试**——否则整块只能 `importorskip` 跳过 = 写完没人验。
（同一手法见 `src/agentic_explorer/orchestration/guardrails.py` 与序 5。）

设计口径（依据 `docs/HARNESS_ARCHITECTURE_REVIEW.md` §3.3 / §7 序 6）：

- **探索是"可降级阶段"，不是门禁**：它产出的是**发现（缺陷候选）**，不产出 pass/fail。
  降级 = 本轮不执行 LLM 探索，回落到既有的**确定性链路**（回归 / 性能安全 / Web），
  并且**必须出声**把原因说清楚（"静默跳过 = 悄悄放松门禁"，这是本项目的铁律）。
- **契约以落盘 JSON 为准**：`artifacts/agentic.json` 是 `project_manager` 消费的唯一来源；
  进程退出码只表示"契约有没有写出来"，不承担判定语义（避免两套口径）。
- **算不出来就不猜**：拿不到 B 世界的结构化结果时，写 `degraded` + 原因，绝不写"成功"。
"""
from __future__ import annotations

import json
import os
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# 契约版本：下游（报告 / 控制台 / 看板）可据此做兼容判断。
# 变更字段语义时**必须**升版本，否则老消费方会静默读错。
SCHEMA = "agentic/1"

# 产物文件名（唯一约定处，禁止各处再写字面量）
RESULT_FILE = "agentic.json"
RUN_DIR = "agentic_run"          # B 世界产物（report_* / action_tape.jsonl）落地目录
LOG_FILE = "agentic.log"         # 探索子进程的完整 stdout/stderr


class Status(str, Enum):
    """阶段状态：**只有两种**。

    刻意不引入 "failed" —— 探索不是门禁，不存在"探索失败=流水线失败"。
    跑不动就是 `degraded`（降级），区别只在**原因**。
    """
    OK = "ok"
    DEGRADED = "degraded"


class DegradeReason(str, Enum):
    """降级原因枚举（验收判据：无 key / 超时 / 异常任一情况下降级且**原因出声**）。"""
    NO_LLM_KEY = "no_llm_key"          # 没有任何可用的 LLM 凭据
    NOT_CONFIGURED = "not_configured"  # 缺 mission 配置 / 读不出任务
    TIMEOUT = "timeout"                # 超出墙钟上限
    ERROR = "error"                    # 其它运行期异常


# 人类可读标签：报告与日志共用，避免两处各写一份措辞（口径唯一）。
DEGRADE_LABEL: Dict[str, str] = {
    DegradeReason.NO_LLM_KEY.value: "缺少 LLM 凭据（未配置 ANTHROPIC_API_KEY / GOOGLE_API_KEY 等）",
    DegradeReason.NOT_CONFIGURED.value: "未配置探索任务（缺少 mission.yaml）",
    DegradeReason.TIMEOUT.value: "执行超时",
    DegradeReason.ERROR.value: "运行期异常",
}

# 降级后"谁顶着"：确定性链路。写成枚举值而不是自由文本，
# 是为了让报告/看板能一眼分辨"这轮结论来自确定性执行，不是 AI 探索"。
DEGRADED_TO_DETERMINISTIC = "deterministic"

_STATUS_LABEL = {
    Status.OK.value: "✅ 已执行",
    Status.DEGRADED.value: "⚠️ 已降级（未执行探索）",
}


# --------------------------------------------------------------------------- #
# 纯判定：什么时候必须降级（可单测、不看环境）
# --------------------------------------------------------------------------- #
def decide_degrade(*, provider: Optional[str],
                   mission_path: Optional[Path] = None) -> Optional[Tuple[DegradeReason, str]]:
    """在"动手跑 B 世界之前"判断要不要降级。

    返回 ``None`` 表示"可以执行"；返回 ``(reason, message)`` 表示"必须降级"。
    顺序刻意是 **任务配置 → LLM 凭据**：

    - 任务没配好是**使用者自己能修**的问题（补 mission.yaml 即可），
      且与"有没有 key"无关——先报它更可操作。
    - 两者都缺时也报任务，因为它同时提示了"该往哪里放任务文件"。
    """
    if mission_path is None:
        return (DegradeReason.NOT_CONFIGURED,
                "未提供探索任务（mission）。请在项目目录下创建 mission.yaml，"
                "或用 --mission 指定任务文件。")
    mp = Path(mission_path)
    if not mp.is_file():
        return (DegradeReason.NOT_CONFIGURED,
                f"探索任务文件不存在：{mp}")

    if not provider or str(provider).strip().lower() in ("", "unknown", "none"):
        return (DegradeReason.NO_LLM_KEY,
                "未检测到可用的 LLM 凭据（判据与基座同源：agentic_explorer.utils.llm）。"
                "请配置 ANTHROPIC_API_KEY 或 GOOGLE_API_KEY，或设置 LLM_PROVIDER。")
    return None


# --------------------------------------------------------------------------- #
# 构造契约负载
# --------------------------------------------------------------------------- #
def _base(status: Status, *, provider: str, elapsed_s: float) -> Dict[str, Any]:
    return {
        "schema": SCHEMA,
        "status": status.value,
        "provider": str(provider or "unknown"),
        "elapsed_s": round(float(elapsed_s or 0.0), 3),
        "degraded_to": None,
        "reason": None,
        "message": "",
        "thread_id": None,
        "end_reason": None,
        "goal_verdict": None,
        "reasons": [],
        "turns": 0,
        "tokens": 0,
        "actions": 0,
        "bugs": [],
        "bug_count": 0,
        "report_dir": None,
        "action_tape": None,
        "log": None,
    }


def build_degraded(reason: DegradeReason, message: str, *,
                   provider: str = "unknown",
                   degraded_to: str = DEGRADED_TO_DETERMINISTIC,
                   elapsed_s: float = 0.0) -> Dict[str, Any]:
    """构造"降级"负载。**reason 必填**——没有原因的降级就是静默跳过。"""
    if not isinstance(reason, DegradeReason):
        # 防御：传字符串也认，但认不出来就归到 ERROR，绝不悄悄放过。
        try:
            reason = DegradeReason(str(reason))
        except ValueError:
            reason = DegradeReason.ERROR
    payload = _base(Status.DEGRADED, provider=provider, elapsed_s=elapsed_s)
    payload["reason"] = reason.value
    payload["degraded_to"] = degraded_to
    payload["message"] = str(message or DEGRADE_LABEL.get(reason.value, reason.value))
    return payload


def build_ok(*, provider: str,
             thread_id: Optional[str] = None,
             end_reason: Optional[str] = None,
             goal_verdict: Optional[str] = None,
             reasons: Iterable[Any] = (),
             turns: int = 0,
             tokens: int = 0,
             actions: int = 0,
             bugs: Iterable[Any] = (),
             bug_count: Optional[int] = None,
             report_dir: Optional[str] = None,
             action_tape: Optional[str] = None,
             log: Optional[str] = None,
             elapsed_s: float = 0.0) -> Dict[str, Any]:
    """构造"已执行"负载（字段来自 B 世界 `report_<tid>/result.json` + action tape）。

    ``bug_count`` 可显式给：基座只报计数而没给原文时，计数**不该**被原文条数覆盖
    （否则"发现了 3 个"会被写成"1 个"，这是把事实改小）。
    """
    payload = _base(Status.OK, provider=provider, elapsed_s=elapsed_s)
    # 缺陷候选统一转成短字符串：B 世界给的是自由文本，这里不做结构化推断
    # （避免"从标题反解字段"那类不可靠做法），结构化留给后续人工/缺陷链。
    items = [str(b).strip()[:500] for b in (bugs or []) if str(b).strip()]
    payload.update(
        thread_id=thread_id,
        end_reason=end_reason,
        goal_verdict=goal_verdict,
        reasons=[str(r) for r in (reasons or [])],
        turns=int(turns or 0),
        tokens=int(tokens or 0),
        actions=int(actions or 0),
        bugs=items,
        report_dir=report_dir,
        action_tape=action_tape,
        log=log,
    )
    payload["bug_count"] = int(bug_count) if bug_count is not None else len(items)
    return payload


def normalize_end_reason(value: Any) -> Optional[str]:
    """把 B 世界的 `end_reason` 归一到契约口径；认不出来就 ``None``（不猜）。"""
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    allowed = {"completed", "blocked", "max-turns", "budget-exhausted", "error"}
    return v if v in allowed else None


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #
def write_result(path: Any, payload: Dict[str, Any]) -> Path:
    """原子性写契约文件：先写临时文件再替换，避免下游读到半截 JSON。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return p


def load_result(path: Any) -> Optional[Dict[str, Any]]:
    """读契约文件；不存在 / 坏 JSON / 非对象 → ``None``（调用方据此走兜底）。"""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def is_ok(payload: Optional[Dict[str, Any]]) -> bool:
    """只有显式写 ``status == "ok"`` 才算"探索真的跑了" —— 缺字段一律**不算**。"""
    return bool(payload) and str((payload or {}).get("status", "")) == Status.OK.value


# --------------------------------------------------------------------------- #
# 呈现（双通道：stdout 结果 / 报告 Markdown）
# --------------------------------------------------------------------------- #
def render_summary_line(payload: Dict[str, Any]) -> str:
    """一行结论，给 stdout（结果呈现通道，可被管道消费）。"""
    status = str(payload.get("status", ""))
    if is_ok(payload):
        return (f"[AI 探索] {_STATUS_LABEL.get(status, status)}"
                f"：{payload.get('actions', 0)} 条动作 / "
                f"{payload.get('bug_count', 0)} 个发现"
                f"（结束原因 {payload.get('end_reason') or '未记录'}）")
    reason = str(payload.get("reason") or "未知")
    label = DEGRADE_LABEL.get(reason, reason)
    return f"[AI 探索] {_STATUS_LABEL.get(status, status)}：{label}"


def render_degrade_warning(payload: Dict[str, Any], *, where: str = "AI 探索") -> str:
    """降级告警句（给日志通道）——**降级必须出声**，这是验收判据本身。"""
    reason = str(payload.get("reason") or "未知")
    label = DEGRADE_LABEL.get(reason, reason)
    msg = str(payload.get("message") or "").strip()
    tail = f"｜{msg}" if msg and msg != label else ""
    return (f"[{where}] ⚠ 未执行探索（降级）：{label}{tail}｜"
            f"本轮结论以确定性链路（回归/性能安全/Web）为准，"
            f"不得把本阶段当作已通过。")


def render_markdown(payload: Dict[str, Any]) -> str:
    """把契约负载渲染成可放进报告的 Markdown 段。"""
    ok = is_ok(payload)
    lines: List[str] = ["## AI 探索测试（可降级阶段）", ""]
    lines.append(f"- **状态**：{_STATUS_LABEL.get(str(payload.get('status')), payload.get('status'))}")
    lines.append(f"- **LLM 提供方**：`{payload.get('provider')}`")
    lines.append(f"- **耗时**：{payload.get('elapsed_s')} s")
    if ok:
        lines += [
            f"- **任务**：`{payload.get('thread_id')}`",
            f"- **结束原因（枚举）**：`{payload.get('end_reason') or '未记录'}`",
            f"- **目标判定**：`{payload.get('goal_verdict') or 'unknown'}`",
            f"- **轮次 / token**：{payload.get('turns')} 轮 / {payload.get('tokens')} tokens",
            f"- **动作记录 / 发现**：{payload.get('actions')} 条 / {payload.get('bug_count')} 个",
        ]
        reasons = payload.get("reasons") or []
        if reasons:
            lines.append("- **判定依据**：")
            lines.extend(f"  - {r}" for r in reasons)
        bugs = payload.get("bugs") or []
        if bugs:
            lines.append("")
            lines.append("**探索发现（原文，未经结构化/定级）**：")
            lines.extend(f"- {b}" for b in bugs)
        for key, label in (("report_dir", "报告目录"), ("action_tape", "动作磁带")):
            if payload.get(key):
                lines.append(f"- **{label}**：`{payload[key]}`")
    else:
        reason = str(payload.get("reason") or "未知")
        lines += [
            f"- **降级原因（枚举）**：`{reason}` — {DEGRADE_LABEL.get(reason, reason)}",
            f"- **说明**：{payload.get('message') or ''}",
            "",
            "> 本轮**未执行** AI 探索，结论回落至确定性链路。"
            "「未执行」不等于「通过」——本段如实留痕，避免把降级误读成绿灯。",
        ]
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 供 project_manager 复用的路径约定（避免两处各拼一次路径）
# --------------------------------------------------------------------------- #
def result_path(project_dir: Any) -> Path:
    return Path(project_dir) / "artifacts" / RESULT_FILE


def run_dir(project_dir: Any) -> Path:
    return Path(project_dir) / "artifacts" / RUN_DIR


def log_path(project_dir: Any) -> Path:
    return Path(project_dir) / "artifacts" / LOG_FILE


def default_timeout(env: Optional[Dict[str, str]] = None) -> float:
    """阶段墙钟上限（秒）。

    默认**有限**（1800s）而不是"不限"：序 5 的结论同样适用于这里——
    没有上限的探索等于把成本控制权交给模型的判断力。0 表示显式不限。
    """
    e = os.environ if env is None else env
    raw = str(e.get("STA_EXPLORE_TIMEOUT", "")).strip()
    if not raw:
        return 1800.0
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 1800.0          # 写错就退回有限默认，绝不变成"不限"
    return v if v >= 0 else 1800.0


def summarize_for_meta(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """给 run_meta 的紧凑摘要（**未执行也要记**，否则报告看不出少了一环）。"""
    if not payload:
        return {"executed": False, "status": None, "reason": "no_artifact",
                "summary": "未产出探索契约文件"}
    return {
        "executed": is_ok(payload),
        "status": payload.get("status"),
        "reason": payload.get("reason"),
        "provider": payload.get("provider"),
        "bug_count": payload.get("bug_count", 0),
        "end_reason": payload.get("end_reason"),
        "summary": render_summary_line(payload),
    }
