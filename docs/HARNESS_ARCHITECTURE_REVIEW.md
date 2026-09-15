# 软件测试智能体 · Agent Harness 六层架构审阅

> 审阅人：墨衡（Agent Harness 架构设计师）｜审阅日期：2026-09-14
> 审阅镜头：**H =（E 执行循环 / T 工具注册 / C 上下文管理 / S 状态存储 / L 生命周期钩子 / V 评估接口）+ P 架构范式**
> 被审对象：`D:\WorkBuddy\2026-09-09-16-35-03`（fork 自 `srbarrios/agentic-test-explorer`）
> 证据级别标注：**【已复核】** = 本人在本次审阅中直接读代码确认；**【调研】** = 由自动调研子代理给出、未逐行复核。
> 本文件是**审阅与建议**，不是设计文档；不含任何未经用户拍板的实施决定。

---

## 0. 一句话结论

**这是一套工程纪律远超同类 PoC 的"确定性测试流水线"，但它现在由两个互不连通的世界组成：**

- **A 世界（运行态）**：`extensions/*` + `project_manager.py` + `web_console/`。确定性、可复现、判据克制——**V 层与 L 层很有纪律，但 E 层是固定管线（无自适应）、C 层无预算**。
- **B 世界（休眠态）**：`src/agentic_explorer/`。LLM 编排、四层记忆、脑手分离——**E 层与 C 层有雏形，但目标达成靠模型自评、步数上限只做"软重置"不硬停**。

两世界之间 **零 import**（【已复核】`project_manager.py` / `web_console/*.py` 对 langgraph/langchain/langmem 无任何引用；构建脚本显式排除这些重依赖）。

> 所以路线图里的 S1「把 Supervisor 接为 run 的可选阶段」，本质不是"接线"，而是**把 B 世界最薄弱的一环（模型自评终止 + 无硬上限 + 无预算）引进 A 世界最讲纪律的地方**。
> 我的建议是：**先给 B 世界的循环补上 harness 侧护栏，再以独立入口接入**——理由见 §3.2 与 §5 D2。

---

## 1. 需求规格（从代码与既有文档反推，供校准）

| 维度 | 内容 | 状态 |
|---|---|---|
| 目标与用途 | 软件测试全流程 AI 智能体：需求分析 → 用例输出 → 接口/Web 自动化 → 报告 → 缺陷草稿 | 已确认 |
| 成功标准 | 提升测试效率、保障交付质量；现有可量化判据 = 门禁三态 + 结构质量分趋势 + 重点覆盖跨轮累积 | 部分确认（缺"效率"的可达量化口径 → §10） |
| 任务结构 | 固定四阶段流水线 + 可选阶段（perf / web / llm / agentic）+ 三项后处理（diff / defects / report） | 已确认 |
| 数据/资源边界 | 本地 mall-admin（localhost:8080）、多项目 `projects/<id>/`、凭据在根 `.env` | 已确认 |
| 硬约束 | 只交付 Web 端；显式路径 `git add`；不推上游仓库；CI 硬门禁 | 已确认 |
| 用户与场景 | 本人（转型期测试工程师，本机单人为主）；团队化为未来态 | 已确认 |
| 边界（不做什么） | 不自动提单、不做桌面打包、不接技能市场、AI 不替代人工测试结论 | 已确认 |
| 技术栈约束 | Python 3.13 venv / Flask / pytest / Playwright / SQLite；核心不绑定任何框架；基座 LLM 层为可选重依赖 | 已确认 |

---

## 2. 六层 + P 现状诊断

### 2.1 E 执行循环

| 项 | 现状 | 证据 |
|---|---|---|
| 编排形态 | **硬编码串行脚本**，步骤顺序不可配：`_step_api → _step_regression →(_step_perf_security)→(_step_web)→_step_diff→insert_snapshot→_step_defects→_step_report` | 【已复核】`project_manager.py:1764-1806`（run）/ `:1831-1843`（regression） |
| 终止条件（A 世界） | **确定性校验**，非模型自评：`all_pass = failed == 0 and passed > 0` | 【已复核】`run_regression.py:327`、`:388` |
| 终止条件（B 世界） | **LLM 自评**：Supervisor 输出枚举含 `FINISH`，prompt 明确"only when the mission objective is fully achieved" | 【已复核】`graph_base.py:485`、`:533` |
| 防跑飞 | 子进程硬超时 1800s；LLM 超时 60s。**B 世界的 `max_steps`（默认 30）到顶后只注入"换个区域"指令并把计数归 1 —— 是软重置，不是硬停** | 【已复核】`graph_base.py:493-511`；【调研】`app.py:137`、`generate_cases.py:129-137` |
| 轮次/Token 预算 | **无**（两世界都没有） | 【已复核】无 token 计数代码 |

**判断**：A 世界的 E 层不是"循环"而是"固定管线"——这在**确定性阶段是正确取舍**（可复现、可审计、CI 友好），但它意味着**新增阶段必须改主流程**，且阶段间的隐式顺序约束（如 diff 必须先于 insert_snapshot）只靠代码顺序与注释承载，没有显式声明。

---

### 2.2 T 工具注册

- **无统一注册表、无接口协议**：能力靠 `sys.path.insert` + 直接 import 接入（【调研】`project_manager.py:394,431,480,509,550,1057`）。
- **跨模块复用私有函数**：`run_web.py` 与 `run_perf_security.py` 各自内联复刻了回归模块的 `_load_yaml` / `_resolve_auth`（【已复核】`run_regression.py:50,84` / `run_web.py:102,107` / `run_perf_security.py:74,78`，后两处带 `# type: ignore`）。
- **唯一"发现"机制未接线**：`extensions/skills/registry.py` 的 `discover_skills` / `build_catalog` 只产出清单；控制台只做展示与开关（【调研】`app.py:844-904`）；基座 `custom_tools.fetch_agent_skill` 在不可达路径上。**当前 6 个技能是"人读的规范"，不是"引擎可加载的能力"**。

**判断**：在"确定性流水线"阶段，技能=文档是**合理的**，不是缺陷。真正的机会在 §4.8。

---

### 2.3 C 上下文管理

- **做得比多数项目好的地方**：相关性门槛不是拍脑袋——`knowledge.py` 有段落长度上限、Top-K、**相对分门槛（低于最高分 30% 丢弃）**，且截断处**显式标注**（"宁可少给，不能给错"）。（【调研】`knowledge.py:44,45,165,179-180`）
- **缺的是预算**：无 token 计数、无水位触发、无摘要压缩（B 世界有确定性的 Summarizer，但**不调用 LLM**，只保留最近 20 条）。【已复核 `_MIN_RATIO`/注入点；调研 `graph_base.py:570-621`】
- **组装点分两处**：CLI 与 Web 各自组装注入上下文（【调研】`project_manager.py:1632-1675` / `app.py:437-462`）——两者当前同源，但属于"同源靠约定"而非"同源靠同一个函数"。

---

### 2.4 S 状态存储

| 载体 | 内容 |
|---|---|
| SQLite `runs.db` | `runs`（任务历史）+ `snapshots`（回归快照），线程锁保护（【调研】`run_store.py:22,26,72,141`） |
| 文件态产物 | `projects/<id>/artifacts/` 下 `regression.json` / `perf_security.json` / `web.json` / `quality.json` / `diff.json` / `defects.json` / `run_meta.json` |
| Markdown 记忆 | `cases.md` / `lessons.md` / `knowledge.md` / `focus_supplement.md`；JSONL 趋势 `quality_history.jsonl` / `focus_history.jsonl` |

- **唯一增量治理点**：`_merge_run_meta()`（【调研】`project_manager.py:646`）——这是项目里做得很对的一处（整体覆盖会抹掉另一侧结论）。
- **缺**：无断点续跑、无事件溯源、无统一写入治理层（每个模块自己写自己的文件）。

---

### 2.5 L 生命周期钩子

- **只有一道门**：Web 层 `@app.before_request` 鉴权（【调研】`auth.py:137-146`，白名单 `:48-49`），且**默认关闭**。
- **CLI / 流水线无任何 pre-post 钩子**：各步骤用 `try/except` 各自兜底。
- **危险动作靠"自律"而非"机制"**：不自动提单、不自动建 Issue 都是**写在 docstring 里的克制**（很好，但是软的）。

**判断**：这是**六层里最薄的一层**。对一个"能触发任意测试任务、能写 `.env`"的控制台，缺确定性拦截点。

---

### 2.6 V 评估接口

- **做对的地方（值得保留）**：判定口径收敛到 `gate_notify.collect_gates` 作为三态唯一源；结构质量分有"**算不出就不猜**"的归一化约定；环境不可达不判绿、基线校验防假漏洞——这套"防假绿"纪律是**本项目最硬的资产**。
- **两个结构性缺口**：
  1. **跑判同源**：`all_pass` 的判定就写在各执行器内部（【已复核】`run_regression.py:327` 与 `:388` 同一文件内两处；【调研】`run_web.py:642`、`run_perf_security.py:591-596`）。**谓词被复制了 4 份**——"口径唯一"目前在**汇总层成立，在执行层已经分叉**。
  2. **模型自评不进 V**：LLM 的质量判定只存在于离线 `tests/eval/llm_judge.py`（抽样），不参与任何判定。这在当前阶段是**正确的**，但它意味着：一旦接入 B 世界，"这次探索测试到底有没有达成目标"没有 harness 侧的验证者。

---

### 2.7 范式 P

| 子维度 | 现状 | 证据 | 判断 |
|---|---|---|---|
| 扩展方式 | **嵌入式** | `extensions/*` 被主入口直接 import | 可预测、零魔法；代价是新增能力要改主流程 |
| 配置方式 | **混合**：场景/阈值声明式（`regression.yaml` / `web.yaml`），流水线步骤命令式 | 【调研】`project_manager.py:1618` vs 各 YAML | 场景层很好；流程层没跟上 |
| 部署拓扑 | **单机** | `127.0.0.1:8765` + 本地 SQLite + 本机子进程 | 与当前场景匹配，无需动 |
| 编排模式 | **中心化** | `project_manager` 单一编排者，串行 | 与"确定性优先"一致 |

**P 层判断**：这是一套**故意选择的保守范式**，与"先把确定性做扎实"的策略自洽。风险不在于"不够插件化"，而在于 §4.7 的隐式顺序依赖。

---

## 3. 高优先级发现（建议优先处理）

### 3.1 🔴 快照被写两次 —— 失败计数与趋势信号被翻倍 **【已复核，是真缺陷】**

> **修复状态（2026-09-14，已完成）**：按 §8 **D1=③（保留两处 + 幂等去重）** 落地。
> 控制台侧改为"兜底"语义：`run`/`regression` 子进程已写快照则跳过（判据 = 本次任务期间
> 该项目的快照数**未增加**），`rerun` 子进程不写、控制台照写（不能一起跳过）。
> 落点：`web_console/app.py` 的 `_should_record_snapshot()` + `web_console/run_store.py`
> 的 `count_snapshots()`；守护测试 `tests/test_console_snapshot_dedupe.py`（6 例，已做变异
> 验证：把去重回退为"无条件写"测试即变红，`delta` 由 1 变 2）。
> ⚠️ **历史遗留**：修复**之前**已产生的重复快照仍留在 `runs.db` 里，趋势图 / `lessons` 的
> 旧数据仍是放大的历史值；需要时可另行做一次一次性去重清洗（**未在本次修复范围内**）。

**证据链**：

1. 控制台起任务时执行的是**普通 `project_manager.py` 子进程**，没有任何"跳过留档"的开关 —— `web_console/app.py:101-135`（`_spawn_task` → `Popen([pm.python_exe(), pm_script(), *full_args])`）。
2. 子进程内部**自己会写一条快照** —— `project_manager.py:1796`（`trigger="run"`）、`:1837`（`trigger="regression"`）。
3. 子进程退出后，**控制台又写一条** —— `app.py:165-170`（`if kind in ("run","regression","rerun") and pid and pid != "*"` → `run_store.insert_snapshot(pid, reg, trigger=kind)`）。

**结论**：**从控制台触发的 run / regression / rerun，每次产生两条内容相同的快照。**

**影响（按严重度排序）**：

- `lessons.rebuild_from_snapshots(run_store.list_snapshots(pid, limit=500))`（`project_manager.py:1797`）读到双份 → **重点清单里的"历史失败次数"约虚高 2×** → 注入到生成提示的权重、`focus` 补齐用例标题里的失败次数全部失真。
- 趋势图 / 通过率序列每次跑出现两个点；`trend_diff` 的 `persistent` 连续失败计数按快照条数走，同样被放大。
- 与你们自己写进 README 的**判据 #4「口径唯一」直接冲突**——同一件事在同一层被记录了两次，且两处 trigger 标签不同（`"run"` vs `kind`）。

**为什么之前没被发现**：CLI 触发只写一条，控制台触发才翻倍；而"失败次数变多"不会让任何测试或门禁变红——**这正是"假绿"的一种形态：信号被污染但没有任何断言会报警**。

**三种修法与权衡**（我不替你定，理由与代价都列出来）：

| 方案 | 做法 | 代价 / 风险 |
|---|---|---|
| **① 删控制台那处（我倾向）** | 留子进程内那一处作唯一真相源；CLI 与 Web 天然同源 | 子进程被 1800s 超时 kill 时不留档（现状控制台这处正是兜底） |
| ② 删子进程那处 | 只由控制台留档 | **CLI 触发的 run 就完全不快照了** → 直接破坏趋势与 lessons，否决 |
| ③ 保留两处 + 幂等去重 | 控制台写入前比对"是否已有属于本次任务的快照"（用 `task["started_ts"]` 与最新快照时间戳比对），命中则跳过 | 多一点代码；但**同时保住"同源"与"兜底"**，且是唯一不牺牲能力的方案 |

> **无论选哪个，都建议补一条守护测试**：同一个任务结束后，`snapshots` 表里 `pid` 的条数增量必须 ≤ 1。这条测试现在写下去会是红的——这就是它存在的意义。

---

### 3.2 🔴 B 世界循环没有 harness 侧硬停，且"达成"由模型自己说了算 **【已复核】**

**证据**（`src/agentic_explorer/orchestration/graph_base.py`）：

- 路由用 `with_structured_output`，enum 含 `FINISH`（`:485`）；prompt 让模型自行判断 `"Respond with 'FINISH' only when the mission objective is fully achieved"`（`:533`）。
- `max_steps`（默认 30）不是上限而是**重置触发器**：`current_step > max_steps` → 注入"回到首页、换一个完全不同的区域"指令，并把 `current_step` 归 1（`:493-511`）。**理论上可以无限循环。**
- 无 token 预算；Summarizer 是纯确定性压缩、不调 LLM（【调研】`:570-621`）。

**为什么这条对你们格外重要**：你们对**被测系统**的信任度极低，并且把这个不信任做成了方法论——HTTP 4xx/5xx 一律判 FAIL、环境不可达绝不判绿、性能安全先用正确凭据登录一次以免报出"假漏洞"。而 B 世界的循环，准备**照单全收模型的自我报告**。

> **一致性追问（我要正式抛给你们）**：
> **"不信任被测系统"这条纪律，为什么只对被测系统生效，不对 agent 自己的循环生效？**
> 同一个团队，两套信任标准——这是本次审阅里我认为最值得回答的问题。

**建议**（接入前补齐，成本不高）：

1. **三道保险**：硬 `max_turns`（到顶即终止并产出"未达成"结论）+ 单步超时 + token/成本预算。
2. **结束原因结构化**：不要只有 `FINISH` 自然语言，改为枚举化原因（`completed` / `blocked` / `max-turns` / `budget-exhausted` / `error`）——**可审计的前提是原因可枚举**。
3. **最终判定权归 V 层**：把 mission 的目标编译成**确定性断言**（如"目标页面被实际访问 + `action_tape` 有对应记录 + 无不可达错误"），由程序判"达成"，模型只提供候选结论。

**案例对照**：DeepSeek Harness 的 E 层把 turn 结束定义为 5 种结构化原因（completed / blocked / max-tokens / aborted / error），引擎不设步数上限但用 `max_rounds` 兜底；Claude Code 的 E 层有 per-session 硬上限。**两者都不把"停不停"交给模型的一句自评。**

> **落地状态（2026-09-14）**
> - 新增 `src/agentic_explorer/orchestration/guardrails.py` —— **只依赖标准库**，不 import
>   langgraph/langchain。这样它才能在**不装基座重型依赖**的 CI 硬门禁里被直接测试
>   （`requirements-dev.txt` 不含 langgraph）。三件事都在这里：
>   `EndReason` 枚举（`completed` / `blocked` / `max-turns` / `budget-exhausted` / `error`）、
>   `Limits` + `check_limits`（**硬**上限）、`judge_mission`（**程序**判定达成）。
> - **软硬分离（§9 魔鬼代言人 #4 的落点）**：`max_steps` 的**软**重置**原样保留**（探索策略），
>   但 `AgentState` 新增只增不减的 `turns`；`check_limits` 用 **`>=`**（上限），
>   而软重置用的是 **`>`**（触发阈值）—— 这个符号差别就是"软 vs 硬"。到顶即路由 `FINISH`
>   并写 `end_reason`，**不再调用路由 LLM**（省掉一次无意义的调用，也别指望它自己停）。
> - **三道保险**：① 硬 `max_turns`（默认 `max(4·max_steps, 40)`，`AGENT_MAX_TURNS` 可覆盖）
>   ② token 预算（`AGENT_TOKEN_BUDGET`，默认 0=显式不限；由 agent 节点累计 `usage_metadata`）
>   ③ 单步超时（`AGENT_STEP_TIMEOUT`，**默认关**——见下方诚实标注）。
> - **判定权归程序**：`judge_mission` 把 mission 目标编译成确定性断言（`require_actions` /
>   `forbid_unreachable` / `must_visit`），返回 `achieved` / `unachieved` / **`unknown`**；
>   `report_<tid>/result.json` 落档，并在报告末尾追加「程序判定（权威）」段。
>   报告提示词里的 `Final Status` 已改成 **`Model's Assessment`（非权威）** —— 模型只提供候选结论。
> - **诚实标注（不假装全覆盖）**：
>   * token 预算只统计**能从 `usage_metadata` 读到**的对话调用；supervisor 的路由调用
>     不回传 usage，**不计入**。这是如实记账，不是精确计量。
>   * `step_timeout` **默认不启用**：现有 Playwright 已有动作级超时（5s/15s），turn 级超时会
>     打断合法的长回合，故做成显式开关。
>   * 判不了就记 `unknown`（**算不出就不猜**，沿用 A 世界判据 #6）—— 既不冒充通过也不冒充失败。
>   * B 世界的 `print` / 静默 `except` 仍未纳入日志化（属序 3 的遗留，不在本轮范围）。
> - **守护**：`tests/test_guardrails.py`（34 例，**已进 CI 硬门禁白名单**）+ 
>   `tests/test_agentic_guardrails_wiring.py`（8 例，用 `importorskip` 保护，在装基座依赖的
>   base-tests 作业里跑）。**变异验证 5 项**：`>=` 改回 `>` → 2 例红；拆硬停分支 → 2 例红；
>   软重置顺手归零 `turns`（=无限循环复现）→ 1 例红；`count_new_tokens` 把历史也算进去 → 2 例红；
>   `classify_end_reason` 恒定 completed → 3 例红。CI 硬门禁精确集合 **577 passed**（543 + 34）。

---

### 3.3 🔴 智能层与运行态的接线是 0 **【已复核】**

`project_manager.py`、`web_console/*.py` 对 langgraph / langchain / langmem **零引用**；构建脚本显式排除这些模块（【调研】`build/build_exe.py:43-56` 排除 11 项重依赖）。

**判断**：这不是缺陷，是**路线选择**——"确定性优先、智能层可降级"完全正确。但必须把它的含义说清楚：

> 现状的"智能"= **LLM 只用于文本生成（需求→用例）**，不参与探索、编排、判定。
> "AI 探索测试业务功能"这条产品主线，**目前没有任何运行路径**。

**因此对 S1 的具体建议**：

- **不要**把 B 世界 import 进 `project_manager.py`。理由有三：① 会把 455 行锁定依赖（【调研】`requirements.txt`）拖进当前刻意精简的 CI 硬门禁安装路径（`requirements-dev.txt` 只有 4 行）；② 一个进程内混"确定性门禁"与"LLM 不确定性"会让超时/异常语义互相污染；③ 与你自己的**双入口教训**（S4 刚消除的漂移）方向相反。
- **改为**：独立入口 / 独立进程（`extensions/agentic/` + 独立 `requirements-agent.txt`），由 `project_manager` 以**子进程 + 明确契约**方式调用，输入 mission、输出结构化结果（bugs / action_tape 路径 / 结束原因），再进入**现有的 V 层与报告链**。这样"探索测试"就变成流水线里的又一个可降级阶段，而不是绕过纪律的旁路。

> **落地状态（2026-09-14，已完成，§7 序 6）**
>
> **交付物**：新增 `extensions/agentic/` —— 零依赖契约 `agentic_contract.py`（`Status` / `DegradeReason`
> 枚举、`build_ok` / `build_degraded`、`decide_degrade` 纯判定、原子读写、Markdown 渲染）+
> 独立入口 `run_agentic.py`（探 key → 降级判定 → 子进程跑基座 → 归一化落 `artifacts/agentic.json`）+
> `requirements-agent.txt`（复用 `-r ../../requirements.txt`，不手抄版本）+ `README.md`（契约字段表）。
>
> **接入**：`project_manager._step_agentic()` 以子进程调用入口（`STA_AGENT_PYTHON` 可指向独立环境，
> `STA_EXPLORE_TIMEOUT` 控墙钟上限，默认 **1800s 有限**）；`cmd_explore` 子命令 + `run --explore`；
> 结果进 `run_meta`、报告新增「AI 探索测试（可降级阶段）」卡片与 summary 项。
>
> **降级（本序验收判据）**：`无 key / 超时 / 异常 / 缺任务` 四条路**全部**降级 + **原因出声**
> （枚举化原因 + stderr 日志 + 报告如实标注）。**三层兜底**：入口内部 → `project_manager` 进程级
> （超时/非零退出/契约缺失 → 合成 degraded）→ 报告照实展示。**跑前清旧契约**，避免入口崩溃时
> 把上一轮成功伪装成本轮。退出码：`ok`=0、显式 `explore` 的降级=**3**（未执行 ≠ 失败）。
>
> **同源判定**：'有没有 key' 直接调 `agentic_explorer.utils.llm.get_active_provider()`
> —— 不另立一套口径，且该模块只 import 标准库，故无 key 路径能在**硬门禁**里被真实验证。
>
> **守护**：`tests/test_agentic_contract.py`（52 例）/ `test_agentic_entry.py`（19 例）/
> `test_agentic_wiring.py`（16 例），全部进 CI 硬门禁白名单；**变异验证 9 项全部在预期粒度变红**
> （其中 M4 暴露"清旧契约"原本没被守住 → 已改为能真正暴露它的用例）。
> 另：序 3 的可观测性守护当场抓到本模块两处新违规（`print(file=sys.stderr)`、静默 except），已改。
> 顺带扩展序 5 的 `build_result(..., bug_items=)` 让 `result.json` 真的携带缺陷原文（加字段、不改字段）。
>
> **未覆盖（诚实标注）**：① **ok 路径无法端到端验证** —— 本机缺少可用被测应用 + 基座依赖冲突
> （见 §10 第 8 条）使 `main.py → custom_tools` 导入即失败；已用"假基座产物"钉住字段映射，
> 并在真实进程边界验证了异常降级。② `degraded_to` 只表达"回落确定性链路"，**没有**造一个
> "规则版探索器"（基座里本就不存在）。③ 探索发现尚未折叠进缺陷草稿链（只展示、不自动提单）。

---

## 4. 建议（中等优先级，按性价比排序）

### 4.1 「口径唯一」在执行层已经分叉 **【已复核】**

| 重复项 | 位置 | 份数 |
|---|---|---|
| `_load_yaml` | `run_regression.py:50` / `run_web.py:102` / `run_perf_security.py:74` | 3 |
| `_resolve_auth` | `run_regression.py:84` / `run_web.py:107` / `run_perf_security.py:78` | 3 |
| 门禁谓词 `failed==0 and passed>0` | `run_regression.py:327` + `:388`，另一侧在 web / perf 模块 | 4（2 处已复核） |
| `cases.md` 行解析 | `project_manager.py:670` / `memory/focus.py:184`（另有 `case_quality` 内解析，未逐行核实） | ≥2 已复核 |

**影响**：任一处修了分隔符/转义/鉴权规则，另外几处**静默不一致**——表现就是你们最熟悉的那种"改了没效果"，而且不会有测试变红。

**建议**：抽 `extensions/common/`（`io_utils.load_yaml` / `load_project` / `resolve_auth`，`cases.parse_rows`），三处改为委托。**改动小、风险低、可以立刻加防漂移测试**：同一份输入喂给新旧两个实现，输出必须逐字段相等——这条测试能在重构前先跑（此时必然全绿），重构后继续守着。

> 这是本次审阅里**性价比最高的一条**：它把你们已经写进 README 的原则，从"汇总层成立"扩展到"全代码成立"。

> **落地状态（2026-09-14，已完成）**：新增 `extensions/common/`（`yamlio.load_yaml` /
> `auth.resolve_auth`+`load_dotenv` / `data.substitute`+`dig` / `gates.all_pass` / `cases.parse_rows`），
> 5 个调用方（`run_regression` / `run_web` / `run_perf_security` / `project_manager` / `focus` / `case_quality`）
> 全部改为**委托**（`from common.x import y as _y`，拿到的是同一个函数对象）。
> 收敛范围比原报告更宽：`load_dotenv`（原本还有 1 份兜底副本）与 `_substitute`/`_dig` 也一并收敛。
> 守护测试 `tests/test_common_parity.py`（23 例，已接入 CI 硬门禁白名单），三层守卫：**身份**（`is` 同一对象）、
> **唯一性**（AST 扫描 `extensions/` 的 `def` 定义数各为 1）、**行为**（同输入逐字段相等）。
> 变异验证：故意在 `run_perf_security` 再各写一份 `_resolve_auth` → 8 条断言同时变红；把 `all_pass`
> 退回"只看 failed" → 真值表 `(0,0)` 变红。
>
> ⚠️ **本次新发现（报告原文未写）**：cases.md 行解析的三份实现**语义并不一致** ——
> `focus`（遇非表格行 `break` + 补齐短行）、`project_manager`（`continue` + 丢弃短行 + 过滤空表头）、
> `case_quality`（`continue` + `min(2,n)` 阈值）。对真实生成的 cases.md（单张满列连续表）三者结果相同，
> 差异只在畸形输入下显现。本次统一到 `common.cases.parse_rows`（`break` + 补齐 + 过滤空表头），
> **对 `project_manager` / `case_quality` 属行为微调**（畸形输入下不再丢行、改为补空串）——已用测试钉住。

### 4.2 可观测性为零 **【已复核：全仓无 `import logging`】**

全项目只有 `print`；`log` 只以文本形式落 `runs.log`。静默吞异常 ≥50 处（【调研】`app.py:171`、`registry.py:109`、`run_store.py:51`、`provenance.py:273`、`run_web.py:572` 等）。

**影响**：CI 绿之前红了 **15 次**都定位不到原因——这次是靠 GitHub 注解这个"应急出口"绕开的。同样的成本会在下一次以别的形式再收一遍。

**建议**（不追求一次到位）：① 引入 `logging` + `run_id`（控制台已有 `tid`，CLI 侧生成短 id）贯穿 CLI/Web/扩展；② 把静默 `except` 分成两类——"可忽略"记 debug，"必须出声"记 warning/error。**你们已经有一套成熟的"必须出声"哲学**（配置问题单列、降级要出声、环境不可达要交代名单），只需把它落到日志层，**这是你们已有的判断力在基础设施上的兑现**。

> **落地状态（2026-09-14，已完成）**：新增 `extensions/common/obs.py`（唯一实现层）——
> `setup()`（幂等，stderr + 可选文件 handler，级别由 `STA_LOG_LEVEL` 控制）、`get_logger()`、
> `new_run_id()`、`set_run_id()`/`get_run_id()`（`ContextVar`）、`RunIdFilter`（把 run_id 注入每条记录）。
> 三入口接入：`project_manager.main()`（`adopt_env_run_id("cli")` + 未捕获异常出声）、
> `web_console/app.py`（任务 tid 即 run_id）、`run_console.py`。**run_id 跨进程贯通**：控制台把
> `STA_RUN_ID=<tid>` 注入子进程 env；工作线程内显式 `set_run_id(tid)`（ContextVar 不跨线程继承）。
>
> **一条刻意确立的边界**：`print` 并未被全面禁止 —— **"给人看的结果呈现"**（`list` 的表格、
> `defects` 的 Markdown、`--json` 载荷）留在 stdout 是**产品行为**（要能被 `|` 管道接走）；
> **日志负责"排障用的诊断"**（进度、判定、降级、异常），走 stderr + 可选文件。二者是**两种受众**，
> 不是二选一。这条边界写进了 `tests/test_obs.py` 的 docstring，是决定而不是遗漏。
>
> **量化结果**（AST 盘点，A 世界 = `project_manager` / `extensions/**` / `web_console/**`）：
> - `print(..., file=sys.stderr)`：**15 → 0**；
> - `except` 块内 `print(`：**24 → 0**；
> - 静默 `except`（体只有 `pass`/`continue`/`return`）：**26 → 0**（全部带分类注释，其中 11 处改为真正 `log.warning`/`log.error`）；
> - 其中 **"必须出声"的典型**：Web 失败截图/复现脚本写不出、清不掉上次证据、知识库或 lessons 注入失败、
>   `regression.json` 读坏、LLM 降级、钉钉通知失败、跳过 YAML 语法校验（**静默放松门禁**）。
> - 守护测试 `tests/test_obs.py`（13 例，已进 CI 硬门禁白名单）：R1 无 stderr print / R2 无 except 内 print /
>   R3 每个静默 except 有 log 或分类注释 / run_id 注入记录 / setup 幂等 / 跨进程贯通（静态断言）。
>   变异验证 5 项：R1、R2、R3 各打一枪**各自精确变红并报出位置**；拆掉控制台 `set_run_id` → 贯通断言红；
>   让 `RunIdFilter` 空转 → run_id 注入断言红（连带格式化用例也红，说明该环节是**承重**的）。
> - ⚠️ **守护测试当场抓到的一个真实设计弱点**：`new_run_id` 原用 4 位 hex，同一秒生成 200 个 **约 1/4 概率相撞** ——
>   唯一性不该交给概率。已改为"时间+随机负责跨进程、进程内自增序号负责同进程绝不重复"。
> - **未覆盖（如实标注）**：`run_regression` / `run_web` / `run_perf_security` 的**进度类** `print`
>   （合计 46 处）未迁移——它们是 CLI 进度反馈，排障价值低于本轮目标；以及 B 世界
>   （`src/agentic_explorer`）的 print 与静默 except 未纳入（属 §7 序 5）。

### 4.3 L 层补最小的确定性拦截 **【已落地：`web_console/guard.py`，2026-09-14】**

现状取舍（本机单人 → 默认为关）**是合理的**，我不建议改成强制鉴权。但可以低成本加三件：

1. **破坏性操作二次确认 + 审计行**：删除项目等写操作落一条 `audit.jsonl`（谁、何时、对哪个项目、结果）。
2. **只读模式开关**：`STA_CONSOLE_READONLY=1` → 允许看，禁止触发任务/删除。局域网演示时的安全档。
3. **密钥写入路径提示**：`.env` 由 Web 明文写入（【调研】`app.py:1031-1050`）——至少写入前明确提示"此文件含明文凭据，勿提交"，并把该路径纳入启动自检。

**案例对照**：capability-security 模式——**按能力授予，而非按身份**；Claude Code 的 `allow/ask/deny` 分级是最小权限的产品化范本。

> **落地状态（2026-09-14）**
> - 新增 `web_console/guard.py`（只读模式 + 审计，**不引入身份体系**），在 `app.py` 里
>   **于 `auth.install` 之后**安装 —— before_request 按注册顺序执行，鉴权必须先于只读判定
>   （否则未登录的人先撞 403 而非 401，前端拿不到"该去登录"的信号）。
> - **① 审计**：所有 `POST/PUT/PATCH/DELETE` 打到 `/api/*` 的请求追加一行 `audit.jsonl`
>   （`ts` / `event` / `method` / `path` / `pid` / `status` / `actor` / `remote` / `run_id`）。
>   **被只读拦下的尝试也留痕**（记 `blocked_readonly`，且用 `g.sta_audited` 去重，避免 before/after 各记一条）。
>   **写失败只出声不抛异常**（静默的审计 = 以为有记录其实没有，比没有更危险）。
>   破坏性操作的"二次确认"沿用现成的两层（前端 `confirmDialog` + 后端 `?confirm=1`），本次未改。
> - **② 只读模式**：`STA_CONSOLE_READONLY=1` → 一切 `/api/*` 写操作 **403**，读操作照常；
>   `/login`（登录流程）不受影响。前端 `auth/status` 读到 `readonly` 后显示横幅，避免"点了才吃 403"。
> - **③ 密钥写入提示**：`.env` 写入时返回 `secret_note`（"含明文凭据、勿提交"）+ `log.warning`；
>   新增 `_env_selfcheck()`，在 `__main__` 启动时报告 `.env` 路径与**是否仍被 `.gitignore` 覆盖**
>   （`git check-ignore` 最准，非仓库时退回静态读 `.gitignore`）。`audit.jsonl` 已加入 `.gitignore`。
> - **守护测试 `tests/test_console_guard.py`（45 例，已进 CI 硬门禁白名单）**：默认不改变行为 /
>   只读写全拦读放行 / 登录不被拦 / 审计字段齐全 / 被拦也留痕且不重复 / 审计失败不弄挂业务 /
>   开了鉴权时 actor 落到会话（且**审计里绝不含 token 本体**）/ `.env` 提示。
>   **变异验证 4 项**：`readonly()` 恒 False → 18 例红；`record()` 空转 → 3 例红；
>   拆掉 after_request 去重 → 1 例红；去掉 `secret_note` → 1 例红。CI 硬门禁精确集合 **543 passed**（498 + 45）。
> - **诚实标注**：未开鉴权时 `actor` 只能是 `anonymous`（记录远端 IP），**不假装有身份**；
>   审计是本地 append-only 文本，**不防篡改**；只读拦的是**控制台入口**，**拦不住直接跑 CLI 的人**。

### 4.4 E 层把隐式顺序变成显式依赖

现在"阶段顺序"承载着至少一条**不写在类型里的强约束**：`diff` 必须在 `insert_snapshot` 之前（否则自己跟自己比，结论永远是"没有新增失败"）。这类约束今天靠代码顺序 + 注释 + 记忆维持。

**建议**：阶段注册表（`name` / `requires` / `run`），主循环按依赖拓扑执行。**注意这是重构不是重写**——先把依赖声明出来并与现状顺序做一次等价性断言，再动执行顺序。

**案例对照**：graph-state-machine 模式——用图显式声明状态与流转，正是为消除这类隐式顺序而生。

> **✅ 落地状态（2026-09-14，§7 序 7）**
>
> - **机制**：新增 `extensions/common/pipeline.py`（**零依赖**，可在 CI 硬门禁直接测）：
>   `Stage(name / requires / run / doc)` + `validate`（重名 / 未知依赖 / 自环）+
>   `resolve_order`（**稳定拓扑排序**：真实依赖决定先后，彼此无依赖者按**声明顺序**作 tie-break，
>   结果确定）+ `run_stages`（阶段自身异常**原样上抛** —— 机制不替业务决定"失败要不要继续"）。
>   解析失败一律抛 `StageError`，**不静默继续**：此时任何顺序都是错的。
> - **注册表**：`project_manager._run_stages()`（13 阶段：requirements→focus→meta→quality→api→
>   regression→perf→web→agentic→diff→snapshot→defects→report）与 `_regression_stages()`（4 阶段），
>   是**顺序的唯一声明处**。`cmd_run` / `cmd_regression` 改为经 `run_stages` 执行；各阶段实现抽成
>   `_stage_*`、阶段间状态经 `_RunCtx` **显式传递**（不再靠局部变量作用域隐式传值）。
> - **隐式约束转正**：`snapshot` 依赖 `diff` 这条边写进 `requires`（原先两处命令各写一遍注释）。
>   **重构不重写**：先把依赖声明出来，再与现状顺序做等价性断言。
> - **等价性断言**：`tests/test_pipeline_registry.py`（**21 例，已进 CI 硬门禁白名单**）四层 ——
>   ① 机制正确（无依赖保持声明序 / 菱形 / 成环报错且**点名阶段**）；
>   ② **等价性**：注册表解析顺序 == **重构前** `cmd_run` 真实顺序（oracle 冻结自序 6 的 HEAD `b2c08a9`）；
>   ③ **承重性**：把 `snapshot` 挪到 `diff` **之前声明**，`requires` 这条边仍强制出正确顺序 ——
>      删掉该边则顺序立刻退化成"snapshot 在 diff 之前"（正是那条静默错误）；
>   ④ **接线**：真跑一遍 `cmd_run` / `cmd_regression` 的控制流，阶段被调用顺序 == 注册表顺序，
>      且 **AST 断言"不得再内联 `_step_*`"** —— 否则顺序又退回"靠书写位置"。
> - **变异验证 5 项（全红在预期粒度）**：① 删 `snapshot→diff` 边 → 2 例红；② 翻转 tie-break
>   （挑"最后一个就绪者"）→ **6 例红**（含两条等价性 + 两条接线）；③ 造环 → `cmd_run`
>   **启动即报错**，绝不静默乱序；④ 删"未知依赖"校验 → 1 例红（且报错退化成**误导性的"成环"**）；
>   ⑤ 往 `cmd_run` 内联一个 `_step_*` → 接线断言 1 例红。
> - **门禁**：CI 硬门禁精确集合 **687 passed**（666 + 21）；基座作业 **772 passed / 5 skipped**。
> - **§4.6 拆大文件：本轮不做**（见下）。

### 4.5 依赖分层（配合 §3.3）

把基座重依赖拆到独立 `requirements-agent.txt`，与产品依赖物理隔离；`requirements.txt`（455 行锁定）只服务基座 CLI。当前靠"不 import"来隔离，比较脆——一次误 import 就会让 CI 硬门禁的安装路径变重。

---

## 5. 可选（低优先级，先不动）

- **4.6 拆大文件**：`project_manager.py`（2000+ 行）、`index.html`（1500+ 行）。**建议在大文件真正阻碍改动时再拆**——现在拆是纯收益为"可读性"的成本，而 §4.4 的注册表化会自然带来拆分。
  → **（2026-09-14）本轮未拆**：按原判"在大文件真正阻碍改动时再拆"。§4.4 已把"阶段顺序"从 2000 行里**提取成可见声明**、`_stage_*` 的抽出也留出了天然切缝；真要物理拆分，建议另开一序单独做（含 `index.html` 的拆分评估）。
- **4.7 双入口收尾**：`software_testing_agent.py` 已是 `pm.run_pipeline` 薄封装（【调研】`:29,39`），S4 已完成。建议在 README/文档里显式标注为"兼容入口，勿新增逻辑"，避免它悄悄长出第二套逻辑。
- **4.8 技能从"文档"升级为"上下文来源"**：`agent-skills/*/SKILL.md` 现在是展示品。**建议等 S1 之后再做**，做法很轻——把 `SKILL.md` 当作 knowledge 的**第三类来源**，复用已有的 2-gram Top-K + 相对门槛机制按需注入。你们已经有这个机制，只差多一个候选源。

---

## 6. 知识库案例对照（供横向校准）

| 你们的现状 | 对照案例（H+P 标签） | 差距 / 启发 |
|---|---|---|
| S 层：SQLite + 文件 + Markdown 三类载体并存，无单一真相源 | **DeepSeek Harness**（S=append-only 事件溯源；不变量「model-visible means logged」） | 你们已有动作磁带与溯源雏形 → 可向"日志是单一真相源、可回放重建"推进一步 |
| L 层：仅 Web 一道鉴权门（默认关），无高危动作拦截 | **Claude Code**（L=hooks 确定性拦截 + allow/ask/deny） | 「哪些动作该确定性拦截，哪些该让模型判断」——高危写操作应当拦截而非自律 |
| E 层（B 世界）：LLM 自评 FINISH、超限软重置 | **DeepSeek Harness**（turn 结束 5 种结构化原因 + `max_rounds` 兜底） | 结束原因应枚举化；硬上限必须是上限 |
| T 层：无注册表、直接 import | **DeepSeek Harness**（`ctx.tools` / `defineTool` 按需注册） | 阶段注册化能同时换来"可筛选子集"的能力 |
| C 层：有相关性门槛（0.34 / 相对分 30%），无预算 | **Claude Code**（技能按需加载 + 历史满自动压缩 + 长会话显式 /compact） | 门槛做得比案例更克制，缺的是 token 水位与压缩策略 |
| V 层：判定内联执行器、谓词复制 4 份 | **DeepSeek Harness**（三维评估 + session log 为唯一权威源） | 你们已有独立汇总层，只差把谓词也收敛进去 |

> 知识库口径说明：以上对照取自本 skill 知识库 `index.md` 的索引定位与两个常驻案例（DeepSeek Harness / Claude Code，`confidence: verified`）的完整详文。**未做入库操作**（本次无新增知识，无需自进化）。

---

## 7. 改进路线（建议顺序 + 验收判据）

| 序 | 项 | 为什么这个顺序 | 验收判据（可量化） |
|---|---|---|---|
| 1 | ✅ 修快照重复（§3.1）**（2026-09-14 已完成）** | **信号污染会污染所有下游判断**——先让仪表盘可信，再谈改进 | 新增守护测试：单任务快照增量 ≤ 1；`lessons` 失败次数不再翻倍 → `tests/test_console_snapshot_dedupe.py`（6 例 + 变异验证），已接入 CI 硬门禁白名单 |
| 2 | ✅ 抽 `extensions/common/`（§4.1）**（2026-09-14 已完成）** | 低风险、立刻兑现"口径唯一"；为后续重构铺路 | `tests/test_common_parity.py`（23 例）：身份 `is` 同一对象 + AST `def` 定义数各为 1 + 同输入逐字段相等；已接入 CI 硬门禁白名单。变异验证 2 项通过 |
| 3 | ✅ 引入 `logging` + `run_id`（§4.2）**（2026-09-14 已完成）** | 没有它，后面每一步的排障成本都在重复支付 | 诊断路径无裸 `print`（stderr **15→0**、except 内 **24→0**）；静默 `except` **26→0** 且**每处都有分类注释**。守护测试 `tests/test_obs.py`（13 例，已进 CI 硬门禁白名单）+ 5 项变异验证。逐项证据见 §4.2 落地状态 |
| 4 | ✅ L 层最小拦截 + 审计（§4.3）**（2026-09-14 已完成）** | 团队化前的最低安全垫 | 写操作有审计行（谁/何时/哪个项目/结果，被拦也留痕）；只读模式可用（写 403、读放行、登录不受影响）；`tests/test_console_guard.py`（45 例，已进 CI 硬门禁白名单）+ 4 项变异验证。逐项证据见 §4.3 落地状态 |
| 5 | ✅ 基座循环补护栏（§3.2）**（2026-09-14 已完成）** | **S1 的前置条件**，不是 S1 的一部分 | 硬 `max_turns` 生效（到顶即 FINISH 且**不调 LLM**，软重置不再动 `turns`）；结束原因为 5 态枚举；目标达成由程序判定（含 `unknown`）。`tests/test_guardrails.py`（34 例，已进硬门禁）+ 接线测试（8 例，importorskip）+ 5 项变异验证。逐项证据见 §3.2 落地状态 |
| 6 | ✅ S1 独立入口接入（§3.3）**（2026-09-14 已完成）** | 前 5 步做完，接线才是"加能力"而不是"引进风险" | 无 key / 超时 / 异常任一情况下降级且**降级原因出声**。`extensions/agentic/`（零依赖契约 + 独立入口 + 独立依赖清单）+ `project_manager` 子进程接入 + 报告卡片 + `run_meta`；降级 4 路全覆盖、三层兜底、跑前清旧契约。守护 87 例（52+19+16，全进硬门禁）+ **9 项变异验证全红**。逐项证据见 §3.3 落地状态 |
| 7 | ✅ E 层注册表化（§4.4）**（2026-09-14 已完成）**；拆文件（§4.6）**本轮不做**（见 §4.6 注） | 纯内部质量，随时可做 | **阶段依赖显式声明**（`snapshot.requires` 含 `diff`）；**等价性断言通过**（注册表解析顺序 == 重构前 `cmd_run` 真实顺序）。`tests/test_pipeline_registry.py`（21 例，已进 CI 硬门禁白名单）：机制 / 等价性 / 承重性 / 接线四层 + **5 项变异验证**。逐项证据见 §4.4 落地状态 |

---

## 8. 关键决策点（交你拍板，附我的建议与依据）

| # | 决策 | 选项 | 我的建议 + 依据 |
|---|---|---|---|
| **D1** | 快照重复怎么修 | ① 删控制台那处 ② 删子进程那处 ③ 保留两处 + 幂等去重 | **③（用户已拍板，2026-09-14 落地）**。①会丢掉"子进程被 kill 时不留档"的兜底，②会直接让 CLI 触发的 run 没有快照（直接破坏趋势与 lessons）。③同时保住同源与兜底，代价是多一段去重逻辑 |
| **D2** | S1 接线方式 | ① import 进 `project_manager` ② 独立入口 + 子进程调用 | **②**。①会把 455 行重依赖拖进 CI 硬门禁安装路径、把不确定性与门禁语义混在一个进程、且与 S4 刚消除的双入口漂移反向 |
| **D3** | 先动哪一类 | ① 修信号污染（序 1）② 补基础设施（序 2–3）③ 直接上 S1 | **①→②→③**，即按 §7 顺序。**先让仪表盘可信**——否则 S1 做完你无法判断它是变好了还是变吵了 |

---

## 9. 魔鬼代言人（我替你的方案构造的最强反驳）

1. **"确定性流水线 + LLM 只写用例"就是终局，何必接智能层？**
   —— 完全站得住。若目标是"稳定复用的测试工程体系"，现状已足够，接智能层纯属增加不确定性。**这条反驳成立的前提是：产品定位不是"AI 探索测试"**。这需要你回答（见 §10）。
2. **"抽 common 层是过度设计，三份 `_resolve_auth` 各自独立反而更解耦。"**
   —— 局部成立，但你们自己把"口径唯一"写成了铁律。**要么承认执行层可以分叉并修改铁律，要么收敛代码**；两者都行，维持现状（铁律说唯一、代码有三份）最糟。
3. **"快照写两次无伤大雅，只是多点数据。"**
   —— 只有在"没人依赖失败次数"时成立。而 `lessons` 的聚类权重、`focus` 补齐标题里的失败次数、趋势图、`persistent` 连续计数**全都依赖它**。
4. **"B 世界的 `max_steps` 软重置是刻意设计（探索测试本来就不该硬停）。"**
   —— 作为**探索策略**这说得通（避免过早收敛）。但作为**资源安全阀**它不成立：没有 token 预算的"不设上限"等于把成本控制权交给模型的判断力。**正确做法是两者分离**：探索可以不设内容上限，但必须有硬资源上限。

---

## 10. 遗留问题与未知区（诚实标注）

**本次审阅未覆盖（不假装全覆盖）**：

1. **未做运行时验证**——我没有执行任何测试或跑通流水线，结论基于静态取证 + 两路调研交叉核对；标【调研】的条目未逐行复核。
2. **"效率提升"缺可达量化口径**——需求里"提升测试效率"目前没有任何测量方式。**建议你补一个定义**（例如"从需求到可执行回归的耗时"或"人工编写用例条数下降比例"），否则无法验收，也无法证明 S1 之后变好了。
3. **性能与成本维度完全空白**——无 token 计量、无并发压测、无语义缓存；本次给不出数字。
4. **多用户/并发**：`TASKS` 为进程内字典 + 线程锁，多 worker 部署会失效（单机当前无影响）。
5. **数据治理**：`runs.db` 与 `projects/` 的增长、备份、保留期、脱敏策略均未设计。
6. **业务用例质量**：本审阅**不评价**"生成的用例是否真测到了业务要害"——那是测试领域专家的判断，我只能评价机制（覆盖校验/质量分的结构与克制）。
7. **灰度与回滚**：现有"可选阶段 + 降级"已具备雏形，但无版本化产物、无灰度分流。
8. ✅ **基座依赖版本冲突**【2026-09-15 已修（B⑧）】
   - **现象**：`langchain-mcp-adapters 0.2.2` 的 `callbacks.py` 依赖
     `mcp.shared.context.RequestContext`，而环境里是 `mcp 2.2.0`（该符号已移除）→
     `agentic_explorer.tools.common.custom_tools` → `pr_analyzer` → `agentic_explorer.main`
     **导入即失败**；`tests/test_context_disclosure.py` 在**收集期**就报错。
   - **掩体**：该测试被 `ci.yml` 的 `--ignore` 挡掉 → 「CI 全绿」与「`main.py` 导不进来」
     **同时成立**。又一个"绿灯掩盖实质损坏"的形态：危险的不是报错，是它被盖住了。
   - **根因（比"版本装错"更深一层）**：适配器 0.2.x～0.3.1 对 `mcp` 声明的**只有下界**
     （`mcp>=1.9.2` / `>=1.24.0`，**无上界**），而 `pyproject.toml` 里 `mcp` 只是传递依赖，
     **没有人为它的主版本负责**。于是 `requirements.txt`（uv 编译产物）会随索引漂移：
     它今天重新编译出来就是 mcp 2.x。锁文件停在 `mcp==1.28.1` 只是"当时恰好如此"，
     本身并不构成约束——这正是它能在环境里悄悄变成 2.2.0 的原因。
   - **修法（把上界交还上游，不新增本地主张）**：`langchain-mcp-adapters`
     `~=0.2.2 → ~=0.3.2`。上游直到 **0.3.2** 才把声明改成 `mcp<2.0.0,>=1.24.0`
     （0.3.0 / 0.3.1 仍是 `mcp>=1.24.0` 无上界——已下载三个 wheel 读取 METADATA 逐条比对）。
     锁文件随之**只改一行**：`mcp==1.28.1` / `langchain-core==1.3.3` /
     `typing-extensions==4.15.0` 本来就已满足 0.3.2 的约束。
   - **验收（可复核）**：`pip check` 无冲突；`main` / `custom_tools` / `pr_analyzer` 均可导入；
     `tests/test_context_disclosure.py` **移除 ignore 后 9 项全过**；
     `MultiServerMCPClient(connections)` 位置参数 + `get_tools()` 无 API 漂移。
   - **守护**：`tests/test_dependency_bounds.py`（8 例，已进 CI 硬门禁白名单）——静态断言
     ①适配器下界 ≥ 0.3.2 ②锁文件 `mcp < 2.0.0` ③`ci.yml` 不得再用 `--ignore` 遮住该测试。
     **5 项变异验证全部红在预期粒度**（下界回退 / mcp 改回 2.2.0 / 重新盖 ignore /
     锁文件适配器回退 / 删掉直接声明）。
   - **仍未知（不假装全覆盖）**：本轮只在**本机 Windows** 验证；ubuntu 上的表现未实测
     （base-tests 是 `continue-on-error`，即使红也只是可见性告警、不拦交付）。
     另：`ci.yml` 里 `test_config.py` / `test_llm.py` 两条遗留 `--ignore` **与本冲突无关**
     （前者是测试自身的 `os.chdir(临时目录)` 后删除该目录的 Windows 平台缺陷），
     本轮**未动**，仅在其上方补写了原因注释，留待单独核实。

**需要你回答的一个问题（决定 §9.1 那条反驳成不成立）**：

> **这个智能体的终局是"稳定的测试工程体系"，还是"能自主探索的测试智能体"？**
> 前者 → §7 做到序 3 即可停；后者 → 序 5、6 是必经之路，且 §3.2 的三道护栏必须先补。

**用户已作答（2026-09-14）**：终局 = **能自主探索的测试智能体**。
→ §9.1 那条反驳**不成立**：接智能层是产品主线，不是过度设计。
→ §7 的**序 5（B 世界护栏）、序 6（S1 独立入口接入）从"可选"变为必经**；
  且 §3.2 的三道护栏（硬 `max_turns` + 单步超时 + token/成本预算、结束原因枚举化、
  目标达成由程序判定）**必须先补**，再接线。

---

## 附：证据复核记录

**本人直接读代码确认（已复核）**：
`project_manager.py:1796,1837`（快照写入）｜`web_console/app.py:101-175`（子进程 + 二次快照）｜`graph_base.py:485,489,491-511,533`（路由枚举 / 自评 FINISH / 软重置）｜`run_regression.py:50,84,318-336,339-399`（谓词两处 + 复刻的 helper）｜`run_web.py:102,107` / `run_perf_security.py:74,78`（helper 复刻）｜`focus.py:184` / `project_manager.py:670`（cases 解析两处）｜全仓 `logging` 检索为空｜`web_console/app.py:37-47`（DATA_ROOT 定位）。

**由自动调研给出、未逐行复核（调研）**：六层行号清单、静默 `except` 位置列表、`knowledge.py` 门槛参数、测试规模（32 文件 / 553 用例）、CI 白名单 17 个文件、`requirements.txt` 行数、`build_exe.py` 排除项。

**审阅中我修正过调研结论的一处**：调研称"根目录 `models_config.json` 没有读者"，经复核 **不成立**——非打包模式下 `DATA_ROOT == 仓库根`（`app.py:45-47`），`MODELS_FILE = DATA_ROOT/"models_config.json"` 确实指向根目录该文件。**此条已从结论中剔除。**
