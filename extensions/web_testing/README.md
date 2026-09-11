# 扩展⑤：Web UI 冒烟（Playwright + 声明式 YAML）

补齐项目承诺的「接口 / **Web 自动化**」里的 Web 那一半，已接入 CLI、报告、看板与 Web 控制台。

接口侧由 `extensions/api_testing` + `extensions/regression` 覆盖，
这里把「浏览器里的关键用户路径」也变成**可判定、可门禁**的声明式回归。

## 与基座的关系（不重复造轮子，但要分清用途）

| | 基座 `src/agentic_explorer/tools/browser/engine.py` | 本模块 `run_web.py` |
|---|---|---|
| 定位 | **面向智能体的交互式探索**（LLM 发 JSON 意图 → 执行 → 记 Action Tape） | **面向 CI 的确定性执行**（场景预先声明、结果可判定、失败即非零退出） |
| 适用 | 探索未知页面、找缺陷 | 回归门禁、上流水线 |
| 输入 | LLM 生成的意图 | 人写的 `web.yaml` |

两者互补：探索用基座，回归门禁用这里。
但**定位器与等待的方法论完全继承**基座（及 `agent-skills/web-automation`），
避免出现「智能体写出的用例被引擎拒绝、而门禁脚本却放行脆弱定位器」的双标。

## 继承的三条方法论约束

1. **定位器优先级**：`data-test-subj` → `aria-label` → 可见文本 → 语义 role。
   **拒绝 XPath 与位置选择器**（`/html/…`、`//div`、`:nth-child`、`:nth-of-type`、裸 div/span 链）——
   它们在视口或布局微调时就会碎，产出的是**假红**（噪音），所以**在使用前就被拦下**，
   并在输出里给出可直接抄的替代写法。
2. **显式等待替代固定 sleep**：用 Playwright 的 web-first 断言（自动重试）与条件等待，
   而不是 `sleep(2)`——固定 sleep 是 e2e 不稳定的头号来源。
3. **失败留证**：失败自动截图（`artifacts/web_shots/`），并生成可复现的
   `.spec.ts`（`artifacts/web_repro_<场景>.spec.ts`），可直接 `npx playwright test` 跑。

## 三道闸门（与核心回归 / 性能安全完全一致）

1. **无断言不算绿**：场景若没有任何 `expect_*` 步骤，即使所有操作都"成功"也无法判定对错
   → 记 `SKIP`（`kind=gate`），门禁不通过。防的是"点了就走、永远通过"的假绿场景。
2. **环境不可达不判绿**：连不上目标 / 浏览器起不来 → 全部 `SKIP`、`all_pass=False`、退出码 1。
   但**严格区分两类失败**：
   - **连接级错误**（`ERR_CONNECTION_REFUSED` 等）→ 算环境不可达，`SKIP`；
   - **HTTP 4xx/5xx** → 算产品/路由缺陷，`FAIL`。

   混为一谈的两种坏结果：把"服务没起"报成一堆用例失败（带偏排查），
   或更糟——"应用 500 被当成环境问题悄悄跳过"。
3. **配置问题单独报出**（`kind=config`）：脆弱定位器、未知步骤、缺必填参数
   → 记入 `config_issues`，**该场景不执行**（否则配置笔误会被当成产品缺陷报出去），
   门禁判不通过并显式打印。静默跳过会让门禁悄悄变松，是最危险的失败模式。

## 抖动与 flaky

默认 `retries: 0`（**不掩盖真实缺陷**：重试会吃掉偶发 bug 的证据）。
需要抗抖动时在 `web.yaml` 配 `retries: 1..2`。
**重试后才通过**的场景结果仍记 `PASS`，但打上 `flaky: true`、单独计数并在报告里标出——
重试可以降噪，但不该把抖动藏起来。

## 失败证据的清理

每次运行**先删掉上一次的 `web_shots/*.png` 与 `web_repro_*.spec.ts`**（只删这两类自产文件）。
不清的话，上一轮的残留会留在目录里被当成本次证据——
"明明本次通过，却看到一堆 FAIL 截图"，排查时误导极大。

## 用法

```bash
# 项目级（推荐）
python project_manager.py web mall-admin
python project_manager.py web mall-admin --only smoke        # 按场景名或 tags 筛
python project_manager.py web mall-admin --headed            # 有头，便于排查
python project_manager.py web mall-admin --browser firefox

# 全流程里带上（需求→用例→接口→回归→Web 冒烟→报告）
python project_manager.py run mall-admin --web

# 单独跑执行器
python extensions/web_testing/run_web.py \
    --project projects/mall-admin/project.yaml \
    --web projects/mall-admin/web.yaml \
    --json projects/mall-admin/artifacts/web.json
```

产物：`projects/<id>/artifacts/web.json` + `artifacts/web_shots/*.png`，
并渲染进 `artifacts/report.html`（「Web UI 冒烟」卡片）与跨项目看板。
控制台侧的失败证据走 `/reports/<pid>/` 与 `/reports/<pid>/web_shots/<file>` 两个静态路由
（后缀白名单 + 路径穿越防护，只放行证据类文件）。

## 配置示例（`projects/<id>/web.yaml`）

```yaml
web:
  # base_url 不写则回退 project.yaml 的 env.web_base_url / env.base_url
  base_url: http://localhost:8090
  browser: chromium          # chromium | firefox | webkit
  headless: true
  timeout_ms: 15000          # 单步超时
  retries: 0
  viewport: {width: 1440, height: 900}
  launch_args: []            # 崩溃逃生口，如 ["--no-sandbox","--disable-gpu","--disable-dev-shm-usage"]
  screenshot_on_failure: true

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

`{{username}}` / `{{password}}` 由 `project.yaml` 的 `env.auth.*_env` 指向的环境变量在运行期替换
（密钥分离：真实值放根 `.env`，不入库）。

## 可用步骤

| 类别 | 步骤 |
|---|---|
| 导航 | `goto` |
| 操作 | `click`、`fill`、`press`、`check`、`uncheck`、`hover`、`select_option`、`scroll_into_view` |
| 等待 | `wait_for`、`wait_for_hidden`、`wait_for_url`、`wait_for_load_state` |
| 断言 | `expect_visible`、`expect_hidden`、`expect_text`、`expect_value`、`expect_count`、`expect_url`、`expect_title` |
| 取证 | `screenshot` |

步骤可以写成两种形态：`- click: "<选择器>"`（快捷）或 `- click: {selector: "...", ...}`（带参数）。

## 环境坑（实测踩过）

1. **浏览器启动失败**：容器/受限环境常需要
   `launch_args: ["--no-sandbox","--disable-gpu","--disable-dev-shm-usage"]`，
   否则会出现 GPU 崩溃（Windows 上表现为 `0xC0000005`）。启动失败会被判为"环境不可达"而不是用例失败。
2. **代理污染**：本机若设了 `HTTP_PROXY`，访问 localhost 前端会走代理导致连接异常。
   执行器对 local/private 目标自动 `trust_env=False`（与性能压测同一处理）。
3. **Web 地址与接口地址不同**：`base_url` 是接口地址（如 `:8080`），前端往往在另一个端口（如 `:8090`），
   请显式配 `env.web_base_url` 或 `web.base_url`，否则"环境不可达"会全部 SKIP。

## 自检

```bash
.venv/Scripts/python.exe -m pytest tests/test_web_testing.py -q     # 执行器（含真实浏览器集成）
.venv/Scripts/python.exe -m pytest tests/test_web_smoke.py -q       # 控制台接入层
```

仓库里带了一个自检用的静态页 `tests/fixtures/web_smoke_page/index.html`（已入库）。
想手工复现一遍，三步：

```bash
python -m http.server 8090 -d tests/fixtures/web_smoke_page
# web.yaml 里把 base_url 指到 http://localhost:8090 并声明场景，然后：
python project_manager.py web <项目ID>          # 期望 ✅ 通过 / EXIT=0
python project_manager.py web <项目ID> --headed # 有头看过程
```
