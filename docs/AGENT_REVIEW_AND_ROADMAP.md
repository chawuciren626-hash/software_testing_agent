# 软件测试智能体 · 审阅与下一步规划

> 依据「智能体开发」技能方法论（场景立项 → 能力拆解 → 角色设定 → 工具装配 → 记忆与状态 → 评测上线），结合本仓库 `D:\WorkBuddy\2026-09-09-16-35-03` 代码现状，于 2026-09-10 完成一次系统审阅。
> 结论先行：**当前是一个"测试流水线编排工具"，尚未成为"自主智能体"**——LLM 多智能体层仍停留在基座 `src/agentic_explorer/` 的休眠代码与文档中，运行态代码无 langgraph/langchain 依赖（打包时甚至被显式排除）。

---

## 一、现状审阅（事实核查）

### 1.1 架构现状（运行态）

```
用户
 ├─ CLI 入口   project_manager.py (834行, 9子命令: create/list/info/run/regression/rerun/dashboard/cases/files)
 │             └─ 纯 Python 编排，无 LLM 调用
 ├─ 流水线入口 software_testing_agent.py (脚本式 3 步: 需求→用例→[接口]→报告)
 └─ Web 控制台 web_console/
       ├─ app.py (Flask, 127.0.0.1:8765, debug=False, 无鉴权) 薄封装调用 project_manager 子进程
       ├─ run_store.py (SQLite: runs 任务历史 + snapshots 回归快照)
       └─ templates/index.html (1550行原生JS 单页, 6导航页)

extensions/ (确定性能力层, 已落地)
 ├─ api_testing/         pytest 接口自动化 (mall-admin, 不依赖 LLM)
 ├─ requirements_to_cases/  需求→用例 (默认规则版; _llm_generate 为可选增强, 需 key+基座依赖)
 ├─ regression/         核心场景回归 (expect_json 门禁语义)
 ├─ reporting/          报告聚合 + 钉钉/邮件通知
 └─ perf_security/      Locust 压测 + 安全 mission 模板

agent-skills/ (5个 BYO 技能: api-test-design / qa-test-design / requirements-to-cases / software-testing / web-automation)
src/agentic_explorer/ (基座 LangGraph Supervisor 代码 —— 运行态未 import, 休眠)
tests/ (11个文件, 全部是基座 fork 测试, 对当前产品代码零覆盖)
```

### 1.2 六维能力对照（技能标准 vs 现状）

| 技能阶段 | 标准产出 | 本仓库现状 | 差距 |
|---|---|---|---|
| 场景立项 | 明确场景与成功标准 | ✅ 需求/接口/回归/报告闭环场景清晰 | — |
| 能力拆解 | 能力清单 | ✅ 5 扩展模块 + 5 技能已拆 | 达标 |
| 角色设定 | 多智能体角色/边界 | ⚠️ 仅在 `ARCHITECTURE_GUIDE.md` 设计, 运行态无角色 | **未落地** |
| 工具装配 | 工具/MCP 接入 | ✅ mcp_servers.json + agent-skills 机制 | 部分(未实际调用) |
| 记忆与状态 | 记忆分层/状态保持 | ⚠️ run_store.py 仅操作日志, 非智能体记忆 | **缺学习闭环** |
| 评测上线 | 评测/灰度/回滚 | ❌ 无 eval 体系, 无质量基线 | **缺失(阻断级)** |

### 1.3 关键发现（6 条）

1. **智能层未接线**：`project_manager.py` 不 import langgraph/langchain；`build/build_exe.py` 显式 `--exclude-module langchain, langgraph...`；`src/agentic_explorer/` 运行态从未被调用。当前"智能"= 规则脚本。
2. **LLM 能力是"可选项中的可选项"**：`generate_cases.py` 的 `_llm_generate` 需同时满足"设了 key + 装了基座依赖 + 手动加 `--llm`"才生效，默认规则版。
3. **测试真空**：`tests/` 11 个文件全是基座用例（`test_swarm_diagram`、`test_pr_analyzer` 等），对 `project_manager.py`、`extensions/*`、`web_console/*` **零单测**。
4. **双入口漂移**：`project_manager.py`(run) 与 `software_testing_agent.py` 都做"需求→用例→报告"，逻辑易分化。
5. **评测缺失**：没有对"生成用例质量""门禁可靠性""报告可读性"的任何量化评测，无法证明"智能体"在变好。
6. **记忆是日志不是学习**：`run_store.py` 的快照能画趋势图、能聚类失败根因，但根因结论**不会回灌**驱动下一次更优的测试设计。

---

## 二、代码评审（5 维评分 + 分级清单）

### 2.1 五维评分

| 维度 | 评分 | 说明 |
|---|---|---|
| 正确性 | 3/5 | 主流程对；门禁语义（`all_pass = failed==0 and passed>0`、环境不可达不判绿）设计正确；但编排器缺异常兜底。 |
| 可读性 | 3/5 | `project_manager.py` 834 行单文件、`index.html` 1550 行单文件，维护成本高。 |
| 测试 | 1/5 | 产品代码零覆盖，仅基座遗留测试。 |
| 安全 | 4/5 | `.env` 已 gitignore；Flask 绑定 `127.0.0.1` 且 `debug=False`；但无鉴权（localhost 可接受，团队化需补）。 |
| 性能 | 4/5 | 当前规模无瓶颈；SQLite 读写轻量。 |

### 2.2 问题清单（阻断 / 建议 / 可选）

**【阻断】**
- B1 建立评测基线：无量化评测 = 无法判定智能体有效。至少补齐：①用例生成质量评分（LLM-as-judge + 人工抽检）；②门禁可靠性回归测试（环境不可达/部分 skip 的场景）；③报告可读性检查。
- B2 补产品代码单测：对 `project_manager.py` 的 `_step_report`/`load_projects`/`rerun`、extensions 的 `run_regression`/`_cases_html`、web_console 的核心路由，加 pytest 守护，避免回归破坏。

**【建议】**
- S1 接入并默认启用 LLM 智能层：把 `src/agentic_explorer` 的 Supervisor 编排接进 `run`，作为"有 key 即增强"的可选阶段（无 key 退化规则版，绝不破坏现有能力）。
- S2 记忆三分法落地（见第四章）：工作记忆(任务态) / 长期记忆(项目知识库) / 情景记忆(历史失败根因)，让聚类出的根因回灌用例设计。
- S3 解耦大文件：`project_manager.py` 按子命令拆 `commands/`，`index.html` 按页拆模块或引入轻量构建。
- S4 统一入口：合并 `software_testing_agent.py` 到 `project_manager run`，消除双入口漂移。
- S5 Web 控制台团队化：加 token 鉴权 + 运行日志流（WebSocket/SSE），当前 localhost 无鉴权仅适合本机。

**【可选】**
- O1 技能自动发现与版本管理（当前 agent-skills 为静态目录）。
- O2 Web 自动化闭环：接 Playwright MCP，让 agent 自探索 UI 并反向生成用例。
- O3 失败根因自动建 Issue / 钉钉卡片（通知能力已有，只差触发）。

---

## 三、架构权衡 · 确定性二维判定 & 职责边界

### 3.1 确定性 × 价值 矩阵（决定先做谁）

| 功能 | 确定性 | 价值 | 决策 |
|---|---|---|---|
| 接口 pytest 自动化 | 高 | 高 | ✅ 已完成 |
| 报告聚合 + 通知 | 高 | 高 | ✅ 已完成 |
| 回归门禁 (expect_json) | 高 | 高 | ✅ 已落地，**需 B2 加固** |
| 规则版 需求→用例 | 高 | 中 | ✅ 已完成 |
| **评测基线** | 中 | 高 | 🔴 **P0 阻断(B1)** |
| **产品单测** | 高 | 高 | 🔴 **P0 阻断(B2)** |
| LLM 用例生成 | 低 | 高 | 🟡 P1（需 key） |
| 多智能体编排 | 低 | 高 | 🟡 P1/P2 |
| 记忆自学习 | 低 | 中 | 🟢 P2 |

> 方法论提示：**确定性高且价值高的先做（已做），价值高但确定性低的用"灰度+回滚"逐步上（LLM/编排）。评测(P0)是低确定性能力能上线的前提——没有评测就没有"上线"的资格。**

### 3.2 职责边界法（智能体"该做/不该做"）

- **该做**：读需求→推理测试点→生成可维护用例；依据历史失败根因建议补测场景；环境不可达时理智 skip 而非假绿；产出人类可读报告。
- **不该做（交由人/CI）**：替代人工探索性测试结论；对拍脑袋的断言"自信满满"；在无 key 时伪装成"智能"；越权访问未授权系统。
- **护栏**：LLM 生成内容须带"来源/置信度"标记；关键断言仍可降级到规则校验；所有外部动作（建 Issue、发通知）走显式开关。

---

## 四、建设意见 · 记忆三分法与评测上线设计

### 4.1 记忆三分法（落地方案）

| 层 | 当前 | 目标 | 载体 |
|---|---|---|---|
| 工作记忆 | 任务运行态变量 | 单次 run 的中间产物(用例草稿/执行轨迹) | 内存 + runs.db（已有） |
| 长期记忆 | 无 | 项目知识库：接口契约、认证方式、稳定场景白名单 | `projects/<id>/knowledge.md` + 向量/全文检索 |
| 情景记忆 | snapshots 仅存结果 | 历史失败根因 + 已验证修复 + 易错场景模式 | snapshots 扩展 `root_cause` 字段 + 聚类库 |

> 闭环：聚类出的根因（`normReason` 已有）→ 写入情景记忆 → 下次 `run` 时注入"重点覆盖清单" → 用例设计自优化。

### 4.2 评测上线体系（P0 必须）

```
评测三层：
L1 产物正确性   pytest 单测 (B2)                  —— 防回归
L2 流程可靠性   端到端冒烟: 起服务→run→门禁判定   —— 防假绿
L3 智能质量     LLM-as-judge 对生成用例打分(覆盖度/可执行性/去重) + 人工抽检看板
```

- **灰度回滚法**：LLM/编排能力一律做成"可选阶段 + 开关"。无 key → 规则版（当前能力不丢）；有 key → 增强版；增强版异常 → 自动回退规则版并告警。每个 Phase 可独立回滚。

---

## 五、下一步规划 · 里程碑

> 原则：**保住现有确定性能力不退化，逐级叠加智能层，每级带评测与回滚。**

### Phase 0 — 质量地基（约 2 周，阻断级）
- [x] B1 评测基线：建 `tests/eval/` —— 用例质量 LLM-judge 脚本 + 门禁可靠性用例 + 报告渲染断言。
- [x] B2 产品单测：覆盖 `project_manager` 关键函数、`run_regression`、`_cases_html`、web 核心路由（用 Flask 测试客户端 + 内存 SQLite）。
- [x] S4 合并双入口：`software_testing_agent.py` → `project_manager run --pipeline`。
- [x] **CI 真正跑起来**：根目录 `.github/workflows/ci.yml`。
      此前模板放在 `extensions/reporting/.github/workflows/`（GitHub **只读仓库根**）→ 从未执行过，
      所以"验收：CI 跑通"实际一直是未验证状态；且旧模板用 `|| true` 吞掉 pytest 失败，与本项目
      「环境不可达不判绿」的口径直接冲突 —— 模板已删除，能力全部移植到根 workflow。
      作业划分：自有单测=硬门禁、基座遗留测试=非门禁（不拿第三方代码问题卡交付）、
      项目级门禁=已配置环境才跑且**未配置时显式警告不静默判绿**、通知=复用上游摘要。
- 验收：CI 跑通；门禁在"环境不可达"场景稳定判不绿。

### Phase 1 — 智能层接入（有 key 后，约 3 周）
- [ ] S1 把 `src/agentic_explorer` Supervisor 编排接为 `run` 的可选阶段（gated by key）。
- [ ] 需求→用例 默认走 LLM（规则版作降级）；生成物带来源/置信度标记。
- [ ] S2 长期记忆：项目知识库 `knowledge.md` + 检索注入。
- 验收：无 key 时能力 == Phase0；有 key 时用例覆盖度/可执行性 L3 评分 ≥ 基线。

### Phase 2 — 自主进化（约 3 周）
- [ ] 情景记忆回灌：失败根因 → 重点覆盖清单 → 用例自优化。
- [x] O3 门禁结果摘要 + 失败通知：`extensions/reporting/gate_notify.py`（三态判定：通过/未通过/未执行；未执行不算绿）。自动建 Issue 未做。
- [ ] L3 评测常态化：每次 run 输出质量分，趋势图入看板。
- 验收：同一项目重复 run，易错场景覆盖率单调上升。

### Phase 3 — 团队化（按需）
- [x] S5 Web 控制台**日志流**（子进程增量日志 + 前端轮询实时刷新）。
- [x] **控制台访问鉴权（默认关闭）**：`web_console/auth.py` + `STA_CONSOLE_TOKEN`。
      选择"默认关闭"而非强制：本地单人使用不增加摩擦，需要暴露到局域网/内网演示时一行配置开启。
      开启后 `/api/*` 回 401 JSON、页面跳 `/login`；免鉴权仅 `/login`、`/logout`、`/healthz`、
      `/api/auth/status`、`/static/`；会话存 token 的 `sha256` 指纹（换 token 旧会话自动失效）；
      token 不写前端、不写日志、启动信息不回显；`next` 只接受站内相对路径防开放重定向。
- [x] O2 Web 自动化闭环：`extensions/web_testing/run_web.py`（声明式 YAML + Playwright + 三道防误判闸门）。未接 Playwright MCP。
- [ ] O1 技能市场/版本管理。

---

## 六、风险与回滚

| 风险 | 影响 | 应对 |
|---|---|---|
| LLM 生成用例质量不稳定 | 误判/漏测 | 规则版作降级；L3 评测门禁拦截低分产物 |
| 智能层引入破坏现有流水线 | 回归 | Phase1 全程开关隔离；CI(B2) 守护 |
| 记忆回灌产生错误偏见 | 越测越偏 | 情景记忆人工可审、可重置 |
| 团队化暴露无鉴权接口 | 越权 | S5 在 Phase3 前置；localhost 阶段维持现状 |

---

## 七、一句话总结

> 你已经有了一套**扎实的测试工程底座**（接口/回归/报告/可视化全链路跑通且门禁严谨），但"智能体"的**智能内核与评测闭环尚未通电**。下一步优先级应是：**先补评测与单测（P0，让现有能力可信可守护），再接入 LLM 编排与记忆闭环（P1/P2，让能力进化）**，全程保持"无 key 可降级、可回滚"。
