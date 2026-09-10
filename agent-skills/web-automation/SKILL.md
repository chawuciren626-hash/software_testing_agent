---
name: web-automation
description: Web 自动化测试方法论（Selenium / Playwright / PO 模式）。覆盖元素定位、等待策略、PageObject、稳定性与自愈、可视化校验。供探索智能体在 Web 探索与生成 Playwright 用例时调用。
---

# Web 自动化技能

> 项目「已配置的 web-automation / playwright 技能」镜像封装。

## 要点
- **定位策略**：优先 `data-test-subj → aria-label → 可见文本`；避免 XPath / 位置选择器（基座引擎运行时拒绝）。
- **等待**：显式等待代替固定 sleep；处理动态加载与异步请求。
- **PageObject 模式**：页面对象封装定位与操作，用例与实现解耦。
- **稳定性/自愈**：失败重试、错误以自然语言回传、Adapter 记录 Action Tape 生成可复现脚本。
- **可视化校验**：用视觉模型比对渲染异常（基座 `analyze_visual_state` 工具）。

## 与基座的关系
- 基座已用 Playwright 驱动浏览器，本技能提供方法论约束（选型/等待/PO）。
- 生成的 `reproduction_*.spec.ts` 可直接 `npx playwright test` 复现。

## 常用接口（基座）
- `execute_browser_command`：智能体发 JSON 意图 → 引擎执行 → 记录 Action Tape。
- `capture_bug_screenshot`：发现缺陷时留证。
- `analyze_visual_state`：视觉异常校验。
