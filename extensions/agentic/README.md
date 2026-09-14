# extensions/agentic —— AI 探索测试（S1 独立入口）

把基座（`src/agentic_explorer/`，Supervisor-Worker 群）的**自主探索能力**接进确定性流水线。
依据：`docs/HARNESS_ARCHITECTURE_REVIEW.md` §3.3 / §7 序 6，接入方式为**决策 D2 = 独立入口 + 子进程**。

```
     mission.yaml                run_agentic.py                     artifacts/agentic.json
  ┌──────────────┐   ┌──────────────────────────────┐   ┌──────────────────────────┐
  │ 探索任务声明  │──▶│ 探 key → 降级判定 → 跑基座    │──▶│ 结构化契约（唯一消费来源） │
  └──────────────┘   │ （子进程：agent-explorer）    │   └────────────┬─────────────┘
                     └──────────────────────────────┘                │
                                                                     ▼
                                        项目报告卡片 / run_meta 溯源 / 看板
```

## 1. 为什么是"独立入口"而不是 import 进 project_manager

| 理由 | 说明 |
|---|---|
| 依赖隔离 | 基座需要 langgraph / langchain / langmem / playwright（见 `requirements-agent.txt`）。import 进 `project_manager` 会把这些**拖进 CI 硬门禁的安装路径**，让"确定性结论"变慢、变脆 |
| 语义隔离 | 一个进程里混"确定性门禁"与"LLM 不确定性"，会让超时/异常互相污染（门禁红得莫名其妙） |
| 避免双入口漂移 | 与刚消除的双入口问题方向一致：能力放一处，调用方只走**一条**受契约约束的路径 |

## 2. 契约：`artifacts/agentic.json`

**这是唯一权威**。调用方（`project_manager`）只读这个文件，不从 exit code / 日志里猜结论。

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema` | str | 契约版本，当前 `agentic/1`。字段语义变更必须升版本 |
| `status` | `ok` \| `degraded` | **只有两种**。探索不是门禁，不存在"失败"，只有"降级" |
| `reason` | enum \| null | 降级原因（见 §3）。`ok` 时为 null |
| `message` | str | 降级的人类可读说明（**降级必须出声**的落点之一） |
| `degraded_to` | `deterministic` \| null | 降级后"谁顶着"：确定性链路（回归 / 性能安全 / Web） |
| `provider` | `claude` \| `gemini` \| `unknown` | LLM 提供方（与基座同源判定） |
| `thread_id` | str \| null | 本次 mission 的 thread_id |
| `end_reason` | enum \| null | 基座 5 态：`completed`/`blocked`/`max-turns`/`budget-exhausted`/`error` |
| `goal_verdict` | enum \| null | 基座程序判定：`achieved`/`unachieved`/`unknown` |
| `reasons` | list[str] | 判定依据 |
| `turns` / `tokens` / `actions` | int | 轮次 / token / 动作条数 |
| `bugs` | list[str] | 探索发现**原文**（截断 500 字，**不做结构化推断**） |
| `bug_count` | int | 发现数量 |
| `report_dir` | str \| null | 基座报告目录（相对仓库根） |
| `action_tape` | str \| null | 动作磁带 `action_tape.jsonl` 路径 |
| `log` | str \| null | 探索子进程完整 stdout/stderr |
| `elapsed_s` | float | 本阶段墙钟耗时 |

## 3. 降级原因枚举（哪个环节出问题一目了然）

| `reason` | 触发 | 谁的问题 |
|---|---|---|
| `not_configured` | 缺 `mission.yaml`（或指定文件不存在） | 使用者：补任务声明即可 |
| `no_llm_key` | 探测不到任何可用 LLM 凭据 | 环境：配 `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` 或 `LLM_PROVIDER` |
| `timeout` | 超过墙钟上限（默认 1800s，`STA_EXPLORE_TIMEOUT` 可改） | 环境/任务：任务太大或环境太慢 |
| `error` | 子进程起不来 / 未产出结构化结果 / 内部异常 | 视 `message` 与 `log` 定位 |

> **"无 key"的判定与基座同源**：直接调用 `agentic_explorer.utils.llm.get_active_provider()`，
> 不在本模块重写一套"看哪些环境变量算有 key"。否则同一件事会有两个答案。

## 4. 退出码语义

| 退出码 | 含义 |
|---|---|
| `0` | 已写出有效契约（`ok` 或 `degraded`） |
| `3` | 降级，且传了 `--fail-on-degraded`（供"显式要求探索"的调用方用） |
| 其它非 0 | 连契约都没写出来 → 调用方走**进程级兜底**，合成 `degraded` |

**契约优先于退出码**：退出码只表示"契约有没有写出来"，不承担判定语义（避免两套口径）。

## 5. 用法

```bash
# 直接跑（项目目录下需有 mission.yaml）
python extensions/agentic/run_agentic.py --project-dir projects/<id>

# 指定任务 / 上限 / 带界面调试
python extensions/agentic/run_agentic.py --project-dir projects/<id> \
    --mission missions/smoke.yaml --max-steps 20 --timeout 600 --headed

# 经 project_manager（推荐；会自动接进报告链）
python project_manager.py explore <id>
python project_manager.py run <id> --explore        # 全流程里追加该阶段
```

### mission 从哪来

沿用基座的任务格式（见 `missions/README.md`）。项目级任务放 `<项目目录>/mission.yaml`。
关键约定：

* `thread_id` 里含 `explorer` / `chaos` / `autonomous` 等关键字 → 基座走**高级图**；
  其余走标准图（三选一路由：new_user / power_user / adversarial）。
* 可选 `goal:` 块声明**可判定的目标**（`must_visit` / `require_actions` / `forbid_unreachable`）——
  不声明时用默认断言（至少 1 条成功动作 + 禁止连接级不可达）。

### 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `STA_EXPLORE_TIMEOUT` | `1800` | 阶段墙钟上限（秒）。写错退回默认，**不会变成"不限"** |
| `STA_AGENT_PYTHON` | 当前解释器 | 跑基座用的解释器（指向装有基座依赖的环境） |
| `AGENT_MAX_TURNS` / `AGENT_TOKEN_BUDGET` | 见序 5 | 基座**硬**护栏，透传给探索循环 |

## 6. 降级是"可降级阶段"的语义（重要）

探索阶段**不产出 pass/fail**，只产出**发现**。所以：

* 降级**不算流水线失败**（`run --explore` 不会因降级变红）；
* 但降级**必须出声**：写 `status=degraded` + `reason`，打到 stderr，并进 `run_meta` 与报告卡片；
* **"未执行" ≠ "通过"**：报告里明确标注降级原因，绝不把降级渲染成绿灯。

## 7. 诚实标注（本模块未覆盖的部分）

1. **`degraded_to` 只表达"回落到确定性链路"**，不是一个"规则版探索器"——
   基座里并不存在不依赖 LLM 的探索实现，本模块**没有**凭空造一个。
2. **缺陷原文不做结构化/定级**（只截断入库），折叠进缺陷草稿链是后续工作。
3. **token 计量沿用基座口径**（只能读到 `usage_metadata` 的部分），不是精确账。
