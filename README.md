# software_testing_agent · 软件测试智能体

> 一个从 0 到 1 搭建的**软件测试全流程智能体**：以开源基座
> [`srbarrios/agentic-test-explorer`](https://github.com/srbarrios/agentic-test-explorer)（MIT, v0.2.0）为底座，
> 在其上补齐五大能力与记忆闭环，把测试串成一条**可门禁、可追溯、不假绿**的流水线：
>
> **需求分析 → 测试用例 → 接口 / Web 自动化 → 测试报告 → 性能 / 安全 → 缺陷草稿**

目标是做"**会自己发现问题，并且不骗人**"的测试智能体：所有判定都要经得起追问——
服务没起来不能判绿、没写断言不能判绿、算不出来的分数不猜、环境问题不能被算成产品缺陷。

---

## 目录

- [1. 它解决什么问题](#1-它解决什么问题)
- [2. 架构总览](#2-架构总览)
- [3. 目录结构](#3-目录结构)
- [4. 五大能力](#4-五大能力)
- [5. 记忆闭环：越跑越准](#5-记忆闭环越跑越准)
- [6. 快速开始](#6-快速开始)
- [7. 多项目对接](#7-多项目对接)
- [8. CLI 速查](#8-cli-速查)
- [9. Web 控制台](#9-web-控制台)
- [10. CI 与通知](#10-ci-与通知)
- [11. Agent Skills](#11-agent-skills)
- [12. 评测体系](#12-评测体系)
- [13. 贯穿全项目的判据（防假绿 / 防假红）](#13-贯穿全项目的判据防假绿--防假红)
- [14. 密钥与 .gitignore 约定](#14-密钥与-gitignore-约定)
- [15. 路线图](#15-路线图)
- [16. 许可证](#16-许可证)

---

## 1. 它解决什么问题

| 测试环节 | 常见现状 | 本项目的做法 |
|---|---|---|
| 需求分析 → 用例 | 手写用例耗时，覆盖靠经验 | `requirements_to_cases`：规则版秒出草稿，可选 LLM 增强（多步自审），**每轮自动算结构质量分** |
| 接口自动化 | 用例零散、结果不可信 | `api_testing` + 声明式回归清单，**环境不可达自动 SKIP 不判绿** |
| Web 自动化 | 定位器脆、点了就走也算过 | `web_testing`：声明式 `web.yaml`，**无断言不判绿**，拒绝脆弱定位器 |
| 性能 / 安全 | 需要独立工具与人力 | `perf_security`：线程池冒烟 + 6 项安全检查，**零新增依赖** |
| 报告 | 只有一堆红的绿的结果 | HTML 报告 + 跨项目看板 + **失败分类** + **缺陷草稿** + **生成溯源** |
| 回归门禁 | "没有失败"就当成通过 | 三态判定：✅ 通过 / ❌ 未通过 / ⏭ **未执行（不算绿）** |
| 用例越写越准 | 每轮从零开始 | `extensions/memory/`：失败聚类 → 项目知识 → **覆盖校验与回灌补齐** |

**两个硬指标**：① 任何一条判定都能追溯到证据；② 任何"看起来绿"的结果都不会是因为**没跑**。

---

## 2. 架构总览

```
                        ┌───────────────────────────────────────────┐
                        │   统一入口  project_manager.py（CLI）      │
                        │   ＋ web_console/（Flask 控制台，8 页）     │
                        └───────────────────┬───────────────────────┘
                                            │  全部执行同源（控制台只做薄封装）
        ┌───────────────┬───────────────┬───┴───────────┬───────────────┬──────────────┐
        ▼               ▼               ▼               ▼               ▼              ▼
   ① 接口自动化     ② 需求→用例      ③ 报告与 CI     ④ 性能/安全     ⑤ Web UI 冒烟   基座 Web 探索
   pytest+requests  规则版/LLM       HTML/Allure     线程池+安全检查  Playwright      LangGraph
   extensions/      extensions/      extensions/     extensions/      extensions/     多人格智能体
   api_testing      requirements_    reporting       perf_security    web_testing     （探索未知页面）
                    to_cases
        │               │               │               │               │              │
        └───────────────┴───────────────┴───────┬───────┴───────────────┴──────────────┘
                                                ▼
                        ┌───────────────────────────────────────────┐
                        │ 记忆闭环  extensions/memory/               │
                        │ lessons（失败聚类）→ knowledge（项目约定） │
                        │ → focus（覆盖校验 + 回灌补齐）              │
                        └───────────────────┬───────────────────────┘
                                            ▼
                        ┌───────────────────────────────────────────┐
                        │ agent-skills/ + mcp_servers.json          │
                        │ 测试方法论与外部工具，供智能体与生成环节调用 │
                        └───────────────────────────────────────────┘
```

**基座提供什么**：LLM 驱动的多人格探索测试（新人 / 高手 / 对抗 / 无障碍 / 大数据量 / 急性子 / 回访 / 探索者）、
Playwright 自愈执行、Action Tape 录制与缺陷可复现脚本、跨会话四级记忆、PR 驱动任务生成。
**本仓库补齐什么**：①~⑤ 五项工程化能力 + 记忆闭环 + CI 门禁 + 可视化控制台。

> 两者是**互补**关系：基座面向"智能体探索未知页面"，`extensions/web_testing` 面向"给 CI 的确定性回归"；
> 只继承定位器与等待的方法论，**不共用执行路径**。

---

## 3. 目录结构

```
software_testing_agent/
├── project_manager.py            # 统一 CLI 入口（10 个子命令）
├── software_testing_agent.py     # 无 LLM 的轻量流水线入口
├── run_console.py                # 本地起控制台（前台 / --detach 后台常驻）
├── extensions/                   # 本仓库新增的能力模块
│   ├── common/                   # 跨扩展的**唯一实现层**：yamlio/auth/data/gates/cases + obs（日志与 run_id）
│   ├── api_testing/              # ① 接口自动化（pytest + requests，可独立运行）
│   ├── requirements_to_cases/    # ② 需求→用例 + 结构质量分（case_quality）+ 生成溯源（provenance）
│   ├── reporting/                # ③ 报告：HTML、门禁摘要、缺陷草稿、新旧对比、钉钉/邮件
│   ├── perf_security/            # ④ 性能与安全冒烟
│   ├── web_testing/              # ⑤ Web UI 冒烟（Playwright 声明式）
│   ├── regression/               # 核心业务回归执行引擎
│   ├── memory/                   # 记忆闭环：lessons / knowledge / focus
│   └── skills/                   # agent-skills 注册表（扫描 + 目录生成）
├── web_console/                  # 可视化控制台
│   ├── app.py                    # Flask 后端（薄封装，执行走 project_manager）
│   ├── auth.py                   # 可选的访问鉴权
│   ├── guard.py                  # L 层最小拦截：只读模式（STA_CONSOLE_READONLY）+ 审计（audit.jsonl）
│   ├── run_store.py              # 任务历史 / 回归快照（SQLite）
│   └── templates/index.html      # 单页前端（原生 JS，无框架）
├── agent-skills/                 # Bring-Your-Own Skills（6 个，SKILL.md 规范）
├── docs/                         # 规划、评审与架构文档
├── projects/                     # 多项目登记（⚠️ 不入库，见第 14 节）
├── tests/                        # 单元测试与评测（eval/ 为 L1~L3）
├── src/ missions/                # 基座源码与任务
├── .github/workflows/ci.yml      # 唯一生效的 CI（GitHub 只读仓库根）
├── config.yaml / .env / mcp_servers.json   # 本地配置（均已 gitignore，保留 .example）
└── requirements.txt / -dev.txt   # 运行依赖 / 开发与 CI 依赖
```

---

## 4. 五大能力

### 4.1 ① 接口自动化（`extensions/api_testing/`）

`pytest + requests`，**零 LLM 依赖**，可直接跑通。

- 声明式断言 `expect_json`：支持点路径（`data.token`）与 `__not_null__`（必须非空）。
- `unique_suffix` fixture 生成动态唯一后缀，解决 register / delete / update 的数据隔离。
- **服务不可达时自动 skip**，不让"环境没起"污染结果。
- 产出可提交的用例与 Allure 结果，并以 Skill 形式供智能体调用。

> ⚠️ 被测服务（mall-admin）的 **HTTP 状态码恒为 200，成败写在 body 的 `code`**。
> 因此 `api_smoke` 回归项**必须配 `expect_json`**，否则"密码错了也判通过"——这是典型的假绿。

### 4.2 ② 需求 → 测试用例（`extensions/requirements_to_cases/`）

三种模式，**任何失败都会自动降级规则版，绝不中断流水线**：

| 模式 | 命令 | 特点 |
|---|---|---|
| 规则版 | `run <id>` | 零依赖、零 key、秒出草稿；步骤/预期是模板占位，需人工细化 |
| LLM 增强 | `run <id> --llm` | 按需求生成贴合业务的用例；失败自动降级 |
| 多步自审编排 | `run <id> --llm --agentic` | 分析 → 初版 → 自评审（注入历史易错点）→ 终版；质量更高，代价是 4 次串行调用 |

**不绑定厂商**（见第 6.3 节），默认走 OpenAI 兼容协议。

**生成溯源**（`provenance.py`）：标记直接写进 `cases.md`，跟着文件走，避免它在离开 `artifacts/` 后无法判断来源：

```markdown
- 生成方式：LLM 增强（qwen-max · openai 兼容） · 可信度：中
- 上下文注入：情景记忆 3 条 · 项目知识 2 段
- ⚠️ 降级产出：403 余额不足 —— 已退回规则版，步骤/预期为模板占位，必须人工细化
```

> 「没配 key 走规则版」与「LLM 超时降级成规则版」长得一样，但前者是预期、后者意味着质量低于预期——
> 所以必须标记出来。可信度不编百分比，只给定性档位 + 实测结构质量分。

### 4.3 ③ 报告与 CI（`extensions/reporting/`）

- `generate_report.py`：项目级 HTML 报告（用例卡片、回归表格、门禁状态、证据链）。
- `gate_notify.py`：把三道门禁汇总成**三态结论**后发钉钉 + 163 邮件；**它是唯一判定源**，
  CLI / 报告 / 控制台 `/api/gates` / 通知全部调用同一函数，避免"控制台绿、CLI 红"。
- `defects.py`：失败项 → 可提交的缺陷草稿（见 4.5）。
- `trend_diff.py`：失败项新旧对比（见 4.6）。

### 4.4 ④ 性能与安全冒烟（`extensions/perf_security/`）

`run_perf_security.py`，**零新增依赖**（`ThreadPoolExecutor + requests`，不引 locust）。

- 性能：P50/P95/P99、错误率、吞吐 + **阈值门禁**；目标**从 `regression.yaml` 的只读接口自动派生**（写操作默认不压）。
- 安全：6 项检查（未授权访问、错口令、SQL 注入、堆栈泄露、用户枚举、安全响应头）。
  **WARN 不拦门禁，FAIL 才拦**。
- **三道闸门防误判**：
  1. 业务码判错 —— HTTP 200 但 `code=500` 仍算失败（需显式声明才启用）；
  2. 环境不可达不判绿；
  3. **基线校验防假红** —— 先用正确凭据登录一次，否则会把"登录没成功"报成"未授权访问 = 存在漏洞"的**假漏洞**。
- 部分重跑（`--only security`）保留另一侧结果并标记 `stale_sections`，不假装全量跑过。

### 4.5 ⑤ Web UI 冒烟（`extensions/web_testing/`）

`run_web.py`，Playwright + 声明式 `web.yaml`，面向 CI 的确定性回归。

- **三道闸门**：**无断言的场景记 SKIP 不判绿**（防"点完就走"）/ 环境不可达不判绿 / 配置问题单独报出且该场景不执行。
- **严格分流**：连接级错误（环境不可达 → SKIP）与 HTTP 4xx/5xx（产品缺陷 → FAIL）不混为一谈。
- **定位器硬约束**：`data-test-subj → aria-label → 可见文本 → role`，**运行时拒绝 XPath、`:nth-child`、裸 div-span 链**。
- `retries` 默认 0（**重试会吃掉偶发缺陷的证据**）；重试才通过记 PASS 但标 `flaky: true`。
- 失败自动截图 + 生成可复现 `.spec.ts`；**每次运行前清理上次证据**，避免旧截图当成本次证据。

### 4.6 用例结构质量分（`case_quality.py`）

每次生成用例都自动算，五维加权 → 0-100 分，落 `quality.json` + `quality_history.jsonl`，在报告 / 看板 / 控制台展示**总分与趋势**：

| 维度 | 权重 | 含义 |
|---|---|---|
| 需求覆盖 | 0.30 | 被用例覆盖到的需求占比 |
| 三类齐备 | 0.25 | 每条需求是否功能 / 边界 / 异常都有 |
| 可执行性 | 0.25 | 步骤有动作、预期可判定 |
| 具体性 | 0.10 | 是否写到具体数据（数值 / 边界词 / 特殊值） |
| 去重 | 0.10 | 用例之间不重复的程度 |

**三条诚实性约定**：

- **结构分 ≠ 质量判定**——它只看形式；"写错的边界值"照样能拿高分，语义质量要用 `tests/eval/llm_judge.py` 抽样评。
- **默认不做硬门禁**——结构分可以注水刷高，拿它卡 CI 等于鼓励刷分；正确用法是**看趋势**。
  确需卡阈值时显式开启 `run <id> --quality-min 80`（**算不出分数时按未达标**，把"没算出来"当"达标"就是假绿）。
- **算不出就不猜**——需求条数未知时覆盖率记「未计分」，总分按剩余维度重新归一化，不填 0 也不填 100。

### 4.7 缺陷草稿（`defects.py`）

把"流水线上的红"变成能提交给开发的缺陷单：编号 / 建议级别 / 复现步骤 / 实际 / 期望 / 证据，
落 `defects.md`（可直接粘贴）与 `defects.json`。

| 约定 | 说明 |
|---|---|
| **不自动提单** | 自动建单产生噪音，且创建容易、删除难；只出草稿，人确认后再提交 |
| **级别只是建议** | S1~S4 由规则推断（安全 FAIL=S1 / 主流程 FAIL=S2 / 性能与抖动=S3），定级权在人 |
| **环境问题不是缺陷** | 连接失败、服务不可达导致的 SKIP **单独列出**，并写明"不是缺陷" |
| **配置问题不是缺陷** | 定位器非法之类单列「配置问题」，改配置即可 |

> 把"服务没起"报成产品缺陷，是最伤信任的一种错误。

### 4.8 失败项新旧对比（`trend_diff.py`）

红灯长期挂在同样几个场景上，人就会忽略它们（警报疲劳）。本模块把本次与**上一次**快照对比并分类：

| 判定 | 含义 | 处理建议 |
|---|---|---|
| 🔺 回归 | 此前通过、本次失败 | 优先级最高，大概率是刚引入的变化 |
| 🆕 新增 | 历史上没有通过记录 | 先确认场景本身是否成立 |
| 🔁 持续失败 | 历史窗口内全失败 | 欠账，不是这次新引入的 |
| 🎲 不稳定 | 窗口内红绿交替 | 先怀疑环境 / 数据 / 竞态，别急着当产品缺陷 |
| ✅ 已恢复 | 本次由失败转通过 | 确认是真的修好了 |
| ❔ 无法判定 | 没有可用基线 | 如实说明，不硬贴标签 |

三条克制：**上次全 SKIP 不作基线**（否则全变"新增"= 噪音）/ **不参与门禁判定**（只回答"先看哪个"）/
**SKIP 不进胜负序列**。缺陷草稿会带上这些标签，并在**同级严重度内**重排。

### 4.9 回溯与对比

- `artifacts/run_meta.json` 记录本次运行的模式与注入情况（`llm / agentic / lessons_injected / quality / focus / ...`）。
  增量更新走 `_merge_run_meta()`，**整体覆盖会把上次的性能安全、Web 结论抹掉**。
- 跨项目看板：一键汇总各项目最近一次回归、性能安全与 Web 冒烟的三组门禁状态。

### 4.10 ⑥ AI 探索测试（`extensions/agentic/`）

把基座（LangGraph Supervisor-Worker 群）的**自主探索**接进确定性流水线。
接入方式：**独立入口 + 子进程**（决策 D2，见审阅报告 §3.3）——
`project_manager` 以 `run_agentic.py` 子进程调用，输入一个 `mission.yaml`、
输出结构化契约 `artifacts/agentic.json`，再进报告卡片与 `run_meta`。

- **为什么不 import 进 `project_manager`**：基座需要 langgraph / langchain / langmem / playwright，
  import 会把重依赖拖进刻意精简的 CI 硬门禁、把"LLM 不确定性"与"确定性门禁"的语义混在一个进程，
  并与刚消除的双入口漂移反向。依赖单独放 `extensions/agentic/requirements-agent.txt`。
- **可降级阶段（不是门禁）**：探索只产出**发现**，不产出 pass/fail。
  `无 key / 超时 / 异常 / 缺任务` → 降级，**回落确定性链路并如实出声**；
  `run --explore` 不因降级变红，显式 `explore` 子命令用退出码 **3** 区分「未执行」与「失败」。
- **三层兜底**：入口内部 → `project_manager._step_agentic`（进程级：超时/非零退出/契约缺失）
  → 报告卡片照实展示「已降级 + 原因」。**跑之前先清旧契约**，否则入口崩溃时会把上一轮的成功当成本轮。
- **判据对齐**：降级原因**是枚举**（`no_llm_key / not_configured / timeout / error`）；
  只有显式 `status == "ok"` 才算"跑了"；「未执行」**不渲染成绿灯**。
- 用法：`python project_manager.py explore <id>` 或 `run <id> --explore`；
  mission 放项目目录下 `mission.yaml`（格式见 `missions/README.md`），
  可用 `STA_AGENT_PYTHON` 指向装有基座依赖的独立解释器、`STA_EXPLORE_TIMEOUT` 控制墙钟上限。

---

## 5. 记忆闭环：越跑越准

`extensions/memory/` 三个模块**分工不重叠**，合起来才是闭环：

| 模块 | 来源 | 时效 | 回答的问题 |
|---|---|---|---|
| `lessons.py` | **自动**聚类流水线失败项 | 短期 | "哪些场景容易红" |
| `knowledge.py` | **人工**撰写（控制台可编辑） | 长期 | "这个项目的取值约定是什么" |
| `focus.py` | 对二者注入结果的**校验** | 每轮 | "注入的易错点，到底覆盖到了没有" |

**`focus.py` 补的是闭环最后一公里**：只做"失败聚类 → 注入生成提示"而不校验，
等于把"提示词发出去了"当成"结果达成了"——这是假绿的另一种形态。

三个数字**分开报**，绝不合并成一个"覆盖率"（否则补齐会冒充成模型学会了）：

- `native`：本轮生成就用例覆盖到了；
- `persisted`：沿用上一轮补齐的行；
- `backfilled`：本轮新增补齐。

补齐结果**必须持久化**到 `projects/<id>/focus_supplement.md` 并每轮自动并入——
`cases.md` 每轮都从需求重新生成，不持久化就永远原地打转。

实测（同一项目连跑两轮）：

```
第 1 轮：重点覆盖 3/6 为生成即覆盖，3 条本轮新增补齐（首次）
第 2 轮：重点覆盖 3/6 为生成即覆盖，3 条沿用上轮回灌（持平）
```

> ⚠️ **已知边界（诚实声明）**：匹配用的是与 `knowledge` 同一套关键词 / 中文 2-gram 重合，
> **不是语义理解**。高度相似的清单项（如 `错误密码A` / `错误密码B`）会被同一条用例同时判为已覆盖，
> 所以覆盖率只能当**粗粒度信号**。宁可漏报"未覆盖"，不能谎报"已覆盖"。

---

## 6. 快速开始

### 6.1 环境要求与安装

```bash
python -m venv .venv
.venv\Scripts\activate                  # Windows（Linux/macOS 用 source .venv/bin/activate）
pip install -r requirements.txt         # 基座 + 扩展依赖
pip install -r requirements-dev.txt     # 开发与 CI（pytest / allure-pytest）
playwright install chromium             # 仅 Web 探索 / Web 自动化需要
```

### 6.2 配置

```bash
cp .env.example .env                    # 密钥与运行参数（不入库）
cp config.yaml.example config.yaml      # 基座配置（app / auth / skills / llm）
cp mcp_servers.json.example mcp_servers.json
```

> **接口自动化不需要任何 LLM Key**；只有跑基座"LLM 驱动探索测试"或"需求→用例 LLM 增强"时才需要。

### 6.3 多模型接入（不绑定厂商）

| 环境变量 | 说明 | 默认 |
|---|---|---|
| `LLM_PROVIDER` | `openai`（默认）或 `gemini` | `openai` |
| `LLM_API_KEY` | OpenAI 兼容平台 key（本地 Ollama 可省略） | 空 |
| `LLM_BASE_URL` | 兼容 endpoint 的 base | OpenAI 官方 |
| `LLM_MODEL` | 模型名，如 `deepseek-chat` / `qwen-plus` / `glm-4-flash` | `deepseek-chat` |
| `LLM_TIMEOUT` | 单次调用超时（秒）；多步编排或慢模型调大 | `60` |
| `GOOGLE_API_KEY` 等 | 仅 `LLM_PROVIDER=gemini` 时使用 | - |

```bash
# 通义千问（注意 base 必须是 compatible-mode 路径）
LLM_PROVIDER=openai LLM_API_KEY=sk-xxx \
  LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1 \
  LLM_MODEL=qwen-max python project_manager.py run <id> --llm

# 本地 Ollama（完全离线、零 key）
LLM_PROVIDER=openai LLM_BASE_URL=http://localhost:11434/v1 LLM_MODEL=llama3 \
  python project_manager.py run <id> --llm
```

> 实测坑：`LLM_MODEL` 必须填**真实模型 id**。控制台上的展示名（如 `qwen3.8-max`）不是 API 参数，直填会超时。

### 6.4 跑通第一条流水线

```bash
# ① 登记一个项目（交互式，也可全参传入）
python project_manager.py create

# ② 全流程：需求 → 用例 → 接口自动化 → 核心回归 → 报告
python project_manager.py run <id>

# ③ 加上性能安全与 Web 冒烟
python project_manager.py run <id> --perf --web

# ④ 生成缺陷草稿与跨项目看板
python project_manager.py defects <id>
python project_manager.py dashboard
```

---

## 7. 多项目对接

**新建项目 = 填写项目信息**（测试环境地址、认证方式、需求），即可执行全流程与核心回归。
每个项目在 `projects/<id>/` 下落一份配置：

| 文件 | 作用 | 是否入库 |
|---|---|---|
| `project.yaml` | 元数据 + 测试环境地址 + 认证方式 | ✅ |
| `requirements.md` | 需求描述（驱动需求 → 用例） | ✅ |
| `regression.yaml` | 核心业务回归场景声明 | ✅ |
| `web.yaml` | Web UI 冒烟场景声明（Playwright 步骤 + `expect_*` 断言） | ✅ |
| `knowledge.md` | 项目知识库（人工维护的取值约定） | ✅ |
| `focus_supplement.md` | 记忆回灌补齐的用例行（自动生成） | ✅ |
| `artifacts/` | 用例、报告、回归 JSON、截图、Allure 结果 | ❌ 不入库 |

**核心回归支持三类回归项**：

| type | 说明 |
|---|---|
| `api_smoke` | 声明式 HTTP 检查（method / path / expect_status / **expect_json**），支持 `{{username}}` 占位 |
| `pytest_marker` | **复用已有 pytest 用例**，按 marker 筛选（如 `-m smoke`）——推荐 |
| `pytest_node` | 复用已有 pytest 用例，指定 node id |

推荐后两种：让核心回归与已写好的自动化用例**同源**，不必重复声明。

**门禁语义**：无 FAIL 且确有实际执行才算通过；环境不可达导致全部 SKIP 时**不判绿**。

**密钥分离**：`project.yaml` 只存环境变量**名**（如 `username_env: APP_USERNAME`），
真实口令写在根 `.env`（已 gitignore）。**独立运行的入口必须自己 `load_dotenv()`**，
否则凭据为空会导致大面积 401，极难排查。

---

## 8. CLI 速查

```bash
python project_manager.py <子命令> [参数]
```

| 子命令 | 作用 | 常用参数 |
|---|---|---|
| `create` | 新建项目（交互式或全参） | `--id --name --base-url --owner --requirements-file --force` |
| `list` | 列出已接入项目 | — |
| `info <id>` | 查看项目详情 | — |
| `run <id>` | **全流程**：需求→用例→接口→回归→报告 | `--llm --agentic --perf --web --explore --quality-min N` |
| `regression <id>` | 仅核心业务回归（适合常态化门禁） | — |
| `rerun <id>` | 重跑单个场景，结果**合并**回报告（不覆盖） | `--scene <场景名>` |
| `perf-security <id>` | ④ 性能与安全冒烟 | `--only perf\|security --users N --iterations N` |
| `web <id>` | ⑤ Web UI 冒烟 | `--only <场景/标签> --headed --browser chromium\|firefox\|webkit` |
| `explore <id>` | ⑥ AI 探索测试（**可降级阶段**，非门禁） | `--mission --max-steps N --timeout SEC --headed --json` |
| `defects <id>` | 由最近一次结果生成缺陷草稿（不重跑） | `--json` |
| `dashboard` | 跨项目总览看板（三组门禁状态） | — |

常用组合：

```bash
python project_manager.py run mall-admin --llm --agentic --perf --web   # 全开
python project_manager.py regression mall-admin                          # 只跑门禁
python project_manager.py perf-security mall-admin --only security        # 只跑安全检查
python project_manager.py web mall-admin --only login --headed            # 有头调试单个场景
```

---

## 9. Web 控制台

```bash
.venv\Scripts\python run_console.py            # 前台运行，Ctrl+C 停止
.venv\Scripts\python run_console.py --detach   # 后台常驻（日志写 web_server.log）
# 打开 http://127.0.0.1:8765
```

也可以直接 `python web_console/app.py`；`run_console.py` 只是多做了两件事——启动前先探活（已在跑就不重复起）、
`--detach` 用 WMI 创建进程以便脱离终端常驻。

八个功能页：

| 页面 | 能力 |
|---|---|
| **项目** | 项目卡片 + 门禁徽章 + 概览统计；一键跑全流程 / 核心回归（后台执行 + 实时日志）；新建项目向导 |
| **任务** | 执行记录列表（全流程 / 回归 / 性能安全 / Web）+ 实时滚动日志 |
| **报告** | 回归趋势与通过率、用例卡片、质量分趋势、证据链 |
| **自动化** | 核心回归场景清单（api_smoke / pytest_marker）与 Web UI 场景清单、类型与最近结果、一键运行 |
| **门禁** | 三道门禁（核心回归 / 性能与安全 / Web 冒烟）的三态结论矩阵 + 可复制摘要；与 `gate_notify.py` **同一套判定** |
| **资料库** | 浏览 `docs/` 下的 Markdown 并在线渲染 |
| **技能** | 列出 `agent-skills/` 技能，支持启用 / 停用 |
| **模型维护** | 查看各 LLM 提供商的密钥配置状态，设置默认 provider / model / base_url |

> 控制台只做**薄封装**：所有执行仍走 `project_manager.py`，与 CLI 同源，不会出现两套结果。

**访问鉴权（默认关闭）**：只要把端口暴露到局域网，它就变成"谁能访问谁就能触发任意测试任务"的入口。
在 `.env` 加一行 `STA_CONSOLE_TOKEN=<随机长字符串>` 并重启即可开启（不配或写 `off` → 行为不变）。

| 行为 | 说明 |
|---|---|
| 已开启 · 未登录 | `/api/*` 返回 401 JSON，页面重定向到 `/login` |
| 已开启 · 已登录 | 会话记录 token 的 `sha256` 指纹，**换 token 会让旧会话失效** |
| 免鉴权路径 | `/login`、`/logout`、`/healthz`、`/api/auth/status`、`/static/` |
| 其他 | 定长比较防时序攻击；`next` 只接受站内相对路径；token 不写前端、不写日志、不回显 |

---

## 10. CI 与通知

> ⚠️ GitHub Actions **只读仓库根 `.github/workflows/`**。此前模板放在扩展目录里，**从未真正执行过**；
> 现已全部迁入根 workflow。

| 作业 | 硬门禁 | 说明 |
|---|---|---|
| `产品单测（硬门禁）` | ✅ | 本仓库自有单测 + 评测基线（456 项，无 `\|\| true`），并产出 Allure 与 HTML 聚合制品 |
| `基座遗留测试（非门禁）` | ❌ | 上游基座遗留测试，`continue-on-error` 只为可见性，**不拿第三方问题卡交付** |
| `项目级门禁` | ✅（需配置环境） | 核心回归 / 性能安全 / Web 冒烟，三步收齐结论后统一判定 |
| `通知` | ❌ | 汇总三态结论后发钉钉 + 163 邮件 |

**三态判定**：✅ 通过 / ❌ 未通过 / ⏭ **未执行（不算绿）**；另设「— 未配置」**不参与判定**。
优先级 `fail > not_run > pass`。**未配置环境时不静默判绿**：缺 `STA_PROJECT_ID` / `STA_GATE_BASE_URL`
时显式打警告并跳过，而不是"没有失败 = 通过"。

**日志读不到就自己造出口**：GitHub 的 REST 日志接口要管理员权限、网页日志页也要登录；
唯一公开可读的是 `check-runs/{id}/annotations`。因此 CI 里加了一步 `if: failure()` 的
**失败摘要写入注解**（抓 `FAILED / ERROR / E` 行写成 `::error::`），无需登录即可定位失败用例。

```bash
# 本地预览门禁摘要（不发通知，无凭据也能跑）
python extensions/reporting/gate_notify.py --dry-run
python extensions/reporting/gate_notify.py --dry-run --project mall-admin --fail-on-gate
```

所需 Variables / Secrets 见 `extensions/reporting/README.md`。

---

## 11. Agent Skills

`agent-skills/` 遵循 agentskills.io 的 `SKILL.md` 规范，基座运行时通过 `fetch_agent_skill` /
`run_agent_skill_script` 自动加载；控制台「技能」页可查看与启停。当前预置 6 个：

| 技能 | 用途 |
|---|---|
| `software-testing` | 软件测试全流程知识体系与文档产出 |
| `qa-test-design` | 需求 / 风险 → 测试点 → 用例设计与评审 |
| `requirements-to-cases` | 需求到用例的转换方法论 |
| `api-test-design` | 接口测试设计与断言策略 |
| `web-automation` | Web 自动化与定位器策略 |
| `ci-gate-design` | CI 门禁设计与三态判定 |

技能目录由 `extensions/skills/registry.py` 扫描生成 `agent-skills/skills_catalog.json`
（路径统一为 POSIX 相对路径，避免跨平台不可读）。

---

## 12. 评测体系

`tests/eval/` 提供三层评测，与 456 项单测一起进 CI 硬门禁：

| 层 | 内容 |
|---|---|
| L1 | 产物断言（生成的用例结构、质量分是否符合预期） |
| L2 | 流程冒烟（`test_pipeline_smoke.py`：端到端跑通不报错） |
| L3 | `llm_judge.py` 语义质量评分（无 key 时 `enabled=False` 并跳过，**不抛异常**） |

> ⚠️ **用 judge 比较质量必须 `--repeat N` 取均值**：单次方差很大，曾据此得出"多步自审不如单步"的**相反结论**。
> 取均值后的真实排序：多步自审 96.0 > 单步 LLM 94.3 >> 规则版 79.7。

---

## 13. 贯穿全项目的判据（防假绿 / 防假红）

这些不是某一模块的实现细节，而是**所有模块共用**的判定原则——改动任何模块都应先对照这一节：

| # | 判据 | 为什么 |
|---|---|---|
| 1 | **环境不可达不判绿** | 全部 SKIP 时 `all_pass=False` 且退出码非 0。"没跑"不能等于"通过" |
| 2 | **严格分流**：连接级错误 = SKIP，HTTP 4xx/5xx = FAIL | 混在一起，要么"服务没起"报成一堆失败带偏排查，要么"应用 500"被悄悄跳过 |
| 3 | **配置问题单列报出** | 静默跳过 = 悄悄放松门禁，是最危险的一种 |
| 4 | **汇总入口必须与列表页同一套过滤** | 被跳过的项目要**如实交代数量与名单**；只隐藏不交代是假绿的另一种形态 |
| 5 | **判定口径唯一** | 两套口径比没有口径更糟：`gate_notify.collect_gates` 是唯一判定源 |
| 6 | **算不出就不猜** | 无法判定的维度记「未计分」并说明，总分按剩余维度归一化，不填 0 也不填 100 |
| 7 | **不自动替人做决定** | 不自动提单、级别只是建议、门禁阈值默认不卡——把判断权留给人 |
| 8 | **测试不能污染工作区** | 测试产物落临时目录；靠"事后 `git checkout` 还原"= 失败静默 + 偶发假红 |
| 9 | **出错必出声**：诊断走日志、结果走 stdout | 静默 `except` 与裸 `print` 让"红了却定位不到"反复发生。诊断进 stderr + 日志文件（带 `run_id` 可跨进程串起同一次运行），**呈现类输出**留在 stdout 以便 `\|` 管道接走 |
| 10 | **一次运行一条线（run_id）** | 控制台任务用 `tid` 作 run_id，经 `STA_RUN_ID` 传给子进程；日志每行带它，跨进程可检索。守护见 `tests/test_obs.py` |
| 11 | **能改状态的动作必须留痕、且能被一键禁掉** | 控制台一旦暴露到局域网，"谁都点得动、出事了查不到"就是两个缺口。所以：`/api/*` 的写操作各留一行 `audit.jsonl`（谁/何时/哪个项目/结果，**被拦下的尝试也留痕**）；`STA_CONSOLE_READONLY=1` 一键切成只读（写 403、读放行、登录不受影响）。守护见 `tests/test_console_guard.py` |
| 12 | **对被测系统不信任，对自己也不信任** | 我们要求"HTTP 4xx/5xx 一律 FAIL、环境不可达绝不判绿"，却曾把"停不停/成没成"交给 agent 自评 —— 同一个团队两套尺度。所以探索循环必须有**硬**资源上限（`AGENT_MAX_TURNS` / `AGENT_TOKEN_BUDGET`，到顶即止且不再问模型）、结束原因**枚举化**（含 `max-turns` / `budget-exhausted`）、"达成"由**程序**按确定性断言判定（判不了记 `unknown`，**算不出就不猜**）。守护见 `tests/test_guardrails.py` |
| 13 | **降级要出声，且"未执行"绝不渲染成绿灯** | 接进来的智能体能力只要"跑不动就悄悄跳过"，整套防假绿体系就在最不确定的一环破功。所以：① 不可用要**降级**（不是失败）并写明**枚举化原因**（缺 key / 超时 / 异常 / 未配置）；② 契约里只有显式 `status == "ok"` 才算"跑了"，缺字段一律不算；③ 消费侧**跑前清旧产物**（否则崩溃会被上一轮成功伪装成本轮）；④ 报告与 `run_meta` 照实写"已降级 + 原因"，退出码区分「未执行(3)」与「失败(1)」。守护见 `tests/test_agentic_contract.py` / `test_agentic_entry.py` / `test_agentic_wiring.py` |

> 第 9 条的两个落点：`print` 负责**给人看的结果呈现**（`list` 表格、`defects` 的 Markdown、`--json` 载荷），
> 日志负责**排障用的诊断**（进度、判定、降级、异常）。这是**两种受众**——所以代码里仍有 `print`，
> 是约定而非疏漏；但 `print(..., file=sys.stderr)` 与 `except` 块内的 `print` 已被测试禁掉。

---

## 14. 密钥与 .gitignore 约定

下列内容**禁止入库**（已在 `.gitignore` 中排除）：

- `.env`、`config.yaml`、`mcp_servers.json`、`models_config.json`（保留对应 `.example`）
- `projects/`（含各项目配置、需求、回归清单与产物）
- `runs.db`、`.workbuddy/`、Allure 结果、截图预览
- `dist/`、`build/`、`*.spec`

提交前请再次确认：`git status` 中不应出现 `.env`、密钥文件或 `projects/` 下的任何内容。
本项目一律**显式路径 `git add`**，不使用 `git add -A`。

---

## 15. 路线图

| 阶段 | 状态 |
|---|---|
| ① 接口自动化（含核心回归门禁与单场景重跑合并） | ✅ 完成 |
| ② 需求 → 用例（规则版 + 多 provider LLM + 自动降级 + 多步自审） | ✅ 完成 |
| ③ 报告与 CI（HTML 报告 / 跨项目看板 / 失败聚类与趋势 / 钉钉与邮件通知 / 根 workflow 三态门禁） | ✅ 完成 |
| ④ 性能与安全冒烟（三道防误判闸门，接入 CLI / 报告 / 看板 / 控制台） | ✅ 完成 |
| ⑤ Web UI 冒烟（Playwright 声明式场景，与基座探索式测试互补） | ✅ 完成 |
| 记忆闭环：失败聚类 → 项目知识 → **覆盖校验与回灌补齐** | ✅ 完成 |
| 用例结构质量分、缺陷草稿、失败新旧对比、生成溯源 | ✅ 完成 |
| 可视化控制台（8 页）与访问鉴权 | ✅ 完成 |
| CI 首次全绿（2026-09-12），三态门禁 + 失败注解可观测 | ✅ 完成 |
| S1：把基座 Supervisor 编排接为 `run` 的可选阶段（需 LLM key） | ⏳ 待办 |
| S2：需求 → 用例默认走 LLM（规则版作降级，需 key，且需 CI 无 key 时仍走规则版） | ⏳ 待办 |

详见 [`docs/AGENT_REVIEW_AND_ROADMAP.md`](docs/AGENT_REVIEW_AND_ROADMAP.md)。

---

## 16. 许可证

本项目基于 [agentic-test-explorer](https://github.com/srbarrios/agentic-test-explorer)（MIT）二次开发，
遵循 **MIT 许可证**。基座原版说明见
[`docs/BASE_README_agentic-test-explorer.md`](docs/BASE_README_agentic-test-explorer.md)。
