"""阶段注册表 + 依赖拓扑执行（E 层：把**隐式顺序**变成**显式依赖**）。

背景（`docs/HARNESS_ARCHITECTURE_REVIEW.md` §4.4 / §7 序 7）
----------------------------------------------------------
全流程流水线的"阶段顺序"里，承载着至少一条**不写在类型里的强约束**：

    `diff`（失败项新旧对比）**必须在** `snapshot`（写本次快照）**之前** ——
    否则历史里最新一条就是本次自己，自己跟自己比，结论永远是"没有新增失败"。

这条约束原先只靠**代码书写顺序 + 一条注释 + 人的记忆**维持。任何一次
"顺手把这两步调个位置"都会静默破坏它，而且**不会有任何测试变红** ——
典型的"信号被污染但没人报警"。

本模块把这种隐式顺序收敛成**显式依赖**：每个阶段声明 `name` / `requires` / `run`，
执行前用**稳定拓扑排序**解析出真实顺序。于是：

  - 想调整阶段，必须显式改 `requires`；依赖不满足会**当场报错**，而不是静默乱序执行；
  - 想删除某条约束，`resolve_order` 的结果就变了 —— **等价性守卫立刻变红**。

本模块只提供**机制**，不含任何具体阶段（阶段声明留在调用方，如 `project_manager.py`）。
只依赖标准库（`time` + 统一日志出口），因此可在**不装任何重依赖**的 CI 硬门禁里被直接测试。

排序规则（稳定拓扑）
--------------------
每一轮从"依赖已满足"的候选里，取**声明顺序最靠前**的那个。因此：

  - **真实依赖**决定必须的先后；
  - **彼此无依赖**的阶段，保持**声明顺序**（= 重构前的书写顺序，人读的意图）。

结果因此是**确定**的：同样的注册表永远解析出同样的顺序。

两个可选声明（机制不越界，只做如实汇报）
----------------------------------------
- `Stage.enabled`：**本次该不该跑**。判否则整段跳过，并在回调里以 `seconds=None`
  汇报为"未执行"—— "没跑"必须与"跑得很快"可分辨（本仓库反复栽在
  "把未执行当成已通过"上，计时同理）。
- `run_stages(on_stage=...)`：**横切关注点**（计时 / 进度 / 审计）的挂载点。
  机制层不认识"效率"这个词，只负责如实回答"谁跑了多久、有没有抛错、有没有被跳过"。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Sequence, Tuple

from common.obs import get_logger

log = get_logger("pipeline")


class StageError(Exception):
    """阶段声明或依赖解析出错（重名 / 未知依赖 / 自环 / 成环）。

    刻意抛**异常**而不是"打印一句后继续"：依赖解析失败属于**声明错误**，
    此时任何执行顺序都是错的 —— 静默继续等于把错误的顺序当成正确的跑下去，
    正是本模块要消灭的那类"无声失效"。
    """


@dataclass(frozen=True)
class Stage:
    """一个流水线阶段。

    字段
    ----
    name      阶段名（注册表内唯一，作为 `requires` 的引用键）
    run       执行体 `run(ctx)`：就地修改传入的上下文，不返回值
    requires  必须**先于本阶段执行**的阶段名。**数据依赖**与**写序约束**都写在这里 ——
              两者对执行顺序的约束力是等价的，不区分对待。
    doc       一句话说明，供文档与排障使用
    enabled   可选谓词 `enabled(ctx) -> bool`：**本次该不该跑**。判否时整段跳过，
              `run` 根本不被调用，且在 `StageOutcome` 里记为 `seconds=None`（未执行）。

              为什么把开关放在**声明处**而不是阶段体内"进去先 return"：
              后者会让"没跑"与"跑得飞快"在计时上长得一模一样（都是 ≈0 秒）——
              报告上看起来像"这个阶段很快"，实际是"这个阶段根本没发生"。
    """
    name: str
    run: Callable[[Any], None]
    requires: Tuple[str, ...] = ()
    doc: str = field(default="")
    enabled: Optional[Callable[[Any], bool]] = None


@dataclass(frozen=True)
class StageOutcome:
    """一个阶段的执行结局（供横切关注点消费：计时 / 进度 / 审计）。

    `seconds is None` 表示**本阶段未执行**（被 `Stage.enabled` 判否跳过）——
    这与"执行了、只是很快（≈0 秒）"是**两件事**，绝不能混为一谈。

    `error is not None` 表示阶段体内抛了异常（此时 `seconds` 仍是**实测值**）：
    失败也必须计时，否则最慢的失败路径会从统计里消失，数字只会更好看。
    """
    name: str
    seconds: Optional[float]
    error: Optional[BaseException] = None

    @property
    def executed(self) -> bool:
        return self.seconds is not None


def validate(stages: Sequence[Stage]) -> None:
    """静态校验声明本身是否自洽：重名 / 未知依赖 / 自环。发现即抛 `StageError`。"""
    names = [s.name for s in stages]
    seen = set()
    for n in names:
        if n in seen:
            raise StageError(f"阶段名重复：{n!r}")
        seen.add(n)

    known = set(names)
    for s in stages:
        for r in s.requires:
            if r == s.name:
                raise StageError(f"阶段 {s.name!r} 依赖它自己")
            if r not in known:
                raise StageError(
                    f"阶段 {s.name!r} 依赖未知阶段 {r!r}；已声明：{sorted(known)}")


def resolve_order(stages: Sequence[Stage]) -> List[str]:
    """按**稳定拓扑排序**返回执行顺序（阶段名列表）。

    - 先 `validate`；声明有误直接抛 `StageError`（绝不返回"看起来能跑"的顺序）。
    - 每轮挑"依赖已满足"里**声明顺序最靠前**的那个 → 结果确定。
    - 若仍有阶段无法就绪 → 说明**存在环**，抛 `StageError` 并报出参与成环的阶段。
    """
    validate(stages)
    done: set = set()
    order: List[str] = []
    remaining = list(stages)

    while remaining:
        picked: Optional[Stage] = None
        for s in remaining:
            if all(r in done for r in s.requires):
                picked = s
                break
        if picked is None:
            blocked = "；".join(f"{s.name} 等 [{', '.join(s.requires)}]" for s in remaining)
            raise StageError(f"阶段依赖成环，无法解析执行顺序：{blocked}")
        order.append(picked.name)
        done.add(picked.name)
        remaining.remove(picked)

    return order


def run_stages(stages: Sequence[Stage], ctx: Any,
               on_stage: Optional[Callable[[StageOutcome], None]] = None) -> List[str]:
    """按 `resolve_order` 的顺序依次执行各阶段，返回**实际执行顺序**（含被跳过的）。

    阶段自身抛出的异常**原样向上传播** —— "某一步失败要不要继续"是业务语义，
    由阶段实现自己决定（可用 try/except 包住自己的易错部分），
    机制层不替它一刀切。

    `Stage.enabled` 判否的阶段**整段跳过**（`run` 不被调用），在回调里以
    `seconds=None` 汇报为"未执行"。

    `on_stage`：可选回调，在**每个阶段结束后**调用一次（成功 / 失败 / 跳过都会调用）。
    成功与失败都带 `seconds`（实测墙钟），跳过则为 `None`。
    ⚠️ 回调自身抛异常会被**记日志后忽略** —— 统计出问题绝不能改变业务结果；
    但也不静默吞掉，否则"计时器坏了"会没人知道。

    返回的顺序是 `resolve_order` 的结果（**注册表的全序**，不因跳过而缩短）：
    它是"这次打算跑什么"的权威记录，与"实际执行了什么"是两个不同的问题，
    后者由 `on_stage` 汇报的 outcome 回答。
    """
    by_name = {s.name: s for s in stages}
    order = resolve_order(stages)

    def _report(outcome: StageOutcome) -> None:
        if on_stage is None:
            return
        try:
            on_stage(outcome)
        except Exception as e:
            # 必须出声：统计/Hook 坏了要看得见；但绝不因此中断业务流水线。
            log.warning("阶段回调失败（统计受影响，业务继续）：%s: %s",
                        type(e).__name__, e)

    for name in order:
        stage = by_name[name]
        if stage.enabled is not None and not stage.enabled(ctx):
            _report(StageOutcome(name=name, seconds=None))
            continue
        t0 = time.perf_counter()
        err: Optional[BaseException] = None
        try:
            stage.run(ctx)
        except BaseException as e:
            err = e
            raise
        finally:
            # 失败也要计时：把最慢的失败路径从统计里抹掉，数字只会更好看。
            _report(StageOutcome(name=name,
                                 seconds=time.perf_counter() - t0,
                                 error=err))
    return order
