# 架构与扩展点分析：软件测试智能体（基于 agentic-test-explorer）

> 本文档是「在开源基座上不断完善软件测试智能体」的第一份评审材料。
> 基座：`srbarrios/agentic-test-explorer` v0.2.0（MIT，Python ≥3.11，LangGraph + Playwright + MCP + Agent Skills + Langmem）。

---

## 1. 为什么选它做底座

你的目标是「类似 deepseek harness / WorkBuddy，用 skills + 智能体串起测试全流程」。
对比多个开源项目后选它，核心原因是它的**扩展机制和我们已配置的 skills/experts 天然对齐**：

| 扩展机制 | 基座如何开放 | 我们怎么用 |
|---|---|---|
| 多智能体编排 | LangGraph Supervisor-Worker Swarm（标准 3 人格 + 高级 5 人格 + 探索者） | 注册新的 QA 智能体（接口测试、性能、安全人格） |
| **MCP 接入** | `mcp_servers.json` + `get_mcp_tools()` | 接入你已配置的专家/MCP 工具 |
| **Agent Skills** | `AGENT_SKILLS_ROOT`（`./agent-skills`）+ `fetch_agent_skill` / `run_agent_skill_script` | 直接挂载你已配的测试技能（software-testing、playwright、qa-skill-suite 等） |
| 适配器契约 | `tools/browser/engine.py`（感知+驱动） | 新增 REST/接口适配器，复用同一套「智能体发意图→引擎执行→记录→自愈」闭环 |
| 记忆 | `memory.py` + Langmem 四级记忆（语义/情节/程序/优先级） | 跨会话沉淀页面、缺陷、用例策略 |
| Missions | `missions/*.yaml` + PR 分析器 | 需求→用例、接口测试 mission 的落点 |
| 报告 | `test_report.md` + `action_tape.jsonl` + `reproduction_*.spec.ts` | 扩展为 Allure / HTML + CI 通知 |

> 备注：蚂蚁 CodeFuse 的 Test-Agent 虽星标最高（719★），但已停更（2024-03）且偏「微调模型+Chatbot demo」，与「harness + skills」愿景契合度低，未选。

---

## 2. 原项目能力边界（先认清，再扩展）

| 已有能力 | 说明 |
|---|---|
| Web 探索式测试 | 多人格智能体驱动真实浏览器，发现缺陷/渲染异常/边界场景 |
| 自愈执行 | Playwright 动作包裹异常捕获，错误以自然语言回传，智能体自适应 |
| 选择器韧性策略 | 运行时拒绝 XPath/位置选择器，强制 `data-test-subj → aria-label → 可见文本` |
| 缺陷→可复现脚本 | 自动从 Action Tape 生成 `reproduction_*.spec.ts`（Playwright） |
| PR 驱动测试 | 解析 GitHub PR diff，自动生成针对性 mission |
| 回归/模型导出 | `--regression` 从缺陷目录生成 mission；`--export-model` 导出应用模型 |
| 可视化看板 | Streamlit 实时看板（截图/状态/Action Tape） |
| 跨会话记忆 | Langmem 四级记忆 + 语义检索 + 程序性提示自优化 |

**缺口（即我们要补的能力）**：
1. ❌ 接口(API)自动化（基座只做 Web，无 REST 适配）
2. ❌ 需求分析→用例（基座有 PR 分析，但无「需求文档→结构化用例」链路）
3. ⚠️ 报告仅 Markdown（缺 Allure/HTML + CI 通知：钉钉/163 邮件）
4. ✅ 性能/安全探索（已实现 `extensions/perf_security/run_perf_security.py`：线程池并发压测 + 6 项安全检查，接入 CLI/报告/看板/Web）
5. ✅ Web 自动化（**声明式可门禁**那一半已补齐：`extensions/web_testing/run_web.py`。基座的 Web 探索是"给智能体用的"，缺少"给 CI 用的确定性回归"，两者互补）

---

## 3. 目标能力的落地映射

| 目标能力 | 复用基座 | 新增模块（位置） | 关键设计 |
|---|---|---|---|
| ①接口(API)自动化 | `config.yaml` 的 app/paths、MCP/Skills 机制 | `extensions/api_testing/` | pytest+requests 直连 mall-admin；`unique_suffix` fixture 数据隔离；产出可提交用例；并以 Skill 形式供智能体调用 |
| ②需求分析→用例 | `missions/*.yaml`、`pr_analyzer.py` 思路 | `extensions/requirements_to_cases/` | 需求/PR → 结构化用例(等价类/边界值/场景法)；可经 LLM 或 Skill 生成；落地为 mission |
| ③报告与CI增强 | `report_*/test_report.md` | `extensions/reporting/` | 聚合 Markdown → HTML/Allure；GitHub Actions + 钉钉 + 163 邮件（复用你 api_auto_demo 经验） |
| ④性能/安全冒烟 | 适配器契约、`orchestration` 人格注册 | `extensions/perf_security/` | 性能：线程池并发（p50/p95/p99、错误率、吞吐、阈值门禁），不依赖 locust；安全：鉴权/注入/错误回显/响应头 6 项检查；三道闸门防假绿与假红（业务码判错、环境不可达不判绿、基线校验防假漏洞） |
| ⑤Web UI 冒烟 | 基座 `tools/browser/engine.py` 的定位器策略（**只继承方法论，不共用执行路径**） | `extensions/web_testing/` | Playwright + 声明式 `web.yaml`；定位器优先级 `data-test-subj → aria-label → 可见文本 → role`，**运行时拒绝 XPath/位置选择器**；无断言的场景记 SKIP 不判绿；连接级错误（不可达）与 HTTP 4xx/5xx（产品缺陷）严格区分；失败留截图 + 可复现 `.spec.ts` |

---

## 4. 与「你已配置的 skills/experts」的对接方式

基座是**自带 Bring-Your-Own Skills/MCP** 机制的，对接路径有两条：

1. **Agent Skills 目录**（推荐，零编码）：把测试方法论写成 `agent-skills/<name>/SKILL.md`（遵循 agentskills.io 规范）。
   智能体在运行时通过 `fetch_agent_skill` / `run_agent_skill_script` 自动加载。
   本仓库已预置 2 个：`api-test-design`、`requirements-to-cases`。
2. **MCP 服务器**：在 `mcp_servers.json` 填入你已有的 MCP 连接器（专家能力、内部工具 API 等），智能体通过 `get_mcp_tools()` 调用。

> 待确认：WorkBuddy 里「已添加的专家和技能」是通过 MCP 暴露，还是仅在本会话内由我（助手）直接调用？
> 若是前者，把这些 MCP 地址写进 `mcp_servers.json` 即可让探索智能体直接受益；
> 若是后者，则它们由我（编排层）在生成用例/分析时调用，再写入 mission 或 Skill。

---

## 5. 目录约定（在基座之上新增）

```
agentic-test-explorer/            # 基座（上游，保留 origin 便于同步）
├── docs/                         # 【新增】本项目规划/分析文档
├── agent-skills/                 # 【新增】Bring-Your-Own Skills（智能体可调用）
│   ├── api-test-design/
│   └── requirements-to-cases/
├── extensions/                   # 【新增】各项能力的扩展模块
│   ├── api_testing/              # ① 接口自动化（pytest+requests，可独立运行）
│   ├── requirements_to_cases/    # ② 需求→用例
│   ├── reporting/                # ③ 报告与 CI
│   ├── perf_security/            # ④ 性能/安全
│   └── web_testing/              # ⑤ Web UI 冒烟（Playwright 声明式）
├── missions/                     # 基座：YAML 测试任务
├── src/agentic_explorer/         # 基座：核心代码
├── config.yaml / .env            # 本地运行配置（已 gitignore）
└── README_CN.md                  # 【新增】中文项目总览
```

---

## 6. 运行前置（重要）

基座运行**必须**有 LLM 凭证（Claude 或 Gemini 其一）：
- `ANTHROPIC_API_KEY`（Claude 直连）或
- `GOOGLE_API_KEY` / `~/.gemini/oauth_creds.json`（Gemini）

在 `.env` 中填入其一后，`agent-explorer --missions missions/...` 即可驱动 Web 探索。
**接口自动化（①）不需要 LLM**，可立即对 mall-admin 跑 pytest。

---

## 7. 风险与待确认

1. **LLM 凭证**：基座运行需要 Claude/Gemini key，当前环境未配置 → 上层智能体暂不能端到端跑，先做工程骨架 + ①接口自动化（不依赖 LLM）。
2. **mall-admin 状态**：探测 localhost:8080 返回 502，服务未真正就绪 → 接口测试写成「不可达自动跳过」，待你启动后端后再实跑。
3. **与 WorkBuddy skills 的桥接方式**：见第 4 节，需你确认是 MCP 暴露还是编排层调用。
4. **Python 版本**：基座要求 ≥3.11，已用受管 3.13.12 建 venv；若个别依赖在 3.13 上有兼容问题，可退回 3.12。

---

## 8. 下一步（已排期）

见各 `extensions/*/README.md` 与 `README_CN.md`。一句话路线：
**先让①接口自动化跑通（不依赖 LLM）→ 接好 skills/MCP → 再补②需求用例 → ③报告CI → ④性能安全**，
待你提供 LLM key 后，把整套串成「需求→用例→接口/Web自动化→报告」的智能体闭环。
