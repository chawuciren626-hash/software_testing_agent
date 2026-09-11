# 软件测试智能体（Software Testing Agent）

> 基于开源基座 [`srbarrios/agentic-test-explorer`](https://github.com/srbarrios/agentic-test-explorer)（MIT，v0.2.0）不断完善而来的**软件测试全流程智能体**。
> 目标：类似 deepseek harness / WorkBuddy，用 **skills + 多智能体** 串起「需求分析 → 测试用例 → 接口/Web 自动化 → 测试报告 → 性能/安全探索」。

---

## 1. 定位

| 维度 | 说明 |
|---|---|
| 基座 | agentic-test-explorer：LangGraph 多人格智能体 + Playwright + MCP + Agent Skills + Langmem 记忆 |
| 我们的增量 | 在基座之上扩展五大能力（见第 4 节 `extensions/`） |
| 已配资源 | 本项目已配置的专家与技能，通过基座的 **MCP / Agent Skills** 机制接入 |
| 运行要求 | 上层智能体需 Claude/Gemini key；**接口自动化无需 LLM 即可跑** |

---

## 2. 架构（总览）

```
                    ┌──────────────────────────────────────────┐
                    │           软件测试智能体 (本项目)           │
                    └──────────────────────────────────────────┘
                                     │
        ┌───────────────┬────────────┬────────────┬───────────────┬──────────────┐
        ▼               ▼            ▼            ▼               ▼              ▼
   ① 接口自动化    ② 需求→用例   ③ 报告与CI   ④ 性能/安全    ⑤ Web 冒烟    基座 Web 探索
   (pytest+requests) (规则/LLM)   (Allure/钉钉/邮件) (线程池+安全检查) (Playwright声明式) (Playwright多人格)
        │               │            │            │               │              │
        └───────────────┴─────┬──────┴────────────┴───────────────┴──────────────┘
                              ▼
              agent-skills/  +  mcp_servers.json   ← 你已配置的 skills/experts 接入点
                              ▼
              agentic-explorer (LangGraph Swarm + 记忆)
```

---

## 3. 目录结构

```
.
├── docs/ARCHITECTURE_AND_EXTENSIONS.md   # 架构与扩展点分析（评审材料）
├── agent-skills/                         # Bring-Your-Own Skills（智能体可调）
│   ├── api-test-design/                  #   接口测试设计方法论
│   └── requirements-to-cases/            #   需求→用例方法论
├── extensions/                           # 五大扩展能力
│   ├── api_testing/                      # ① 接口自动化（pytest+requests，可独立跑）
│   ├── requirements_to_cases/            # ② 需求→用例（规则版可跑，可选 LLM）
│   ├── reporting/                        # ③ 报告与 CI（HTML/Allure + 钉钉/163邮件）
│   ├── perf_security/                    # ④ 性能/安全冒烟（线程池压测 + 6 项安全检查）
│   └── web_testing/                      # ⑤ Web UI 冒烟（Playwright 声明式 YAML + 门禁）
├── extensions/regression/                # 核心业务回归执行器
├── projects/                             # 公司各类项目登记表（每项目一个目录）
│   └── <project_id>/                     #   project.yaml + requirements.md
│                                         #   + regression.yaml + web.yaml + artifacts/
├── missions/                             # 基座：YAML 测试任务
├── src/agentic_explorer/                 # 基座：核心代码
├── project_manager.py                    # 多项目对接统一入口（建/列/跑/回归/看板）
├── software_testing_agent.py             # 无 LLM 流水线入口
├── config.yaml / .env                    # 本地运行配置（已 gitignore）
├── README.md                             # 上游基座说明（保留）
└── README_CN.md                          # 本文件
```

---

## 4. 五大能力进度

| 能力 | 状态 | 交付内容 |
|---|---|---|
| ① 接口(API)自动化 | ✅ 可运行（不依赖 LLM） | `extensions/api_testing/`：conftest（数据隔离+可达性跳过）、登录/注册用例、Allure 接入点 |
| ② 需求分析→用例 | ✅ 规则版 + LLM 增强 | `extensions/requirements_to_cases/`：标准库生成器 + 多 provider LLM（OpenAI 兼容 / Gemini）+ 失败自动降级 |
| ③ 报告与CI增强 | ✅ 已落地 | `extensions/reporting/`（HTML/Allure 聚合 + 钉钉/163 邮件 + `gate_notify.py` 门禁摘要）+ **`.github/workflows/ci.yml`（根 workflow，真生效）** |
| ④ 性能/安全冒烟 | ✅ 已落地 | `extensions/perf_security/`：线程池并发（P50/P95/P99、错误率、吞吐、阈值门禁）+ 6 项安全检查；三道闸门防假绿与假红 |
| ⑤ Web UI 冒烟 | ✅ 已落地 | `extensions/web_testing/`：Playwright + 声明式 `web.yaml`；拒绝脆弱定位器、无断言不判绿、连接级错误与 HTTP 错误分流、失败留截图与可复现 `.spec.ts` |

> ①~⑤ 均可独立运行并接入 CLI / 报告 / 跨项目看板 / Web 控制台；
> ②的 LLM 增强与基座的智能体编排需 LLM key，其余能力**零 LLM 依赖**。

---

## 5. 快速开始

### 5.1 安装
```bash
python -m venv .venv && .venv/Scripts/activate
pip install -e .                       # 基座依赖（需网络）
pip install -r extensions/api_testing/requirements.txt
playwright install chromium
```

### 5.2 接口自动化（无需 LLM）
```bash
export BASE_URL="http://localhost:8080"
export APP_USERNAME="admin"
export APP_PASSWORD="macro123"
.venv/Scripts/python -m pytest extensions/api_testing -v
# 服务不可达时用例自动 skip
```

### 5.3 需求 → 用例（规则版，无需 LLM）
```bash
.venv/Scripts/python extensions/requirements_to_cases/generate_cases.py \
    --input extensions/requirements_to_cases/sample_requirements.md
```

### 5.4 上层智能体（需 LLM key）
在 `.env` 填入 `ANTHROPIC_API_KEY` 或 `GOOGLE_API_KEY`，然后：
```bash
agent-explorer --missions missions/new_user_agent.yaml --headed
```

### 5.5 可视化控制台（Web / 桌面客户端）
本项目自带一个可视化控制台，四种启动方式任选：

| 方式 | 命令 | 说明 |
|---|---|---|
| **桌面客户端（推荐）** | 双击 `launch_desktop.bat` 或 `.venv/Scripts/python web_console/desktop.py` | pywebview 原生窗口，无浏览器地址栏 |
| 应用模式（零依赖） | 双击 `launch_desktop_appmode.bat` | 用 Edge/Chrome `--app` 模式，视觉等同独立应用 |
| Web 服务 | `.venv/Scripts/python web_console/app.py` | 浏览器访问 http://127.0.0.1:8765 |
| 纯命令行 | `python project_manager.py run <项目ID>` | 适合 CI / 无人值守 |

界面采用 WorkBuddy 风格左侧分组导航，包含 6 个功能页：

| 页面 | 能力 |
|---|---|
| **项目** | 项目卡片 + 回归门禁徽章 + 概览统计；一键跑全流程/核心回归（后台执行 + 实时日志）；新建项目向导（填环境地址/认证/需求/密钥变量名）；查看报告 |
| **任务** | 执行记录列表（全流程/回归/看板）+ 实时滚动日志 |
| **自动化** | 各项目核心回归场景清单（冒烟/pytest marker/node）**与 Web UI 场景清单**、类型与最近结果、一键运行；无断言的 Web 场景会提前标红 |
| **资料库** | 浏览 `docs/` 下的 Markdown 文档并在线渲染阅读 |
| **技能** | 列出 `agent-skills/` 下技能，支持启用/停用（`.disabled` 开关） |
| **模型维护** | 查看 Anthropic/Google 密钥配置状态，设置默认 provider/model/base_url（存 `models_config.json`） |

> 依赖：`pip install flask pywebview`。桌面端与 Web 端共用同一套 Flask 代码
> （`web_console/app.py`），Web 层只做薄封装——所有执行仍走 `project_manager.py`，与 CLI 同源。

### 5.6 打包为独立 exe（免环境分发）
无需用户安装 Python/依赖，双击即用：

| 产物 | 构建命令 | 形态 |
|---|---|---|
| **Web 控制台** `dist/TestAgentConsole.exe`（~17MB） | `build/build_exe.py` | 启动后监听 `http://127.0.0.1:8765`，浏览器打开即可 |
| **桌面客户端** `dist/TestAgentDesktop.exe`（~20MB） | `build/build_desktop_exe.py` | 启动后弹出 pywebview 原生窗口（无地址栏），无图形环境则自动回退为仅服务模式 |

```bash
.venv/Scripts/python -m pip install pyinstaller
.venv/Scripts/python build/build_exe.py            # -> dist/TestAgentConsole.exe
.venv/Scripts/python build/build_desktop_exe.py    # -> dist/TestAgentDesktop.exe
```

- **运行数据隔离**：打包后 `projects/`、`models_config.json`、可写技能标记（`agent-skills/*.disabled`）落在本 exe 同目录
  （`STA_ROOT`），不污染仓库；首次启动会在同目录生成 `.env` 模板供填写密钥（仓库 `.env` **不会**被打进包，避免密钥随二进制泄露）。
- 打包时**显式排除** langchain/langgraph/openai/playwright 等 LLM 依赖（控制台/桌面端只走
  `create/list/regression/dashboard` 子命令，不触发上层智能体探索），体积与启动速度最优。
- `build_exe.py` / `build_desktop_exe.py` 使用系统临时目录作为 workpath、临时 `distpath`，规避对仓库内
  `dist/`、`build/` 的覆盖写保护，构建后自动拷贝回 `dist/`。
- 桌面端打包要点：入口用仓库根的薄封装 `run_desktop.py`（而非直接打 `web_console/desktop.py`），
  否则冻结后 `import web_console.app` 会因包层级丢失而报 `ModuleNotFoundError`；`web_console/` 已补 `__init__.py`。

---

## 6. 多项目对接（Project Registry）

对接公司各类项目：**新建项目 = 填写项目信息（测试环境地址、需求等）**，即可对该项目执行全流程测试与核心业务回归。

```bash
python project_manager.py create              # 交互式填写项目信息（也支持全参传入）
python project_manager.py list                # 列出已接入项目
python project_manager.py run <id>            # 全流程：需求->用例 -> 接口自动化 -> 核心回归 -> 报告
python project_manager.py run <id> --perf --web  # 全流程 + ④性能/安全 + ⑤Web 冒烟
python project_manager.py regression <id>     # 仅核心业务回归（适合常态化门禁）
python project_manager.py perf-security <id>  # ④ 性能 + 安全冒烟（--only perf|security）
python project_manager.py web <id>            # ⑤ Web UI 冒烟（--only 场景名/标签、--headed、--browser）
python project_manager.py dashboard           # 跨项目总览看板（回归 / 性能安全 / Web UI 三组门禁）
```

每个公司项目 = `projects/<id>/` 四件套：

| 文件 | 作用 | 是否入库 |
|---|---|---|
| `project.yaml` | 元数据 + **测试环境地址** + 认证方式 | ✅ 入库 |
| `requirements.md` | 项目需求（驱动需求→用例） | ✅ 入库 |
| `regression.yaml` | 核心业务回归场景声明 | ✅ 入库 |
| `artifacts/` | 用例/报告/Allure 产物 | ❌ 不入库 |

**核心回归支持三类回归项**（在 `regression.yaml` 中声明）：

| type | 说明 |
|---|---|
| `api_smoke` | 声明式 HTTP 检查（method / path / expect_status），body 支持 `{{username}}` 占位 |
| `pytest_marker` | **复用已有 pytest 用例**，按 marker 筛选（如 `-m smoke`）—— 推荐 |
| `pytest_node` | 复用已有 pytest 用例，指定 node id |

推荐后两种：让核心回归与已写好的自动化用例**同源**，不必重复声明。

**门禁语义**：无 FAIL 且确有实际执行才算通过；环境不可达导致全部 SKIP 时**不判绿**，避免 CI 给出虚假安全信号。

**密钥分离**：`project.yaml` 只存环境变量**名**（如 `username_env: APP_USERNAME`），真实口令/token 写在根 `.env`（已 gitignore），`project_manager` 启动时自动载入。

详见 `docs/PROJECT_REGISTRY.md`。

---

## 6.5 CI 与通知（`.github/workflows/ci.yml`）

> ⚠️ 架构提示：GitHub Actions **只读仓库根 `.github/workflows/`**。此前模板放在
> `extensions/reporting/.github/workflows/`，位置不对 → 从来没有真正跑过；
> 且模板用 `|| true` 吞掉 pytest 失败，与本项目「环境不可达不判绿」的口径直接冲突。现已删除，全部迁入根 workflow。

| 作业 | 硬门禁 | 说明 |
|---|---|---|
| `unit-tests` | ✅ | 本仓库自有单测 + 评测基线（无 `\|\| true`，失败即红）；产出 Allure + HTML 聚合制品 |
| `base-tests` | ❌ | fork 上游基座的遗留测试，`continue-on-error` 只为可见性，不拿第三方代码问题卡交付 |
| `project-gates` | ✅（需已配置环境） | 核心回归 / 性能安全 / Web 冒烟；三步都 `continue-on-error` 收齐结论后统一判定 |
| `notify` | ❌ | 复用上游摘要发钉钉 + 163 邮件 |

**未配置环境时不静默判绿**：缺 `STA_PROJECT_ID` / `STA_GATE_BASE_URL` 时显式打 GitHub 警告并跳过，
而不是「没有失败 = 通过」。

```bash
# 本地预览门禁摘要（不发通知，无凭据也能跑）
python extensions/reporting/gate_notify.py --dry-run
python extensions/reporting/gate_notify.py --dry-run --project mall-admin --fail-on-gate
```

需要的 Variables / Secrets 见 `extensions/reporting/README.md`。

---

## 7. 接入你已配置的 skills / experts

基座原生支持两种接入：
1. **Agent Skills**：把测试方法论文档放到 `agent-skills/<name>/SKILL.md`（已预置 2 个），智能体运行时经 `fetch_agent_skill` 自动加载。
2. **MCP 服务器**：在 `mcp_servers.json` 填入你已有的 MCP 连接器（专家能力 / 内部工具），智能体经 `get_mcp_tools()` 调用。

> 待确认：你配置的专家/技能是经 MCP 暴露，还是由编排层（我）直接调用？见 `docs/ARCHITECTURE_AND_EXTENSIONS.md` 第 4 节。

---

## 8. 路线图

1. ~~先跑通①接口自动化~~ → ✅ 已完成（含核心回归门禁与单场景重跑合并）。
2. ~~补②需求用例，并与①打通~~ → ✅ 已完成（规则版 + 多 provider LLM + 自动降级）。
3. ~~完善③报告与 CI~~ → ✅ 已完成（项目级 HTML 报告、跨项目看板、失败聚类与趋势、钉钉/163 通知、**根 workflow 真跑起来 + 门禁摘要三态判定**）。
4. ~~补④性能/安全探索~~ → ✅ 已完成（冒烟执行器 + 三道防误判闸门，接入 CLI/报告/看板/控制台）。
5. ~~补⑤Web 自动化（可门禁的那一半）~~ → ✅ 已完成（Playwright 声明式场景，与基座的探索式测试互补）。
6. 你提供 LLM key 后，把整套串成「需求 → 用例 → 接口/Web 自动化 → 报告」的智能体闭环。
