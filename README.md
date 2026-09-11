# software_testing_agent · 软件测试智能体

一个从 0 到 1 搭建的「软件测试智能体」：以开源项目 **agentic-test-explorer**（MIT）为基座，
叠加**接口自动化、需求→用例、报告/CI、可视化 Web 控制台**等扩展能力，把测试全流程串成闭环：

> 需求分析 → 测试用例 → 接口 / Web 自动化 → 测试报告 → 性能 / 安全

基座提供「LLM 驱动、用真实浏览器探索任意 Web 应用」的核心能力（LangGraph Swarm + Playwright + Claude/Gemini）；
本仓库在其之上补齐了工程化落地所需的 CLI 编排、接口回归、需求转用例与 Web 控制台。

---

## ✨ 核心特性

- **统一项目登记与多项目治理**：一个 `project_manager` 命令管理公司内所有被测项目（元数据 / 测试环境 / 认证 / 需求 / 回归清单）。
- **接口自动化（零 LLM 依赖）**：`extensions/api_testing` 基于 `pytest + requests`，不需要任何 LLM Key 即可独立跑通；被测服务不可达时自动跳过，不污染结果。
- **需求 → 测试用例生成**：`extensions/requirements_to_cases` 提供零依赖的规则版生成器（按关键词拆需求草稿）；可选接入 **LLM 增强**（`--llm`），**默认走 OpenAI 兼容协议（DeepSeek / 通义千问 / 智谱 GLM / Kimi / 本地 Ollama 等）**，也可切到 Gemini；**任何失败（缺 key / 限流 / 网络）都会自动降级规则版**，绝不中断流水线。
- **核心业务回归 + 门禁**：声明式回归清单（`api_smoke` / `pytest_marker` / `pytest_node`），环境不可达导致全 SKIP 时**不判绿**，避免 CI 假绿。
- **可视化 Web 控制台**：`web_console`（Flask + 原生前端），含项目 / 任务 / 自动化 / 资料库 / 技能 / 模型维护 6 大页面，报告弹窗内可查看分组用例卡片与回归结果。
- **报告增强**：失败原因聚类、新增/老毛病标记、跨项目趋势对比、不稳定场景洞察。
- **跨项目看板**：一键生成各项目最近一次回归与门禁状态的总览。

---

## 📁 目录结构

```
software_testing_agent/
├── project_manager.py          # 统一 CLI 入口（项目登记 / 全流程 / 回归 / 看板）
├── software_testing_agent.py   # 基座 LangGraph 探索测试入口
├── config.yaml / .example      # 基座配置（app / auth / skills / llm）
├── mcp_servers.json / .example # MCP 服务器配置
├── extensions/                 # 本仓库新增的扩展能力
│   ├── api_testing/            # 接口自动化（pytest + requests）
│   ├── requirements_to_cases/  # 需求 → 测试用例生成
│   ├── reporting/              # 报告与 CI 增强
│   ├── perf_security/          # 性能 / 安全冒烟（线程池压测 + 安全检查）
│   └── regression/             # 核心业务回归执行引擎
├── web_console/                # 可视化控制台
│   ├── app.py                  # Flask 后端（薄封装，调用 project_manager）
│   ├── run_store.py            # 任务历史 / 回归快照 SQLite 持久化
│   ├── desktop.py              # 桌面端外壳（可选）
│   └── templates/index.html   # 单页前端（原生 JS，无框架）
├── agent-skills/               # Bring-Your-Own Skills（SKILL.md 规范）
├── docs/                       # 规划 / 分析文档
├── projects/                   # 本地项目登记（⚠️ 不入库，见下方说明）
├── src/ tests/ missions/       # 基座源码 / 测试 / 任务
├── AGENTS.md                   # 基座 Agent 说明
├── ARCHITECTURE_GUIDE.md      # 基座架构指南
└── LICENSE                     # MIT
```

> 基座（agentic-test-explorer）的原始说明已保留在 [`docs/BASE_README_agentic-test-explorer.md`](docs/BASE_README_agentic-test-explorer.md)，
> 架构与扩展规划见 [`docs/ARCHITECTURE_AND_EXTENSIONS.md`](docs/ARCHITECTURE_AND_EXTENSIONS.md)，项目登记模型见 [`docs/PROJECT_REGISTRY.md`](docs/PROJECT_REGISTRY.md)。

---

## 🚀 快速开始

### 1. 环境要求
- Python 3.12+
- 建议使用虚拟环境（仓库根 `.venv`）

### 2. 安装依赖
```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
playwright install chromium     # 仅需 Web 探索/自动化时
```

### 3. 配置（复制示例并填入你的值）
```bash
cp .env.example .env            # 填入 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL（需求→用例增强用，可选；接口自动化不需要）
cp config.yaml.example config.yaml
cp mcp_servers.json.example mcp_servers.json
```
> 接口自动化（API）**无需 LLM Key**；只有在运行基座「LLM 驱动探索测试」时才需要 Key。

### 4. 启动 Web 控制台
```bash
.venv\Scripts\python web_console/app.py
# 打开 http://127.0.0.1:8765
```
控制台提供：项目登记、任务执行与历史、报告查看与导出、失败聚类、需求转用例、技能/模型维护等。

### 5. CLI 速查
```bash
python project_manager.py create        # 交互式新建项目
python project_manager.py list           # 列出已接入项目
python project_manager.py info <id>      # 查看项目详情
python project_manager.py run <id>       # 全流程：需求→用例→接口→回归→报告
python project_manager.py run <id> --llm  # 全流程，且需求→用例采用 LLM 智能生成（默认 OpenAI 兼容，失败自动降级规则版）
python project_manager.py regression <id># 仅核心业务回归
python project_manager.py rerun <id> --scene <场景名>  # 重跑单个核心场景
python project_manager.py dashboard      # 生成跨项目总览看板
```

---

## 🗂️ 项目模型（`projects/<id>/`）

每个公司项目在本地登记为一个目录，包含：

| 文件 | 说明 |
| --- | --- |
| `project.yaml` | 元数据 + 测试环境地址 + 认证（**只存环境变量名，真实密钥放根 `.env`**） |
| `requirements.md` | 需求描述（用于需求→用例） |
| `regression.yaml` | 核心业务场景声明（api_smoke / pytest_marker / pytest_node） |
| `artifacts/` | 产物（报告、回归 JSON、Allure 结果，**不入库**） |

> ⚠️ **演示 / 示例测试项目属于本地运行时数据，不纳入本仓库。**
> `.gitignore` 已整体排除 `projects/`，避免把个人/公司项目配置、密钥与产物提交到公开仓库。
> 接入你自己的项目时，在本地 `projects/` 下登记即可，仓库只保留框架与扩展代码。

---

## 🧩 扩展模块（`extensions/`）

- **api_testing**：`pytest + requests` 接口自动化，声明式 `expect_json`（支持点路径与 `__not_null__`），服务不可达自动 skip。
- **requirements_to_cases**：把 `requirements.md` 按规则拆成用例草稿（功能/边界/异常 + 优先级 + 可自动化标记），无需 LLM；加 `--llm` 可走 LLM 增强。**默认 provider=openai**（OpenAI 兼容协议），配置 `LLM_API_KEY` + `LLM_BASE_URL` + `LLM_MODEL` 即可用 DeepSeek / 通义千问 / 智谱 GLM / Kimi 等；设 `LLM_PROVIDER=gemini` + `GOOGLE_API_KEY` 则走 Gemini（可用 `GEMINI_MODEL` / `GEMINI_API_BASE` 覆盖）；任何失败自动降级规则版。
- **reporting**：生成项目级 HTML 报告（分组用例卡片 + 回归表格 + 门禁状态）。
- **perf_security**：性能 / 安全**冒烟**执行器（`run_perf_security.py`，零新增依赖）。
  性能用线程池并发，给 P50/P95/P99、错误率、吞吐与**阈值门禁**；安全查 6 项（未授权访问、错口令、SQL 注入、
  堆栈泄露、用户枚举、安全响应头）。压测目标默认**从 `regression.yaml` 的只读接口自动派生**（写操作默认跳过）。
  三道闸门防误判：业务码判错（HTTP 200 但 code=500 仍算失败）、环境不可达不判绿、基线登录失败时跳过而不是报假漏洞。
  CLI：`python project_manager.py perf-security <项目ID>`（`--only perf|security`、`--users`、`--iterations`），
  或 `python project_manager.py run <项目ID> --perf` 并入全流程。`locustfile_api.py` 保留作专职长压入口。
- **regression**：核心业务回归执行与单场景重跑合并。

---

## 🤖 多模型接入（需求→用例 LLM 增强）

需求 → 用例的 LLM 增强**不绑定任何厂商**，通过环境变量选择模型，且失败永远降级规则版：

| 环境变量 | 说明 | 默认值 |
| --- | --- | --- |
| `LLM_PROVIDER` | `openai`（默认）或 `gemini` | `openai` |
| `LLM_API_KEY` | OpenAI 兼容平台 key（本地 Ollama 可省略） | 空 |
| `LLM_BASE_URL` | OpenAI 兼容 endpoint 的 base，如 `https://api.deepseek.com/v1`、`http://localhost:11434/v1` | OpenAI 官方 |
| `LLM_MODEL` | 模型名，如 `deepseek-chat`、`qwen-plus`、`glm-4-flash`、`moonshot-v1-8k` | `deepseek-chat` |
| `LLM_TIMEOUT` | 单次 LLM 调用超时（秒）。多步编排或慢模型可调大（如 `120`） | `60` |
| `GOOGLE_API_KEY` / `GEMINI_MODEL` / `GEMINI_API_BASE` | 仅 `LLM_PROVIDER=gemini` 时使用 | - |

**常用接入示例：**

```bash
# 1) DeepSeek（OpenAI 兼容，便宜、国内稳定）
LLM_PROVIDER=openai LLM_API_KEY=sk-xxx LLM_BASE_URL=https://api.deepseek.com/v1 LLM_MODEL=deepseek-chat \
  python project_manager.py run <id> --llm

# 2) 通义千问 Qwen / 智谱 GLM / Kimi：仅换 BASE_URL 与 MODEL 即可
#    Qwen:   LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1  LLM_MODEL=qwen-plus
#    GLM:    LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4            LLM_MODEL=glm-4-flash
#    Kimi:   LLM_BASE_URL=https://api.moonshot.cn/v1                       LLM_MODEL=moonshot-v1-8k

# 3) 本地 Ollama（完全离线、零 key）
LLM_PROVIDER=openai LLM_BASE_URL=http://localhost:11434/v1 LLM_MODEL=llama3 \
  python generate_cases.py --input req.md --llm

# 4) 仍想用 Gemini
LLM_PROVIDER=gemini GOOGLE_API_KEY=xxx python project_manager.py run <id> --llm
```

> 切换模型**只需改环境变量**，代码无需改动；Web 控制台「模型维护」页可查看当前 provider 与 key 配置状态。

### 🧠 智能体多步自审编排（`--agentic`）

在 LLM 增强基础上，可开启**多步自审编排**（P1 智能内核），把单次调用升级为四步闭环：

```
需求分析 → 初版用例 → 自评审（注入历史易错点）→ 终版优化
```

- 命令行：`python project_manager.py run <id> --llm --agentic`
- Web 控制台：项目详情 → 「多步自审编排」勾选框（需先配置 LLM）
- 质量更高（自评审会补齐边界/异常、去冗余），代价是**串行调用 LLM 4 次**（更慢/更费额度）；
  模型较慢时请调大 `LLM_TIMEOUT`。**任意一步失败都会自动降级规则版**，不中断流水线。
- 若项目已生成 `lessons.md`（情景记忆），历史易错点会在评审/优化步被注入，形成"越跑越准"的闭环。

---

## 🔐 密钥与 `.gitignore` 约定

下列内容**禁止入库**（已在 `.gitignore` 中排除）：

- `.env`、`config.yaml`、`mcp_servers.json`、`models_config.json`（保留对应 `.example`）
- `projects/`（含各项目配置、需求、回归清单与产物）
- `runs.db`、`.workbuddy/`、Allure 结果、截图预览
- `dist/`、`build/`、`*.spec`、桌面启动脚本（本仓库以 Web 端为主）

提交前请再次确认：`git status` 中不应出现 `.env`、密钥文件或 `projects/` 下的任何内容。

---

## 📜 许可证

本项目基于 [agentic-test-explorer](https://github.com/srbarrios/agentic-test-explorer)（MIT）二次开发，
遵循 **MIT 许可证**。基座原版说明见 [`docs/BASE_README_agentic-test-explorer.md`](docs/BASE_README_agentic-test-explorer.md)。
