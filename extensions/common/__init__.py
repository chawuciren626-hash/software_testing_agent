"""跨扩展共享的**唯一实现层**（口径唯一）。

为什么存在
----------
同一件事（读 YAML、解析认证上下文、载入 .env、占位替换、判定门禁、解析
cases.md 表格）曾在多个扩展里**各自复刻一份**。复刻不会立刻出错，但任一处
修了规则、另一处没跟上，就会**静默不一致** —— 表现成最熟悉的那种"改了没效果"，
而且不会有任何测试变红。

本包把这几件事收敛为**唯一定义处**，其余模块只做委托（`from common.x import y as _y`）。
动机、证据与验收口径见 `docs/HARNESS_ARCHITECTURE_REVIEW.md` §4.1 与 §7 序 2。

模块
----
- `yamlio`  —— `load_yaml`：读 YAML 文件为 dict
- `auth`    —— `resolve_auth` / `load_dotenv`：认证上下文解析、.env 加载（密钥分离）
- `data`    —— `substitute` / `dig`：`{{username}}` 占位替换、点路径取值
- `gates`   —— `all_pass`：核心回归 / Web 冒烟共用的门禁谓词（全 SKIP 不判绿）
- `cases`   —— `parse_rows`：cases.md Markdown 表格行解析
- `obs`     —— 统一日志出口 + `run_id` 贯通：`get_logger` / `setup` / `new_run_id`

防漂移
------
`tests/test_common_parity.py` 守两件事，任何一处被重新复制出去就会立刻变红：
1. **身份**：各调用方拿到的是本包的**同一个函数对象**（`is` 判定），不是各写一份；
2. **唯一性**：AST 扫描 `extensions/`，上述函数的 `def` 定义数必须各为 1。
"""
