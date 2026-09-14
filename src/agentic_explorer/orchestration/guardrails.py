"""B 世界（`src/agentic_explorer`）探索循环的 **harness 侧护栏**（审阅报告 §3.2 / §7 序 5）。

问题（§3.2，均已复核）
----------------------
1. **`max_steps` 不是上限，是软重置触发器**：到顶就把计数归 1 并注入"换个区域"指令
   （`graph_base.py:493-511`）→ **理论上可以无限循环**。
2. **结束只有模型自评的一句 `FINISH`**，无可枚举的原因 → 无法审计。
3. **"目标是否达成"由报告用的 LLM 自评**（`main.py` 的 "Final Status (PASS or FAIL …)"）。

> 一致性追问（§3.2）：我们不信任**被测系统**（HTTP 4xx/5xx 一律 FAIL、环境不可达绝不判绿），
> 却把"停不停""成没成"交给 **agent 自己**说了算 —— 同一个团队两套信任标准。

本模块把这三件补上，并且**刻意与框架解耦**：只 import 标准库，不碰 langgraph / langchain。
理由很实际——CI 的**硬门禁**只装 `requirements-dev.txt`（不含基座重型依赖），
零依赖才能在门禁里被直接测试，而不是"写完没人验"。

设计要点
--------
- **软硬分离**（§9 魔鬼代言人 #4）：`max_steps` 的**软**重置照旧保留（探索策略：避免过早收敛）；
  另加**硬** `max_turns`（资源安全阀：到顶即终止、**不再重置**）。两者是两件事。
- **结束原因枚举化**：可审计的前提是原因可枚举（`EndReason`）。
- **达成由程序判定**：把 mission 目标编译成**确定性断言**（`judge_mission`），
  模型只能提供**候选**结论。**算不出就不猜** —— 判不了返回 `unknown`，不冒充通过、也不冒充失败。
- **诚实标注**：
  * token 预算只统计**能从消息 `usage_metadata` 读到**的部分（对话类调用）；
    supervisor 的路由调用不回传 usage，**不计入** —— 这是如实记账，不是假装精确。
  * `step_timeout` **默认不启用**（`0`）：现有 Playwright 已有动作级超时（5s/15s），
    turn 级超时可能**打断合法的长回合**，所以做成显式开关而非默认开启。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

__all__ = [
    "EndReason",
    "GoalVerdict",
    "Limits",
    "MissionGoal",
    "check_limits",
    "count_tokens",
    "count_new_tokens",
    "judge_mission",
    "classify_end_reason",
    "parse_end_reason",
    "build_result",
    "render_verdict_markdown",
]

# 环境变量（与 A 世界的 STA_* 命名区分开：这里是基座智能层的开关）
ENV_MAX_TURNS = "AGENT_MAX_TURNS"
ENV_TOKEN_BUDGET = "AGENT_TOKEN_BUDGET"
ENV_STEP_TIMEOUT = "AGENT_STEP_TIMEOUT"

# 硬上限默认值：约 4 个软周期（max_steps 默认 30）。够探索，但有界。
DEFAULT_MAX_TURNS = 120
DEFAULT_TOKEN_BUDGET = 0          # 0 = 显式不限（"没设预算"就说没设，不假装有）
DEFAULT_STEP_TIMEOUT = 0.0        # 0 = 不启用（见模块 docstring 的取舍）


# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #
class EndReason(str, Enum):
    """一次 mission 的**结构化**结束原因（对照 DeepSeek Harness 的 turn 结束 5 态）。"""

    COMPLETED = "completed"                    # 循环正常结束，且程序判定目标达成
    BLOCKED = "blocked"                        # 循环结束，但程序无法确认达成
    MAX_TURNS = "max-turns"                    # 触及硬轮次上限（未达成）
    BUDGET_EXHAUSTED = "budget-exhausted"      # 触及 token 预算（未达成）
    ERROR = "error"                            # 运行期异常


class GoalVerdict(str, Enum):
    """程序对"目标是否达成"的判定。`unknown` 是**一等公民**：算不出就不猜。"""

    ACHIEVED = "achieved"
    UNACHIEVED = "unachieved"
    UNKNOWN = "unknown"


# --------------------------------------------------------------------------- #
# 上限（Limits）
# --------------------------------------------------------------------------- #
def _env_int(raw: Any, default: int) -> int:
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(float(str(raw).strip()))     # 容忍 "120" 与 "120.0"
    except (TypeError, ValueError):
        return default


def _env_float(raw: Any, default: float) -> float:
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Limits:
    """一次运行的**硬**资源上限。`0` 一律表示"该维度不限"（显式声明，而非默认放开）。"""

    max_turns: int = DEFAULT_MAX_TURNS
    token_budget: int = DEFAULT_TOKEN_BUDGET
    step_timeout: float = DEFAULT_STEP_TIMEOUT

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None,
                 *, max_steps: Optional[int] = None) -> "Limits":
        """从环境变量解析；`max_steps`（软周期长度）用于推导硬上限的默认值。

        写错的值**退回默认**而不是放行成"无限" —— 一个打错的数字不该把护栏关掉。
        """
        env = os.environ if env is None else env

        default_turns = DEFAULT_MAX_TURNS
        if isinstance(max_steps, int) and max_steps > 0:
            default_turns = max(4 * max_steps, 40)

        max_turns = _env_int(env.get(ENV_MAX_TURNS), default_turns)
        if max_turns <= 0:
            max_turns = default_turns

        token_budget = max(_env_int(env.get(ENV_TOKEN_BUDGET), DEFAULT_TOKEN_BUDGET), 0)
        step_timeout = max(_env_float(env.get(ENV_STEP_TIMEOUT), DEFAULT_STEP_TIMEOUT), 0.0)
        return cls(max_turns=max_turns, token_budget=token_budget, step_timeout=step_timeout)

    def as_dict(self) -> Dict[str, Any]:
        return {"max_turns": self.max_turns, "token_budget": self.token_budget,
                "step_timeout": self.step_timeout}


def check_limits(limits: Limits, *, turns: int, tokens: int) -> Optional[EndReason]:
    """是否已触及**硬**上限；`None` = 还能继续。

    ⚠️ `turns >= max_turns` 用的是 `>=`：这是**上限**，不是"触发重置的阈值"。
    旧的软重置用的是 `current_step > max_steps` —— 语义差别正在此处。
    """
    if limits.token_budget > 0 and tokens >= limits.token_budget:
        return EndReason.BUDGET_EXHAUSTED
    if turns >= limits.max_turns:
        return EndReason.MAX_TURNS
    return None


def count_tokens(messages: Iterable[Any]) -> int:
    """累计消息里的 `usage_metadata.total_tokens`（鸭子类型，不依赖 langchain）。"""
    total = 0
    for m in messages or []:
        um = getattr(m, "usage_metadata", None)
        if isinstance(um, dict):
            t = um.get("total_tokens")
            if isinstance(t, int) and t > 0:
                total += t
    return total


def count_new_tokens(messages: Iterable[Any], prior_messages: Iterable[Any] = ()) -> int:
    """只统计**本轮新增**消息的 token —— 否则每轮都会把历史消息重复计入（严重高估）。

    内层 agent 返回的是**完整**消息列表（含历史），所以必须减去历史。
    匹配优先用消息 id（稳，且不怕"重排/替换"）；消息没有 id 时退回按长度切片。
    """
    msgs = list(messages or [])
    prior = list(prior_messages or [])
    prior_ids = {getattr(m, "id", None) for m in prior} - {None}
    if prior_ids:
        fresh = [m for m in msgs if getattr(m, "id", None) not in prior_ids]
    else:
        fresh = msgs[len(prior):]
    return count_tokens(fresh)


# --------------------------------------------------------------------------- #
# 目标判定（把 mission 目标编译成确定性断言）
# --------------------------------------------------------------------------- #
# Playwright/Chromium 的"连不上"错误标记。只认这些**连接级**失败 ——
# 业务报错（HTTP 4xx/5xx、表单校验失败）不算"不可达"，它们是**产品缺陷**（对应 A 世界的判据 #2）。
_UNREACHABLE_MARKERS = (
    "ERR_CONNECTION_REFUSED", "ERR_CONNECTION_TIMED_OUT", "ERR_CONNECTION_RESET",
    "ERR_NAME_NOT_RESOLVED", "ERR_INTERNET_DISCONNECTED", "ERR_ADDRESS_UNREACHABLE",
    "ERR_SOCKET_NOT_CONNECTED", "ERR_EMPTY_RESPONSE", "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_CONNECTION_CLOSED", "ERR_CONNECTION_ABORTED",
)


def _is_unreachable(entry: Dict[str, Any]) -> bool:
    if not isinstance(entry, dict) or entry.get("ok"):
        return False
    err = str(entry.get("error") or "")
    if not err:
        return False
    return "net::ERR_" in err or any(m in err for m in _UNREACHABLE_MARKERS)


def _path_of(value: Any) -> str:
    """把 URL 或路径归一成 path（去 host / query / fragment / 尾斜杠）。"""
    v = str(value or "").strip()
    if "://" in v:
        v = urlsplit(v).path or "/"
    v = v.split("?", 1)[0].split("#", 1)[0]
    if not v.startswith("/"):
        v = "/" + v
    if len(v) > 1:
        v = v.rstrip("/") or "/"
    return v


def _path_matches(required: str, visited: str) -> bool:
    r, v = _path_of(required), _path_of(visited)
    if r == "/":
        return True                       # 入口级要求：任何一次访问都算
    return v == r or v.startswith(r + "/")


def _visited_targets(explored_paths: Sequence[Any],
                     tape: Sequence[Dict[str, Any]]) -> List[str]:
    """汇总"实际访问过的地方"：探索路径 + tape 里记录的落点 URL + navigate 目标。"""
    out: List[str] = []
    for p in explored_paths or []:
        if isinstance(p, str) and p.strip():
            out.append(_path_of(p))
    for e in tape or []:
        if not isinstance(e, dict):
            continue
        pu = e.get("page_url")
        if isinstance(pu, str) and pu.strip():
            out.append(_path_of(pu))
        if e.get("action") == "navigate":
            params = e.get("params") or {}
            u = params.get("url")
            if isinstance(u, str) and u.strip():
                out.append(_path_of(u))
    return out


@dataclass(frozen=True)
class MissionGoal:
    """mission 的可判定目标。mission YAML 里可选地写一个 `goal:` 块；不写就用默认。

    默认 = 「至少一条成功动作 + 不得出现不可达错误」—— 够通用，不需要预先知道被测应用的路径。
    """

    must_visit: Tuple[str, ...] = ()
    require_actions: int = 1
    forbid_unreachable: bool = True

    @classmethod
    def from_spec(cls, raw: Any = None) -> "MissionGoal":
        """从 mission YAML 的 `goal:` 块构造；任何脏输入都退回默认，不因写错就放弃判定。"""
        if not isinstance(raw, dict):
            return cls()
        mv = raw.get("must_visit") or []
        if isinstance(mv, str):
            mv = [mv]
        must_visit = tuple(
            str(x).strip() for x in mv
            if isinstance(x, (str, int, float)) and str(x).strip()
        )
        require_actions = _env_int(raw.get("require_actions", 1), 1)
        require_actions = max(require_actions, 0)
        forbid_unreachable = bool(raw.get("forbid_unreachable", True))
        return cls(must_visit=must_visit, require_actions=require_actions,
                   forbid_unreachable=forbid_unreachable)


def judge_mission(goal: MissionGoal, *,
                  explored_paths: Sequence[Any] = (),
                  tape: Sequence[Dict[str, Any]] = ()) -> Tuple[GoalVerdict, List[str]]:
    """程序判定目标达成 —— 返回 (verdict, 依据/理由列表)。

    只评价**能评价**的断言：评价不了就说评价不了（`unknown`），不猜。
    """
    tape = [e for e in (tape or []) if isinstance(e, dict)]
    failures: List[str] = []
    unknowns: List[str] = []
    checks = 0

    if goal.require_actions > 0:
        checks += 1
        ok = [e for e in tape if e.get("ok")]
        if len(ok) < goal.require_actions:
            failures.append(f"成功动作 {len(ok)} 条 < 要求的 {goal.require_actions} 条")

    if goal.forbid_unreachable:
        checks += 1
        bad = [e for e in tape if _is_unreachable(e)]
        if bad:
            first = str(bad[0].get("error") or "")[:80]
            failures.append(f"{len(bad)} 条动作出现**不可达**错误（例：{first}）")

    if goal.must_visit:
        checks += 1
        visited = _visited_targets(explored_paths, tape)
        if not visited:
            unknowns.append("没有任何访问轨迹记录，无法判断是否到达目标路径")
        else:
            for p in goal.must_visit:
                if not any(_path_matches(p, v) for v in visited):
                    failures.append(f"未访问到目标路径 {p}")

    if failures:
        return GoalVerdict.UNACHIEVED, failures
    if unknowns:
        return GoalVerdict.UNKNOWN, unknowns
    if checks == 0:
        return GoalVerdict.UNKNOWN, [
            "mission 未声明任何可判定断言（require_actions=0、未禁止不可达、且无 must_visit）"
        ]
    return GoalVerdict.ACHIEVED, [f"{checks} 条确定性断言全部通过"]


# --------------------------------------------------------------------------- #
# 结束原因归类 + 结果落档
# --------------------------------------------------------------------------- #
def parse_end_reason(value: Any) -> Optional[EndReason]:
    """把 state 里存的 `end_reason` 解析回枚举；认不出来就当作"未设置"。"""
    if isinstance(value, EndReason):
        return value
    if not value:
        return None
    try:
        return EndReason(str(value))
    except ValueError:
        return None


def classify_end_reason(*, forced: Optional[EndReason] = None,
                        verdict: Optional[GoalVerdict] = None,
                        error: bool = False) -> EndReason:
    """定结束原因。

    优先级：**异常** > **硬上限**（轮次/预算）> 依程序判定（达成→completed，否则→blocked）。
    硬上限优先于 verdict：被硬停时目标根本没跑完，不该因为"恰好断言都过"就记成 completed。
    """
    if error:
        return EndReason.ERROR
    if forced in (EndReason.MAX_TURNS, EndReason.BUDGET_EXHAUSTED, EndReason.ERROR):
        return forced
    if verdict == GoalVerdict.ACHIEVED:
        return EndReason.COMPLETED
    return EndReason.BLOCKED


def build_result(*, thread_id: str, end_reason: EndReason, goal_verdict: GoalVerdict,
                 reasons: Sequence[str] = (), turns: int = 0, tokens: int = 0,
                 actions: int = 0, bugs: int = 0,
                 limits: Optional[Limits] = None) -> Dict[str, Any]:
    """落档到 `report_<thread_id>/result.json` 的结构化结果（给 V 层/报告链消费）。"""
    return {
        "thread_id": thread_id,
        "end_reason": end_reason.value if isinstance(end_reason, EndReason) else str(end_reason),
        "goal_verdict": goal_verdict.value if isinstance(goal_verdict, GoalVerdict) else str(goal_verdict),
        "reasons": [str(r) for r in (reasons or [])],
        "turns": int(turns),
        "tokens": int(tokens),
        "actions": int(actions),
        "bugs": int(bugs),
        "limits": limits.as_dict() if limits is not None else None,
    }


_VERDICT_LABEL = {
    "completed": "✅ completed —— 循环正常结束，且程序判定目标达成",
    "blocked": "⛔ blocked —— 循环结束，但程序无法确认目标达成",
    "max-turns": "🛑 max-turns —— 触及**硬**轮次上限，未达成",
    "budget-exhausted": "💸 budget-exhausted —— 触及 token 预算，未达成",
    "error": "💥 error —— 运行期异常",
}


def render_verdict_markdown(result: Dict[str, Any]) -> str:
    """把结构化结果渲染成可追加进报告的一段 Markdown（程序判定权威段）。"""
    reason = str(result.get("end_reason", ""))
    label = _VERDICT_LABEL.get(reason, reason)
    lines = [
        "",
        "---",
        "",
        "## 程序判定（权威 · 由 harness 计算，非模型自评）",
        "",
        f"- **结束原因（枚举）**：`{reason}` — {label}",
        f"- **目标判定**：`{result.get('goal_verdict')}`",
        f"- **轮次 / token**：{result.get('turns')} 轮 / {result.get('tokens')} tokens",
        f"- **动作记录 / 缺陷**：{result.get('actions')} 条 / {result.get('bugs')} 个",
    ]
    reasons = result.get("reasons") or []
    if reasons:
        lines.append("- **判定依据**：")
        lines.extend(f"  - {r}" for r in reasons)
    lines += [
        "",
        "> 说明：本段由程序计算，与上文由模型生成的描述**可能不一致**——以本段为准。",
        "> 「算不出就不猜」：判不了会写 `unknown`，既不冒充通过也不冒充失败。",
        "",
    ]
    return "\n".join(lines)
