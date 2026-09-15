"""效率口径（§10 #2）：本系统对「效率」的**唯一**测量定义。

背景
====
需求里写着"提升测试效率"，但此前**没有任何测量方式** —— 一句无法验收的口号。
本模块把它落成一个**可自动计算**的口径，同时把它**测不到的部分**写死在这里，
免得日后有人拿近似值冒充。

口径（唯一）
============
    效率 := 一次 `run` 的**进程内墙钟耗时**（秒），按阶段分解为明细 + 合计。

**范围**：`需求 → 报告生成`。`report` 阶段**自身**的渲染耗时不计入 ——
它正是在生成报告时渲染本卡片，无法预先知道自己的耗时。该排除项写在载荷的
`excluded` 字段里，不靠读者猜。

**测得到**：机器侧时间（阶段级明细 + 合计 + 未执行名单）。
**测不到（不猜、不近似）**：
  - **人力节省 / 人工替代率** —— 需要"人工写同等资产要多久"的**外部基线**，
    系统内部无从测量。本口径**不给这个数**，也**不允许**任何估算值冒充它。
  - **质量** —— 耗时短 ≠ 效率高。5 秒产出 10 条废用例，并不比 60 秒产出
    10 条可用用例更"高效"。时间只是原料，不是成绩；用例好坏另有结构质量分
    （`case_quality.py`）与其自身的"结构分 ≠ 质量判定"克制度。

三条纪律（沿用项目既有判据）
============================
1. **未执行 ≠ 0 秒**（同 CI 门禁三态的「未执行不是绿」）。本次没开启的阶段记为
   `seconds=None` 并落进 `skipped`，绝不记成 `0.000` —— 否则"没跑"在报告上
   看起来像"跑得飞快"。
2. **失败阶段照实计时**。抛异常的阶段同样要记耗时：把最慢的失败路径从统计里
   抹掉，只会让数字看起来更好看，又是一种假绿。
3. **算不出就不猜**。拿不到的时间记 `None`，不用 `0` 或估算值冒充；合计只在
   **至少有一个阶段被实测**时给出，否则为 `None`。

与 pipeline 的关系
==================
本模块**不 import** `common.pipeline`：只做"从鸭子类型的 outcome 序列构造口径条目"，
方向是单向的（pipeline 不必知道效率这回事，timing 也不必知道阶段怎么排序）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

SPEC = "timing/1"
SCOPE = "需求 → 报告生成"

#: **明确不承诺测量**的项。逐条列出，而不是留白 —— 留白会被当成"应该能算"。
NOT_MEASURED = (
    "人力节省 / 人工替代率（需外部工时基线，系统内无法测量）",
    "质量（耗时短不等于效率高；用例好坏由结构质量分另行评价）",
)

#: 计入载荷的排除项（口径范围内、但本次不测量）。
EXCLUDED = (
    "report 阶段自身的渲染耗时（生成报告时无法预知自己的耗时）",
)

#: 错误摘要落盘的截断长度；完整信息在日志里，这里只留指针。
_ERROR_MAX = 200


@dataclass(frozen=True)
class StageTime:
    """一个阶段的耗时条目。

    seconds is None ⇔ 本阶段**未执行**（不是"耗时为零"）。
    """
    name: str
    seconds: Optional[float]
    ok: bool = True
    error: Optional[str] = None

    @property
    def executed(self) -> bool:
        return self.seconds is not None


# --------------------------------------------------------------------------- #
# 构造
# --------------------------------------------------------------------------- #
def from_outcome(name: str, seconds: Optional[float],
                 error: Optional[BaseException] = None) -> StageTime:
    """由「阶段名 / 秒数 / 异常」构造口径条目。

    秒数照实收（含失败阶段）；异常只留 `类型: 摘要`（截断），完整栈在日志里。
    """
    detail: Optional[str] = None
    if error is not None:
        detail = f"{type(error).__name__}: {error}"[:_ERROR_MAX]
    return StageTime(
        name=str(name),
        seconds=(None if seconds is None else float(seconds)),
        ok=error is None,
        error=detail,
    )


def from_outcomes(outcomes: Iterable[Any]) -> List[StageTime]:
    """把 `pipeline.StageOutcome` 序列转成口径条目（鸭子类型，不反向依赖 pipeline）。

    字段缺失按"未执行 + 无错误"处理，而不是抛错 —— 口径层宁可少记一条，
    也不因为上游形状变化就中断整条流水线。
    """
    out: List[StageTime] = []
    for o in outcomes or ():
        out.append(from_outcome(
            getattr(o, "name", ""),
            getattr(o, "seconds", None),
            getattr(o, "error", None),
        ))
    return out


# --------------------------------------------------------------------------- #
# 汇总（全部遵守"算不出就不猜"）
# --------------------------------------------------------------------------- #
def executed(stages: Sequence[StageTime]) -> List[StageTime]:
    return [s for s in stages if s.seconds is not None]


def skipped(stages: Sequence[StageTime]) -> List[str]:
    """本次**未执行**的阶段名（顺序与执行序一致）。"""
    return [s.name for s in stages if s.seconds is None]


def failed(stages: Sequence[StageTime]) -> List[str]:
    """阶段体内抛过异常的阶段名（它们**已被计时**，只是结果不算数）。"""
    return [s.name for s in stages if not s.ok]


def total_seconds(stages: Sequence[StageTime]) -> Optional[float]:
    """各**已实测**阶段之和；一个都没测到 → None（不猜）。"""
    secs = [s.seconds for s in executed(stages) if s.seconds is not None]
    return sum(secs) if secs else None


def format_seconds(sec: Optional[float]) -> str:
    """人类可读的时长。None（未执行/未测到）显式写作「未执行」，不写作 0。"""
    if sec is None:
        return "未执行"
    if sec < 1:
        return f"{sec * 1000:.0f}ms"
    if sec < 60:
        return f"{sec:.1f}s"
    minutes, rest = divmod(int(round(sec)), 60)
    return f"{minutes}m{rest:02d}s"


# --------------------------------------------------------------------------- #
# 载荷（落盘 / 展示的唯一形状）
# --------------------------------------------------------------------------- #
def build(stages: Sequence[StageTime], *, scope: str = SCOPE) -> Dict[str, Any]:
    """构建落盘载荷（写进 `run_meta.json` 的 `timing` 字段）。

    形状一旦确定就不再变化 —— `load` 会按同一形状校验，任何"隐式扩展"
    都会在下游读到 None，从而**不会**被当成有效数据。
    """
    total = total_seconds(stages)
    detail: List[Dict[str, Any]] = []
    for s in stages:
        item: Dict[str, Any] = {
            "name": s.name,
            "executed": s.executed,
            "ok": s.ok,
            "seconds": (None if s.seconds is None else round(s.seconds, 3)),
        }
        if s.error:
            item["error"] = s.error
        detail.append(item)
    return {
        "spec": SPEC,
        "scope": scope,
        "unit": "seconds",
        "measured": "进程内墙钟（time.perf_counter，单调时钟）",
        "stages": detail,
        "total_seconds": (None if total is None else round(total, 3)),
        "executed_count": len(executed(stages)),
        "stage_count": len(list(stages)),
        "skipped": skipped(stages),
        "failed": failed(stages),
        "excluded": list(EXCLUDED),
        "not_measured": list(NOT_MEASURED),
    }


def load(payload: Any) -> Optional[Dict[str, Any]]:
    """校验形状；不是本模块写出的载荷一律返回 None（"缺字段不算数"）。

    刻意**不做兼容性猜测**：消费侧拿到 None 就会把整块卡片省掉，
    而不是渲染出一张半真半假的效率卡。
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("spec") != SPEC:
        return None
    if not isinstance(payload.get("stages"), list):
        return None
    return payload


# --------------------------------------------------------------------------- #
# 呈现
# --------------------------------------------------------------------------- #
def summary_line(payload: Any) -> str:
    """单行摘要（CLI 收尾打印用）。"""
    loaded = load(payload)
    if loaded is None:
        return "效率：未测量"
    total = format_seconds(loaded.get("total_seconds"))
    parts = [f"流水线耗时 {total}", f"已执行 {loaded.get('executed_count')}/{loaded.get('stage_count')} 阶段"]
    sk = loaded.get("skipped") or []
    if sk:
        parts.append(f"未执行：{'、'.join(sk)}")
    return "效率：" + " · ".join(parts)


def render_lines(payload: Any) -> List[str]:
    """多行明细（报告 / 日志 / 排障）。未执行与未测量都显式写出。"""
    loaded = load(payload)
    if loaded is None:
        return ["效率口径：本次无数据（不是 0 秒，是没测到）"]

    lines = [f"效率口径 [{loaded.get('spec')}] —— {loaded.get('scope')}",
             f"  合计：{format_seconds(loaded.get('total_seconds'))}"
             f"（{loaded.get('measured')}）"]
    for st in loaded.get("stages") or []:
        if not isinstance(st, dict):
            continue
        name = st.get("name", "?")
        if st.get("executed") is False:
            lines.append(f"    - {name}：未执行")
            continue
        note = "" if st.get("ok", True) else "（阶段内异常，耗时仍计入）"
        lines.append(f"    - {name}：{format_seconds(st.get('seconds'))}{note}")
    if loaded.get("failed"):
        lines.append(f"  异常阶段：{'、'.join(loaded['failed'])}")
    for item in loaded.get("excluded") or []:
        lines.append(f"  已排除：{item}")
    for item in loaded.get("not_measured") or []:
        lines.append(f"  未测量：{item}")
    return lines
