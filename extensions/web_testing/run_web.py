"""Web UI 冒烟执行器（Playwright + 声明式 YAML，CI 可门禁）。

补齐项目承诺的「接口 / **Web** 自动化」里的 Web 那一半：
接口侧由 `extensions/api_testing` + `extensions/regression` 覆盖，
这里把「浏览器里的关键用户路径」也变成可门禁的声明式回归。

## 与基座的关系（不重复造轮子，但要分清用途）
基座 `src/agentic_explorer/tools/browser/engine.py` 是**面向智能体的交互式探索**
（LLM 发 JSON 意图 → 引擎执行 → 记录 Action Tape），适合"探索未知页面"。
本模块是**面向 CI 的确定性执行**：场景预先声明、结果可判定、失败即非零退出。
两者互补：探索用基座，回归门禁用这里。
但**定位器与等待的方法论完全继承** `agent-skills/web-automation`（见下方约束），
避免出现"智能体写出的用例被引擎拒绝、而门禁脚本却放行脆弱定位器"的双标。

## 继承的三条方法论约束
1. **定位器优先级**：`data-test-subj` → `aria-label` → 可见文本 → 语义 role；
   **拒绝 XPath 与位置选择器**（`:nth-child`、`/html/…`、裸 div/span 链）——
   它们会在视口或布局微调时碎掉，产出的是**假红**（噪音），必须在使用前就被拦下。
2. **显式等待替代固定 sleep**：用 Playwright 的 web-first 断言（自动重试）
   与条件等待，而不是 `sleep(2)`——固定 sleep 是 e2e 不稳定的头号来源。
3. **失败留证**：失败自动截图，并生成可复现的 `.spec.ts`（对应基座的
   `capture_bug_screenshot` 与 `generate_playwright_spec`）。

## 三道闸门（与核心回归 / 性能安全完全一致）
1. **无断言不算绿**：一个场景若没有任何 `expect_*` 步骤，即使所有操作都"成功"也无法
   判定对错 → 记 `SKIP`（防止"点了就走、永远通过"的假绿场景混进门禁）。
2. **环境不可达不判绿**：连不上目标 / 浏览器起不来 → 全部 `SKIP`，`all_pass=False`，
   退出码 1。但**区分两类失败**：连接级错误（`ERR_CONNECTION_REFUSED` 等）算环境不可达；
   HTTP 4xx/5xx 算**产品/路由缺陷** → FAIL。混为一谈会导致"服务没起就报一堆用例失败"，
   或更糟——"应用 500 被当成环境问题跳过"。
3. **配置问题单独报出**：脆弱定位器、未知步骤、缺必填参数 → 记入 `config_issues`，
   门禁判不通过并显式打印，而不是静默跳过（静默跳过会让门禁悄悄变松）。

## 抖动与 flaky
默认 `retries: 0`（**不掩盖真实缺陷**：重试会吃掉偶发 bug 的证据）。
需要抗抖动时在 `web.yaml` 配 `retries: 1..2`；**重试后才通过**的场景结果仍记 PASS，
但打上 `flaky: true` 并单独计数、在报告里标出——重试可以降噪，但不该把抖动藏起来。

## 输入格式（projects/<id>/web.yaml）
```yaml
web:
  base_url: http://localhost:8090      # Web 前端地址；不写则用 project.yaml 的 env.web_base_url / base_url
  browser: chromium                    # chromium | firefox | webkit
  headless: true
  timeout_ms: 15000                    # 单步超时
  retries: 0
  viewport: {width: 1440, height: 900}
  launch_args: []                      # 崩溃时的逃生口，如 ["--no-sandbox","--disable-gpu"]
  scenarios:
    - name: 管理员登录进入后台
      tags: [smoke]
      steps:
        - goto: /login
        - fill: {selector: "[data-test-subj='username']", value: "{{username}}"}
        - fill: {selector: "[data-test-subj='password']", value: "{{password}}"}
        - click: "[data-test-subj='submit']"
        - wait_for_url: "**/home"
        - expect_visible: "[data-test-subj='sidebar']"
        - expect_text: {selector: "h1", contains: "工作台"}
        - screenshot: login-ok
```

## 用法
    python run_web.py --project projects/mall-admin/project.yaml \
                      --web projects/mall-admin/web.yaml \
                      --json projects/mall-admin/artifacts/web.json
    python project_manager.py web mall-admin [--only 场景名] [--headed] [--browser firefox]
    python project_manager.py run mall-admin --web
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

# ---------------------------------------------------------------------------
# 共享实现层（唯一定义处）
# ---------------------------------------------------------------------------
# 不再与 run_regression「同源靠约定」，而是**同一个函数对象**（extensions/common）。
# 见 docs/HARNESS_ARCHITECTURE_REVIEW.md §4.1。
_EXTENSIONS_DIR = Path(__file__).resolve().parent.parent              # extensions/
if str(_EXTENSIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_EXTENSIONS_DIR))
from common.yamlio import load_yaml as _load_yaml                     # noqa: E402
from common.auth import resolve_auth as _resolve_auth                 # noqa: E402
from common.auth import load_dotenv as _load_dotenv                   # noqa: E402
from common.data import substitute as _substitute                     # noqa: E402
from common.gates import all_pass as _all_pass                        # noqa: E402
from common.obs import get_logger                                     # noqa: E402

log = get_logger("web_testing")


DEFAULT_TIMEOUT_MS = 15000
SHOTS_DIR = "web_shots"

# ---------------------------------------------------------------------------
# 方法论约束（与 agent-skills/web-automation 及基座引擎保持一致）
# ---------------------------------------------------------------------------
# 脆弱定位器：编码了 DOM 位置或用 XPath，布局微调即碎 → 产出假红（噪音）
_BRITTLE_SELECTOR_PATTERNS = re.compile(
    r"""
    (^/)                           # XPath starting with /
    | (/{2})                       # XPath descendant //
    | (xpath=)                     # 显式 xpath= 前缀
    | (:nth-child\s*\()            # :nth-child pseudo
    | (:nth-of-type\s*\()          # :nth-of-type pseudo — structural position
    | (>\s*(?:div|span|li|ul|ol|td|tr|th)[\s>+~,\[{$])  # bare positional div/span chains
    """,
    re.VERBOSE,
)

_RESILIENT_SELECTOR_HINTS = (
    "建议改用下列之一（按稳定性排序）：\n"
    "  1. data-test-subj  → [data-test-subj='myButton']\n"
    "  2. aria-label      → [aria-label='Search bar']\n"
    "  3. 可见文本        → button:has-text('保存'), text='应用'\n"
    "  4. 语义角色        → role=button[name='提交']"
)

# 连接级错误特征：属于「环境不可达」而非产品缺陷
_CONN_ERROR_SIGNS = (
    "err_connection_refused", "err_name_not_resolved", "err_connection_timed_out",
    "err_address_unreachable", "err_connection_reset", "err_connection_closed",
    "err_empty_response", "err_internet_disconnected", "err_network_changed",
    "econnrefused", "getaddrinfo", "net::err_",
)

# 步骤分类
_ACTIONS = ("goto", "fill", "click", "press", "select_option", "check", "uncheck",
            "hover", "wait_for", "wait_for_hidden", "wait_for_url", "scroll_into_view",
            "screenshot")
_ASSERTS = ("expect_visible", "expect_hidden", "expect_text", "expect_value",
            "expect_count", "expect_url", "expect_enabled", "expect_disabled")
_KNOWN_STEPS = _ACTIONS + _ASSERTS


def _is_brittle_selector(selector: str) -> bool:
    return bool(selector) and bool(_BRITTLE_SELECTOR_PATTERNS.search(selector))


# ---------------------------------------------------------------------------
# 步骤解析与校验
# ---------------------------------------------------------------------------
def _normalize_steps(raw_steps: Any) -> List[Dict[str, Any]]:
    """把 YAML 里的步骤统一成 {action, params, raw} 列表。

    支持两种写法：
      - click: "[data-test-subj='x']"                  （单值简写）
      - fill: {selector: "...", value: "..."}          （参数对象）
    """
    out: List[Dict[str, Any]] = []
    for i, item in enumerate(raw_steps or []):
        if isinstance(item, str):
            out.append({"action": item, "params": {}, "raw": item, "_idx": i})
            continue
        if not isinstance(item, dict):
            out.append({"action": "", "params": {}, "raw": item, "_idx": i,
                        "_bad": f"第 {i + 1} 步不是对象"})
            continue
        keys = [k for k in item.keys()]
        if len(keys) != 1:
            out.append({"action": keys[0] if keys else "", "params": {}, "raw": item,
                        "_idx": i,
                        "_bad": f"第 {i + 1} 步必须只含一个动作键，实际有 {keys}"})
            continue
        action = keys[0]
        val = item[action]
        if isinstance(val, dict):
            params = dict(val)
            # expect_visible: "[sel]" 与 expect_visible: {selector: "..."} 等价
        else:
            params = {"_value": val}
        out.append({"action": action, "params": params, "raw": item, "_idx": i})
    return out


def _step_selector(step: Dict[str, Any]) -> str:
    p = step["params"]
    if "selector" in p:
        return str(p["selector"])
    if "_value" in p and step["action"] not in ("goto", "screenshot", "wait_for_url", "expect_url"):
        return str(p["_value"])
    return ""


def _step_target_desc(step: Dict[str, Any]) -> str:
    s = _step_selector(step)
    if s:
        return s
    p = step["params"]
    return str(p.get("_value", p.get("url", p.get("path", ""))))


def _issues_for(sc: Dict[str, Any], auth: Dict[str, Any]) -> List[Tuple[str, str]]:
    """校验单个场景，返回 [(kind, message)]。

    kind 分两级（决定处置方式不同，不能混为一谈）：
      - "config"：确定性配置错误（脆弱定位器 / 未知步骤 / 缺必填参数）。
        这类错误会让执行结果毫无意义，而且失败原因在配置而非产品 →
        **拦住不执行**，否则会把配置笔误当成"产品缺陷"报出去（双重归因）。
      - "gate"：门禁有效性问题（场景没有任何断言）。
        不是错误，但这样的场景"永远通过"、无法提供保护 → 仍然执行（让用户看到步骤确实走通），
        只是结果记 SKIP、门禁不判绿。
    """
    issues: List[Tuple[str, str]] = []
    name = sc.get("name") or "(未命名场景)"
    steps = _normalize_steps(sc.get("steps"))
    if not steps:
        issues.append(("config", "没有任何步骤"))
        return issues
    n_asserts = 0
    for st in steps:
        idx = st["_idx"] + 1
        if st.get("_bad"):
            issues.append(("config", st["_bad"]))
            continue
        act = st["action"]
        if act not in _KNOWN_STEPS:
            issues.append(("config",
                           f"第 {idx} 步：未知动作 `{act}`（支持：{', '.join(_KNOWN_STEPS)}）"))
            continue
        sel = _step_selector(st)
        if sel and _is_brittle_selector(sel):
            issues.append(("config",
                           f"第 {idx} 步（{act}）：脆弱定位器 `{sel}`\n{_RESILIENT_SELECTOR_HINTS}"))
        p = st["params"]
        if act == "fill" and not (sel and ("value" in p or "_value" in p)):
            issues.append(("config", f"第 {idx} 步：fill 需要 selector 与 value"))
        if act == "goto" and not (p.get("_value") or p.get("url") or p.get("path")):
            issues.append(("config", f"第 {idx} 步：goto 需要路径或 URL"))
        if act in ("wait_for_url", "expect_url") and not (
                p.get("_value") or p.get("pattern") or p.get("url")):
            issues.append(("config", f"第 {idx} 步：{act} 需要 URL 模式"))
        if act == "expect_text" and not (p.get("contains") or p.get("equals") or p.get("matches")):
            issues.append(("config", f"第 {idx} 步：expect_text 需要 contains / equals / matches 之一"))
        if act == "expect_count" and not any(k in p for k in ("equals", "min", "max")):
            issues.append(("config", f"第 {idx} 步：expect_count 需要 equals 或 min/max"))
        if act in _ASSERTS:
            n_asserts += 1
    if n_asserts == 0:
        issues.append(("gate", "没有任何 expect_* 断言步骤：无法判定对错，"
                               "本场景不计为通过（防止『点了就走、永远通过』的假绿场景）"))
    return issues


def validate_scenario(sc: Dict[str, Any], auth: Dict[str, Any]) -> List[str]:
    """校验场景配置，返回带场景名与级别前缀的问题列表（空表示无误）。"""
    name = sc.get("name") or "(未命名场景)"
    return [f"[{name}][{kind}] {msg}" for kind, msg in _issues_for(sc, auth)]


def _has_blocking_issue(sc: Dict[str, Any], auth: Dict[str, Any]) -> List[str]:
    """该场景是否存在会导致"执行无意义"的配置错误（kind == config）。"""
    return [msg for kind, msg in _issues_for(sc, auth) if kind == "config"]


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
class StepError(Exception):
    """步骤执行失败（区分连接级问题，供上层归类）。"""

    def __init__(self, message: str, connection: bool = False):
        super().__init__(message)
        self.connection = connection


def _looks_like_connection_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(s in msg for s in _CONN_ERROR_SIGNS)


def _abs_url(base_url: str, path: str) -> str:
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", path or ""):
        return path
    return base_url.rstrip("/") + "/" + (path or "").lstrip("/")


def _run_step(page: Any, expect: Any, step: Dict[str, Any], base_url: str,
              timeout_ms: int, shots_dir: Path, auth: Dict[str, Any]) -> str:
    """执行单个步骤，失败抛 StepError。返回成功说明文本。"""
    act = step["action"]
    p = _substitute(step["params"], auth)
    sel = _step_selector(step)
    low: Dict[str, Any] = {k: (v.lower() if isinstance(v, str) else v) for k, v in p.items()}
    loc = page.locator(sel) if sel else None

    def _need(key: str) -> Any:
        if key in p:
            return p[key]
        if key == "selector" and "_value" in p:
            return p["_value"]
        raise StepError(f"缺少参数 `{key}`")

    if act == "goto":
        url = _abs_url(base_url, str(p.get("_value") or p.get("url") or p.get("path") or "/"))
        resp = page.goto(url, timeout=timeout_ms, wait_until=low.get("wait_until", "domcontentloaded"))
        if resp is not None and resp.status >= 400:
            raise StepError(
                f"页面返回 HTTP {resp.status}（{url}）"
                "；若你确信地址与路由正确，这属于应用侧错误页，请检查服务日志")
        return f"HTTP {resp.status if resp else '—'} · {url}"

    if act == "fill":
        loc.fill(str(_need("value")), timeout=timeout_ms)
        return f"已填入 {sel}"
    if act == "click":
        loc.click(timeout=timeout_ms)
        return f"已点击 {sel}"
    if act == "press":
        loc.press(str(p.get("key") or "Enter"), timeout=timeout_ms)
        return f"按键 {p.get('key') or 'Enter'} @ {sel}"
    if act == "select_option":
        loc.select_option(str(_need("value")), timeout=timeout_ms)
        return f"已选择 {p.get('value')}"
    if act == "check":
        loc.check(timeout=timeout_ms)
        return f"已勾选 {sel}"
    if act == "uncheck":
        loc.uncheck(timeout=timeout_ms)
        return f"已取消勾选 {sel}"
    if act == "hover":
        loc.hover(timeout=timeout_ms)
        return f"已悬停 {sel}"
    if act == "scroll_into_view":
        loc.scroll_into_view_if_needed(timeout=timeout_ms)
        return f"已滚动到 {sel}"
    if act in ("wait_for", "wait_for_hidden"):
        state = "hidden" if act == "wait_for_hidden" else "visible"
        page.wait_for_selector(sel, state=state, timeout=timeout_ms)
        return f"{sel} 已{('隐藏' if state == 'hidden' else '可见')}"
    if act == "wait_for_url":
        pattern = str(p.get("_value") or p.get("pattern") or p.get("url"))
        page.wait_for_url(pattern, timeout=timeout_ms)
        return f"URL 已匹配 {pattern}"
    if act == "screenshot":
        nm = str(p.get("_value") or p.get("name") or f"step{step['_idx'] + 1}")
        f = shots_dir / f"{nm}.png"
        page.screenshot(path=str(f), full_page=bool(p.get("full_page")))
        return f"截图 {f.name}"

    # ---- 断言（web-first 自动重试，不用固定 sleep）----
    if act == "expect_visible":
        expect(loc).to_be_visible(timeout=timeout_ms)
        return f"{sel} 可见"
    if act == "expect_hidden":
        expect(loc).to_be_hidden(timeout=timeout_ms)
        return f"{sel} 隐藏"
    if act == "expect_text":
        if "contains" in p:
            expect(loc).to_contain_text(str(p["contains"]), timeout=timeout_ms)
            return f"{sel} 含文本 {p['contains']!r}"
        if "equals" in p:
            expect(loc).to_have_text(str(p["equals"]), timeout=timeout_ms)
            return f"{sel} 文本等于 {p['equals']!r}"
        expect(loc).to_have_text(re.compile(str(p["matches"])), timeout=timeout_ms)
        return f"{sel} 文本匹配 /{p['matches']}/"
    if act == "expect_value":
        want = p.get("equals", p.get("value"))
        expect(loc).to_have_value(str(want), timeout=timeout_ms)
        return f"{sel} 值等于 {want!r}"
    if act == "expect_count":
        if "equals" in p:
            expect(loc).to_have_count(int(p["equals"]), timeout=timeout_ms)
            return f"{sel} 数量等于 {p['equals']}"
        # min / max：条件轮询（仍是显式等待，不是 sleep）
        lo = int(p.get("min", 0))
        hi = int(p.get("max", 10 ** 9))
        deadline = time.time() + timeout_ms / 1000.0
        got = loc.count()
        while time.time() < deadline:
            got = loc.count()
            if lo <= got <= hi:
                return f"{sel} 数量 {got} ∈ [{lo}, {hi}]"
            time.sleep(0.15)
        raise StepError(f"{sel} 数量 {got} 不在 [{lo}, {hi}]（等待 {timeout_ms}ms）")
    if act == "expect_url":
        pattern = str(p.get("_value") or p.get("pattern") or p.get("url"))
        expect(page).to_have_url(pattern, timeout=timeout_ms)
        return f"URL 匹配 {pattern}"
    if act == "expect_enabled":
        expect(loc).to_be_enabled(timeout=timeout_ms)
        return f"{sel} 可用"
    if act == "expect_disabled":
        expect(loc).to_be_disabled(timeout=timeout_ms)
        return f"{sel} 禁用"
    raise StepError(f"未知动作 `{act}`")


def _slug(s: str) -> str:
    v = re.sub(r"[^\w\u4e00-\u9fa5-]+", "_", str(s)).strip("_")
    return v[:60] or "scenario"


def _repro_ts_spec(sc: Dict[str, Any], base_url: str, auth: Dict[str, Any],
                   failed_step: Optional[Dict[str, Any]]) -> str:
    """把失败场景导出为可复现的 Playwright `.spec.ts`（对应基座 generate_playwright_spec）。"""
    def js(s: Any) -> str:
        return json.dumps(s, ensure_ascii=False)

    lines = ["// 由软件测试智能体自动生成：失败场景的可复现脚本",
             "// 直接运行：npx playwright test <本文件>",
             "import { test, expect } from '@playwright/test';",
             "",
             f"test({js(sc.get('name') or 'scenario')}, async ({{ page }}) => {{"]
    for st in _normalize_steps(sc.get("steps")):
        act = st["action"]
        p = _substitute(st["params"], auth)
        sel = _step_selector(st)
        v = p.get("_value", p.get("value"))
        if act == "goto":
            lines.append(f"  await page.goto({js(_abs_url(base_url, str(v or '/')))});")
        elif act in _ACTIONS and sel:
            fn = {"fill": f"fill({js(str(v))})", "click": "click()", "check": "check()",
                  "uncheck": "uncheck()", "hover": "hover()",
                  "scroll_into_view": "scrollIntoViewIfNeeded()",
                  "wait_for": "waitFor({ state: 'visible' })",
                  "wait_for_hidden": "waitFor({ state: 'hidden' })"}.get(act)
            if fn:
                lines.append(f"  await page.locator({js(sel)}).{fn};")
        elif act == "press":
            lines.append(f"  await page.locator({js(sel)}).press({js(str(p.get('key') or 'Enter'))});")
        elif act == "select_option":
            lines.append(f"  await page.locator({js(sel)}).selectOption({js(str(v))});")
        elif act == "wait_for_url":
            lines.append(f"  await page.waitForURL({js(str(p.get('_value') or p.get('pattern') or p.get('url')))});")
        elif act == "expect_visible":
            lines.append(f"  await expect(page.locator({js(sel)})).toBeVisible();")
        elif act == "expect_hidden":
            lines.append(f"  await expect(page.locator({js(sel)})).toBeHidden();")
        elif act == "expect_text":
            if "contains" in p:
                lines.append(f"  await expect(page.locator({js(sel)})).toContainText({js(str(p['contains']))});")
            elif "equals" in p:
                lines.append(f"  await expect(page.locator({js(sel)})).toHaveText({js(str(p['equals']))});")
            else:
                lines.append(f"  await expect(page.locator({js(sel)})).toHaveText(/{p['matches']}/);")
        elif act == "expect_value":
            lines.append(f"  await expect(page.locator({js(sel)})).toHaveValue({js(str(p.get('equals', p.get('value'))))});")
        elif act == "expect_count":
            if "equals" in p:
                lines.append(f"  await expect(page.locator({js(sel)})).toHaveCount({int(p['equals'])});")
            else:
                lines.append(f"  // 数量范围断言 min={p.get('min')} max={p.get('max')}"
                             f"（Playwright 无内置范围断言，可用 toHaveCount 或自定义轮询）")
        elif act == "expect_url":
            lines.append(f"  await expect(page).toHaveURL({js(str(p.get('_value') or p.get('pattern') or p.get('url')))});")
        elif act == "expect_enabled":
            lines.append(f"  await expect(page.locator({js(sel)})).toBeEnabled();")
        elif act == "expect_disabled":
            lines.append(f"  await expect(page.locator({js(sel)})).toBeDisabled();")
    if failed_step:
        lines.append("")
        lines.append(f"  // ⚠ 本次执行在「{failed_step.get('action')} "
                     f"{_step_target_desc(failed_step)}」处失败，请重点核对上方对应行")
    lines.append("});")
    return "\n".join(lines) + "\n"


def _run_scenario(ctx: Dict[str, Any], sc: Dict[str, Any],
                  cfg: Dict[str, Any]) -> Dict[str, Any]:
    """执行单个场景（含整场重试），返回结果记录。"""
    page = ctx["page"]
    expect = ctx["expect"]
    base_url = cfg["base_url"]
    timeout_ms = cfg["timeout_ms"]
    shots_dir = ctx["shots_dir"]
    auth = ctx["auth"]
    steps = _normalize_steps(sc.get("steps"))
    n_asserts = sum(1 for s in steps if s["action"] in _ASSERTS)
    name = sc.get("name") or "(未命名场景)"
    started = time.time()

    def _attempt() -> Tuple[bool, str, List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        records: List[Dict[str, Any]] = []
        for st in steps:
            t0 = time.time()
            rec: Dict[str, Any] = {"index": st["_idx"] + 1, "action": st["action"],
                                   "target": _step_target_desc(st),
                                   "is_assertion": st["action"] in _ASSERTS}
            try:
                detail = _run_step(page, expect, st, base_url, timeout_ms, shots_dir, auth)
                rec.update(status="PASS", detail=detail, ms=int((time.time() - t0) * 1000))
            except Exception as e:
                conn = _looks_like_connection_error(e)
                rec.update(status="FAIL", detail=str(e).splitlines()[0][:300],
                           ms=int((time.time() - t0) * 1000),
                           connection_error=conn)
                records.append(rec)
                return False, str(e), records, st
            records.append(rec)
        return True, "", records, None

    attempts = 0
    ok, err, records, failed_step = _attempt()
    attempts = 1
    retries = int(cfg.get("retries") or 0)
    while not ok and attempts <= retries:
        attempts += 1
        ok, err, records, failed_step = _attempt()

    # 无断言 → 即便全跑通也不判绿（假绿防护）
    if ok and n_asserts == 0:
        return {"name": name, "tags": list(sc.get("tags") or []), "result": "SKIP",
                "reason": "场景没有任何 expect_* 断言，无法判定对错（不计为通过）",
                "assertions": 0, "attempts": attempts, "flaky": False,
                "duration_ms": int((time.time() - started) * 1000),
                "failed_step": None, "screenshots": [], "repro": "",
                "connection_error": False, "steps": records}

    flaky = bool(ok and attempts > 1)
    conn_err = bool(not ok and any(r.get("connection_error")
                                   for r in records if r["status"] == "FAIL"))
    if ok:
        result = "PASS"
        reason = "重试后通过（首次失败，存在抖动）" if flaky else ""
    elif conn_err:
        # 连接级错误 = 环境不可达，不是产品缺陷 → SKIP（门禁照样不判绿）。
        # 把连接错误报成 FAIL，会让"服务没起"退化成"一堆用例失败"，把人引向错误的排查方向。
        result = "SKIP"
        reason = (f"连接级错误，按环境不可达处理：{err.splitlines()[0][:200]}"
                  "（若服务本应在线，请检查是否中途崩溃或网络抖动）")
    else:
        result = "FAIL"
        reason = err.splitlines()[0][:300]

    shots: List[str] = []
    repro = ""
    if result == "FAIL" and cfg.get("screenshot_on_failure", True):
        try:
            f = shots_dir / f"{_slug(name)}-FAIL.png"
            page.screenshot(path=str(f), full_page=True)
            shots.append(str(f.relative_to(shots_dir.parent.parent)))
        except Exception as e:
            # 必须出声：截图是失败证据链的一环。没存下来却不说，"报告里没有截图"
            # 就会被读成"这次没有失败"。
            log.warning("  [Web 冒烟] 失败截图保存失败：%s", e)
        try:
            rp = shots_dir.parent / f"web_repro_{_slug(name)}.spec.ts"
            rp.write_text(_repro_ts_spec(sc, base_url, auth, failed_step), encoding="utf-8")
            repro = str(rp.relative_to(shots_dir.parent.parent))
        except Exception as e:
            # 必须出声：复现脚本是"把失败交出去"的载体，写不出来要让排障的人知道。
            log.warning("  [Web 冒烟] 复现脚本写入失败：%s", e)

    return {"name": name, "tags": list(sc.get("tags") or []), "result": result,
            "reason": reason,
            "assertions": n_asserts, "attempts": attempts, "flaky": flaky,
            "connection_error": conn_err,
            "duration_ms": int((time.time() - started) * 1000),
            "failed_step": ({"index": failed_step["_idx"] + 1,
                             "action": failed_step["action"],
                             "target": _step_target_desc(failed_step)} if failed_step else None),
            "screenshots": shots, "repro": repro, "steps": records}


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def _web_config(project: Dict[str, Any], web: Dict[str, Any],
                args: Optional[argparse.Namespace]) -> Dict[str, Any]:
    env = project.get("env", {}) or {}
    w = web.get("web", web) or {}
    base = (w.get("base_url") or env.get("web_base_url") or env.get("base_url") or "").strip()
    vp = w.get("viewport") or {}
    return {
        "base_url": base,
        "browser": (getattr(args, "browser", None) or w.get("browser") or "chromium"),
        "headless": (False if getattr(args, "headed", False)
                     else bool(w.get("headless", True))),
        "timeout_ms": int(w.get("timeout_ms") or DEFAULT_TIMEOUT_MS),
        "retries": int(w.get("retries") or 0),
        "slow_mo": int(w.get("slow_mo") or 0),
        "viewport": {"width": int(vp.get("width") or 1440),
                     "height": int(vp.get("height") or 900)},
        "launch_args": list(w.get("launch_args") or []),
        "screenshot_on_failure": bool(w.get("screenshot_on_failure", True)),
    }


def _scenarios(web: Dict[str, Any], only: Optional[str]) -> List[Dict[str, Any]]:
    w = web.get("web", web) or {}
    items = list(w.get("scenarios") or [])
    if only:
        items = [s for s in items if str(s.get("name", "")) == only or
                 only in (s.get("tags") or [])]
    return items


def _summarize(scenarios: List[Dict[str, Any]], issues: List[str],
               baseline_ok: bool) -> Tuple[bool, str]:
    passed = sum(1 for s in scenarios if s["result"] == "PASS")
    failed = sum(1 for s in scenarios if s["result"] == "FAIL")
    skipped = sum(1 for s in scenarios if s["result"] == "SKIP")
    flaky = sum(1 for s in scenarios if s.get("flaky"))
    conn = sum(1 for s in scenarios if s.get("connection_error"))
    parts: List[str] = []
    if not baseline_ok:
        return False, "环境不可达（或浏览器无法启动），全部场景跳过，按未通过处理（防 CI 假绿）"
    parts.append(f"场景 {passed} 通过 / {failed} 失败 / {skipped} 跳过")
    if flaky:
        parts.append(f"{flaky} 个抖动（重试后通过）")
    if conn:
        parts.append(f"{conn} 个场景因连接级错误跳过，请先确认目标服务已启动")
    if issues:
        parts.append(f"{len(issues)} 项配置/门禁问题待修（门禁不通过）")
    ok = _all_pass(failed, passed) and not issues     # 谓词见 common.gates.all_pass（唯一）
    if not ok and not parts:
        parts.append("无有效场景")
    if passed == 0 and not issues and failed == 0:
        parts.append("没有任何实际执行的通过场景")
    return ok, " · ".join(parts)


def _clean_previous_evidence(shots_dir: Path, art_dir: Path) -> int:
    """清理上一次运行留下的截图与可复现脚本，返回清理数量。

    为什么必须清：截图/脚本是"失败证据"。若不清，上一轮的残留会留在目录里，
    被当成本次运行的证据（排查时误导极大——明明本次通过了，却看到一堆 FAIL 截图）。
    只删本工具自己产出的两类文件（`web_shots/*.png` 与 `web_repro_*.spec.ts`），
    不做任何目录级删除。
    """
    removed = 0
    if shots_dir.is_dir():
        for f in shots_dir.glob("*.png"):
            try:
                f.unlink()
                removed += 1
            except OSError as e:
                # 必须出声：清不掉旧证据 → 上一次的 FAIL 截图会被当成本次的证据，
                # 排查时误导极大（本函数存在的全部理由就是这个）。
                log.warning("  [Web 冒烟] 旧截图删除失败（可能残留为下次证据）：%s", e)
    for f in art_dir.glob("web_repro_*.spec.ts"):
        try:
            f.unlink()
            removed += 1
        except OSError as e:
            log.warning("  [Web 冒烟] 旧复现脚本删除失败（可能残留为下次证据）：%s", e)
    return removed


def run_web(project_path: Path,
            web_path: Optional[Path] = None,
            out_json: Optional[Path] = None,
            only: Optional[str] = None,
            args: Optional[argparse.Namespace] = None) -> Dict[str, Any]:
    """执行 Web UI 冒烟。"""
    _load_dotenv()   # 独立运行时必须自行载入 .env（凭据来自 .env，密钥分离）
    try:
        from playwright.sync_api import sync_playwright, expect as pw_expect
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("需要 Playwright：pip install playwright && playwright install chromium") from e

    project_path = Path(project_path)
    project = _load_yaml(project_path)
    web_path = Path(web_path) if web_path else (project_path.parent / "web.yaml")
    if not web_path.is_file():
        raise RuntimeError(f"未找到 Web 场景文件：{web_path}（需在项目目录下创建 web.yaml）")
    web = _load_yaml(web_path)
    auth = _resolve_auth(project)
    cfg = _web_config(project, web, args)
    scenarios_raw = _scenarios(web, only)
    art = project_path.parent / "artifacts"
    art.mkdir(parents=True, exist_ok=True)
    shots_dir = art / SHOTS_DIR
    shots_dir.mkdir(parents=True, exist_ok=True)
    stale = _clean_previous_evidence(shots_dir, art)
    if stale:
        print(f"  [清理] 已移除上次运行的 {stale} 个证据文件（截图/可复现脚本）")

    print(f"  [Web 冒烟] 目标 {cfg['base_url'] or '(未配置)'} · 浏览器 {cfg['browser']}"
          f" · {'无头' if cfg['headless'] else '有头'} · 超时 {cfg['timeout_ms']}ms"
          + (f" · 重试 {cfg['retries']}" if cfg["retries"] else ""))

    # 配置校验：先分流。kind=config 的错误场景**不执行**（否则会把配置笔误当产品缺陷报出去）
    issues: List[str] = []
    blocked: Dict[str, List[str]] = {}
    for sc in scenarios_raw:
        items = _issues_for(sc, auth)
        sc_name = str(sc.get("name") or "(未命名场景)")
        for kind, msg in items:
            label = "配置问题" if kind == "config" else "门禁有效性"
            issues.append(f"[{sc_name}][{kind}] {msg}")
            print(f"  [{label}] [{sc_name}] {msg.splitlines()[0]}")
        if any(kind == "config" for kind, _ in items):
            blocked[sc_name] = [m for k, m in items if k == "config"]

    if not scenarios_raw:
        msg = (f"没有匹配的场景（only={only!r}）" if only else "web.yaml 未声明任何场景")
        # 必须出声：没有场景就没有断言、没有断言不算绿 —— 但用户若只见"流程完成"
        # 就会误以为 Web 那一道过了。
        log.error("  [Web 冒烟] %s", msg)

    result: Dict[str, Any] = {
        "project_id": project.get("project_id") or project_path.parent.name,
        "base_url": cfg["base_url"], "browser": cfg["browser"],
        "headless": cfg["headless"],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "config_issues": issues,
        "baseline": {"ok": False, "reason": ""},
        "scenarios": [], "total": 0, "passed": 0, "failed": 0, "skipped": 0,
        "flaky": 0, "assertions": 0, "all_pass": False, "summary": "",
    }
    if not scenarios_raw:
        result["summary"] = "没有可执行的场景"
        if out_json:
            Path(out_json).parent.mkdir(parents=True, exist_ok=True)
            Path(out_json).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
            print(f"  结果已写出 {out_json}")
        print(f"\n  == Web UI 冒烟：❌ 未通过 ==\n  {result['summary']}")
        return result

    if not cfg["base_url"]:
        reason = "未配置 Web 地址：请在 web.yaml 的 web.base_url 或 project.yaml 的 env.web_base_url 指定"
        result["baseline"] = {"ok": False, "reason": reason}
        result["scenarios"] = [{"name": sc.get("name"), "result": "SKIP", "reason": reason,
                                "tags": list(sc.get("tags") or []), "assertions": 0,
                                "attempts": 0, "flaky": False, "duration_ms": 0,
                                "failed_step": None, "screenshots": [], "steps": []}
                               for sc in scenarios_raw]
        result["total"] = len(result["scenarios"])
        result["skipped"] = result["total"]
        _, result["summary"] = _summarize(result["scenarios"], issues, False)
        print(f"  [基线] ⚠ {reason}")
        if out_json:
            Path(out_json).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
        return result

    baseline_ok = False
    baseline_reason = ""
    browser_args = cfg["launch_args"]
    with sync_playwright() as pw:
        try:
            browser = getattr(pw, cfg["browser"]).launch(
                headless=cfg["headless"], slow_mo=cfg["slow_mo"],
                args=browser_args or None,
            )
        except Exception as e:
            baseline_reason = (f"浏览器启动失败（{str(e).splitlines()[0][:200]}）；"
                               "如需依赖系统浏览器/容器环境，请在 web.yaml 里配 launch_args，"
                               "例如 ['--no-sandbox','--disable-gpu','--disable-dev-shm-usage']")
            log.warning("  [基线] ⚠ %s", baseline_reason)
            result["baseline"] = {"ok": False, "reason": baseline_reason}
            result["scenarios"] = [{"name": sc.get("name"), "result": "SKIP",
                                    "reason": baseline_reason, "tags": list(sc.get("tags") or []),
                                    "assertions": 0, "attempts": 0, "flaky": False,
                                    "duration_ms": 0, "failed_step": None,
                                    "screenshots": [], "steps": []} for sc in scenarios_raw]
            result["total"] = len(result["scenarios"])
            result["skipped"] = result["total"]
            _, result["summary"] = _summarize(result["scenarios"], issues, False)
            if out_json:
                Path(out_json).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
            return result

        try:
            context = browser.new_context(viewport=cfg["viewport"])
            page = context.new_page()
            page.set_default_timeout(cfg["timeout_ms"])
            # 基线：只把「连接级失败」判为环境不可达；HTTP 4xx/5xx 交给场景断言去报
            try:
                resp = page.goto(cfg["base_url"], timeout=cfg["timeout_ms"],
                                 wait_until="domcontentloaded")
                baseline_ok = True
                baseline_reason = (f"可达（HTTP {resp.status if resp else '—'}，"
                                   f"title={page.title()[:40]!r}）")
            except Exception as e:
                if _looks_like_connection_error(e):
                    baseline_reason = (f"环境不可达：{str(e).splitlines()[0][:200]}"
                                       "（请确认目标服务已启动且地址/端口正确）")
                else:
                    baseline_reason = f"基线访问异常：{str(e).splitlines()[0][:200]}"
            print(f"  [基线] {'✅' if baseline_ok else '⚠'} {baseline_reason}")
            result["baseline"] = {"ok": baseline_ok, "reason": baseline_reason}

            ctx = {"page": page, "expect": pw_expect, "shots_dir": shots_dir, "auth": auth}
            for sc in scenarios_raw:
                sc_name = str(sc.get("name") or "(未命名场景)")
                if not baseline_ok:
                    # 基线不通过 → 不逐个执行：否则"服务没起"会退化成"每个场景都失败"，
                    # 报告里全是 FAIL，反而看不出真正原因是环境没起（把排查带偏）。
                    rec = {"name": sc_name, "tags": list(sc.get("tags") or []),
                           "result": "SKIP", "assertions": 0, "attempts": 0, "flaky": False,
                           "connection_error": True, "duration_ms": 0,
                           "reason": f"未执行：{baseline_reason}", "failed_step": None,
                           "screenshots": [], "repro": "", "steps": []}
                elif sc_name in blocked:
                    # 配置错误 → 不执行。失败原因在配置，报成产品缺陷是误导。
                    rec = {"name": sc_name, "tags": list(sc.get("tags") or []),
                           "result": "SKIP", "assertions": 0, "attempts": 0, "flaky": False,
                           "connection_error": False, "duration_ms": 0,
                           "reason": "配置错误，未执行：" + "；".join(
                               m.splitlines()[0] for m in blocked[sc_name]),
                           "failed_step": None, "screenshots": [], "repro": "", "steps": []}
                else:
                    rec = _run_scenario(ctx, sc, cfg)
                result["scenarios"].append(rec)
                mark = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭"}.get(rec["result"], "?")
                line = (f"  [{rec['result']}] {mark} {rec['name']} "
                        f"（{rec['assertions']} 断言 / {rec['duration_ms']}ms"
                        + (f" / 重试 {rec['attempts']} 次" if rec["attempts"] > 1 else "") + "）")
                print(line)
                if rec["result"] != "PASS" and rec.get("reason"):
                    print(f"        {rec['reason']}")
                if rec.get("flaky"):
                    print(f"        ⚠ 抖动：首次失败、重试后通过")
                if rec.get("screenshots"):
                    print(f"        截图：{', '.join(Path(s).name for s in rec['screenshots'])}")
                if rec.get("repro"):
                    print(f"        可复现脚本：{rec['repro']}")
        finally:
            try:
                browser.close()
            except Exception:
                pass  # 可忽略：收尾关浏览器，进程随即退出；失败不影响任何门禁结论

    result["total"] = len(result["scenarios"])
    result["passed"] = sum(1 for s in result["scenarios"] if s["result"] == "PASS")
    result["failed"] = sum(1 for s in result["scenarios"] if s["result"] == "FAIL")
    result["skipped"] = sum(1 for s in result["scenarios"] if s["result"] == "SKIP")
    result["flaky"] = sum(1 for s in result["scenarios"] if s.get("flaky"))
    result["assertions"] = sum(int(s.get("assertions") or 0) for s in result["scenarios"])
    ok, summary = _summarize(result["scenarios"], issues, baseline_ok)
    result["all_pass"], result["summary"] = ok, summary

    if out_json:
        Path(out_json).parent.mkdir(parents=True, exist_ok=True)
        Path(out_json).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"  结果已写出 {out_json}")
    print(f"\n  == Web UI 冒烟：{'✅ 通过' if ok else '❌ 未通过'} ==\n  {summary}")
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Web UI 冒烟执行器（Playwright + 声明式 YAML）")
    ap.add_argument("--project", required=True, help="project.yaml 路径")
    ap.add_argument("--web", help="web.yaml 路径（默认同目录 web.yaml）")
    ap.add_argument("--json", help="输出 JSON 结果路径")
    ap.add_argument("--only", help="只跑指定场景名（或标签）")
    ap.add_argument("--headed", action="store_true", help="有头模式（便于排查）")
    ap.add_argument("--browser", choices=["chromium", "firefox", "webkit"])
    args = ap.parse_args()
    res = run_web(Path(args.project), Path(args.web) if args.web else None,
                  Path(args.json) if args.json else None, only=args.only, args=args)
    sys.exit(0 if res.get("all_pass") else 1)


if __name__ == "__main__":
    main()
