"""基座依赖上界的守护（审阅报告 §10 第 8 条）。

守的是什么
----------
**一个"约束没有上界"导致的静默炸弹。**

`langchain-mcp-adapters` 从 0.2.x 一直到 0.3.1，对 `mcp` 声明的一直是
**开区间下界**（`mcp>=1.9.2` / `mcp>=1.24.0`）——**没有上界**。而 `mcp 2.x` 把
`mcp.shared.context` 整个重写了：`RequestContext` 被删掉，换成了结构完全不同的
`BaseContext`。于是：

    mcp 升到 2.x
      → langchain_mcp_adapters/callbacks.py 的 `from mcp.shared.context import RequestContext` 失败
      → agentic_explorer.tools.common.custom_tools 导不进来
      → agentic_explorer.main 导不进来
      → tests/test_context_disclosure.py 在**收集期**就报错

真正危险的不是"报错"，而是它**被 `--ignore` 掩盖**了：CI 照旧全绿，同时
`main.py` 根本导不进来。绿灯与实质损坏同时成立 —— 这正是本项目一直在防的假绿形态。

上游在 **0.3.2** 修了它：`mcp>=1.24.0,`**`<2.0.0`**。所以本守护断言的是：

> **依赖声明必须停在一个"已经自己声明了 mcp 上界"的适配器版本上。**
> 具体化为三条可静态检查的线（**不联网、不装基座依赖**，因此能进 CI 硬门禁）：
>   1. `pyproject.toml` 里 `langchain-mcp-adapters` 的**下界 >= 0.3.2**；
>   2. `requirements.txt`（uv 锁）里 `mcp` 被钉在 **< 2.0.0**；
>   3. `ci.yml` 里**不得再用 `--ignore` 把这个测试遮起来**。

为什么用"版本下界"做代理，而不是直接读上游元数据？
硬门禁作业按设计**不装 langchain/mcp**（装得越少越快出结论），所以读不到上游
`Requires-Dist`。版本下界是能静态检查的最贴近的代理：跨过 0.3.2 就等于跨过了
"上游开始替你管 mcp 大版本"这条线。

维护提示
--------
将来若真的要迁到 mcp 2.x / 适配器 0.4.x，**这个测试必须先红**——那不是"测试坏了"，
而是它要求的"这次迁移必须是一次显式决定"生效了。届时同步更新本文件顶部的
`MIN_ADAPTER_VERSION` 与 `MCP_MAX_MAJOR`，并在此处记录迁移依据。
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
LOCK = ROOT / "requirements.txt"
CI = ROOT / ".github" / "workflows" / "ci.yml"

# --------------------------------------------------------------------------- #
# 守护的两个常量 —— 改动它们等于"做了一次依赖迁移决定"，必须写明依据
# --------------------------------------------------------------------------- #
#: 第一个对 mcp 声明了上界（`mcp<2.0.0`）的适配器版本。
#: 0.3.0 / 0.3.1 仍是 `mcp>=1.24.0`（无上界）；0.3.2 起为 `mcp<2.0.0,>=1.24.0`
#: —— 依据为本地下载 0.3.0/0.3.1/0.3.2 三个 wheel 后读取其 METADATA 逐条比对。
MIN_ADAPTER_VERSION = (0, 3, 2)

#: mcp 允许的最大主版本（含）。`mcp 2.x` 移除了 `mcp.shared.context.RequestContext`。
MCP_MAX_MAJOR = 1

#: 适配器 0.3.2 对 mcp 声明的下界（`mcp>=1.24.0`）。锁文件里的 mcp 必须不低于它。
ADAPTER_MCP_FLOOR = (1, 24, 0)


# --------------------------------------------------------------------------- #
# 解析小工具（纯 stdlib —— 不依赖 packaging，保证硬门禁里没有额外依赖）
# --------------------------------------------------------------------------- #
def _parse_version(text: str) -> tuple[int, ...] | None:
    """把 `1.28.1` / `0.3.2` 解析成元组；无法解析（如带 ``*``）返回 None。"""
    text = text.strip()
    if not text or not text[0].isdigit():
        return None
    nums: list[int] = []
    for chunk in text.split("."):
        m = re.match(r"^(\d+)", chunk)
        if not m:
            break
        nums.append(int(m.group(1)))
    return tuple(nums) if nums else None


def _specifier_floor(spec: str) -> tuple[int, ...] | None:
    """取规格串的**下界**。

    `>=1.9.2` → (1,9,2)；`~=0.3.2` → (0,3,2)（等价于 >=0.3.2,<0.4.0）；
    `==1.28.1` → (1,28,1)；`<2.0.0` 之类的纯上界**不参与**。
    """
    best: tuple[int, ...] | None = None
    for part in spec.split(","):
        m = re.match(r"^(>=|~=|==|>)\s*(\S.*)$", part.strip())
        if not m:
            continue
        version = _parse_version(m.group(2))
        if version is None:
            continue
        if best is None or version > best:
            best = version
    return best


def _pyproject_dependencies() -> dict[str, str]:
    """`{包名(小写): 规格串}` —— 只取 [project].dependencies 里的直接声明。"""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for raw in data.get("project", {}).get("dependencies", []):
        m = re.match(r"^\s*([A-Za-z0-9._-]+)\s*(.*)$", raw)
        if not m:
            continue
        out[m.group(1).lower()] = m.group(2).strip()
    return out


def _locked_versions() -> dict[str, tuple[int, ...]]:
    """`{包名(小写): 版本}` —— 只认 `name==x.y.z` 这种钉死的行。"""
    out: dict[str, tuple[int, ...]] = {}
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9._-]+)==(\S+)\s*$", line)
        if not m:
            continue
        version = _parse_version(m.group(2))
        if version is not None:
            out[m.group(1).lower()] = version
    return out


# --------------------------------------------------------------------------- #
# 1. 声明侧：pyproject.toml
# --------------------------------------------------------------------------- #
def test_pyproject_declares_the_adapter():
    """适配器必须是被**直接声明**的依赖，而不是靠别的包捎带进来。

    直接声明 = 我们能对它表态；捎带进来 = 它的版本由别人的上界决定，出问题时
    连"该改哪儿"都不明确。
    """
    deps = _pyproject_dependencies()
    assert "langchain-mcp-adapters" in deps, (
        "pyproject.toml 必须直接声明 langchain-mcp-adapters —— "
        "否则它属于 mcp 的传递依赖，上界无人负责（这正是本次缺陷的成因）"
    )


def test_pyproject_adapter_floor_at_or_above_min():
    """声明侧的下界必须 >= 0.3.2（上游开始自己管 mcp 大版本的那一版）。"""
    spec = _pyproject_dependencies().get("langchain-mcp-adapters", "")
    floor = _specifier_floor(spec)
    assert floor is not None, f"无法从 langchain-mcp-adapters 的规格串解析出下界：{spec!r}"
    assert floor >= MIN_ADAPTER_VERSION, (
        f"langchain-mcp-adapters 的下界 {floor} 低于 {MIN_ADAPTER_VERSION}；"
        f"低于该版本的上游声明对 mcp **没有上界**，会静默漂到 mcp 2.x 并炸掉 "
        f"mcp.shared.context.RequestContext（规格串：{spec!r}）"
    )


# --------------------------------------------------------------------------- #
# 2. 锁定侧：requirements.txt（uv 编译产物）
# --------------------------------------------------------------------------- #
def test_lock_pins_the_adapter():
    assert "langchain-mcp-adapters" in _locked_versions(), (
        "requirements.txt 必须钉死 langchain-mcp-adapters 的版本"
    )


def test_lock_adapter_at_or_above_floor():
    version = _locked_versions().get("langchain-mcp-adapters")
    assert version is not None, "锁文件里没有 langchain-mcp-adapters"
    assert version >= MIN_ADAPTER_VERSION, (
        f"锁文件把 langchain-mcp-adapters 钉在 {version}，低于 {MIN_ADAPTER_VERSION}；"
        f"该版本对 mcp 无上界 → 重新解析依赖时会漂到 mcp 2.x"
    )


def test_lock_pins_mcp_below_2():
    """**本守护的核心**：锁文件里的 mcp 必须 < 2.0.0。"""
    version = _locked_versions().get("mcp")
    assert version is not None, "锁文件里没有 mcp（它应由适配器传递引入并被钉死）"
    assert version[0] <= MCP_MAX_MAJOR, (
        f"锁文件把 mcp 钉在 {version}，跨过了主版本 {MCP_MAX_MAJOR + 1}；"
        f"mcp 2.x 已移除 `mcp.shared.context.RequestContext`，会让 "
        f"langchain_mcp_adapters 导入失败并一路炸到 main.py"
    )


def test_lock_mcp_satisfies_declared_adapter_floor():
    """锁里的 mcp 还必须同时满足适配器声明的下界 —— 防"为了躲 2.x 而降到太旧"。"""
    version = _locked_versions().get("mcp")
    assert version is not None, "锁文件里没有 mcp"
    assert version >= ADAPTER_MCP_FLOOR, (
        f"锁里的 mcp {version} 低于适配器声明的下界 {ADAPTER_MCP_FLOOR}，"
        f"依赖本身不自洽"
    )


# --------------------------------------------------------------------------- #
# 3. 掩盖侧：不许再把受影响的测试用 --ignore 遮起来
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("flag", ["--ignore", "--deselect"])
def test_ci_does_not_mask_context_disclosure(flag):
    """`ci.yml` 里不得再出现 `--ignore/--deselect ... test_context_disclosure`。

    这条守的是"掩盖"本身：本次缺陷能藏很久，靠的不是代码错，而是那行 `--ignore`。
    允许在注释里提到这个文件名（说明来龙去脉），只禁止把它写进**实际参数**。
    """
    offenders = [
        line.strip()
        for line in CI.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith(flag) and "test_context_disclosure" in line
    ]
    assert not offenders, (
        f"ci.yml 里 {flag} 又把这个测试遮起来了：{offenders}\n"
        f"——它因 mcp 版本冲突而在收集期失败，已由适配器升到 "
        f"{'.'.join(map(str, MIN_ADAPTER_VERSION))} 修复；若确实需要跳过，"
        f"请先说明新原因，而不是重新盖上去。"
    )
