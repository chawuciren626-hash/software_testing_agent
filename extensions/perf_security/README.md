# 扩展④：性能 / 安全 冒烟

路线图的最后一块：**性能**与**安全**的可执行冒烟，已实现并接入 CLI、报告、看板与 Web 控制台。

## 为什么不用 locust

本项目的定位是「开发/CI 阶段的质量门禁」——需要的是**秒级完成、可直接进流水线、失败即非零退出**的冒烟；
locust 更适合专职压测（独立环境、较长观测窗口、人盯曲线）。
因此主执行器用 `concurrent.futures.ThreadPoolExecutor` + `requests`（**零新增依赖**），
指标口径向压测标准看齐：p50 / p95 / p99、错误率、吞吐（rps）、阈值门禁。

`locustfile_api.py` 作为**专职长压**入口保留（需 `pip install locust`），两者互补：
日常/CI 用冒烟，版本发布前需要长压时再起 locust。

## 用法

```bash
# 两项都跑（默认读 project.yaml 的配置）
python project_manager.py perf-security mall-admin

# 只跑安全 / 只跑性能
python project_manager.py perf-security mall-admin --only security

# 覆盖并发规模
python project_manager.py perf-security mall-admin --users 16 --iterations 10

# 全流程里带上（需求→用例→接口→回归→性能与安全→报告）
python project_manager.py run mall-admin --perf

# 单独跑执行器（可指定 JSON 产物）
python extensions/perf_security/run_perf_security.py \
    --project projects/mall-admin/project.yaml \
    --json projects/mall-admin/artifacts/perf_security.json
```

产物：`projects/<id>/artifacts/perf_security.json`，并渲染进 `artifacts/report.html`（「性能与安全冒烟」卡片）与跨项目看板。

## 压测目标从哪来（零配置可用）

优先级依次为：

1. `project.yaml` 的 `perf_security.perf.targets`（显式声明，最可控）；
2. **从 `regression.yaml` 的 `api_smoke` 项自动派生**（默认，取前 3 个只读接口）
   —— 保证「压的就是回归的那批接口」，避免两处声明漂移；
3. 内置兜底（健康检查 + 登录）。

### 写操作保护

路径含 `register / create / add / save / delete / remove / update / modify / upload / import / reset / logout / batch`
的目标**默认不参与压测**（避免造脏数据）。确实需要时显式加 `write: true`。

## 完整配置示例（project.yaml）

```yaml
perf_security:
  perf:
    users: 10               # 并发虚拟用户
    iterations: 5           # 每用户请求次数（总量 = users × iterations）
    warmup: 2               # 预热次数，不计入统计（排除首连与懒加载长尾）
    timeout: 10
    targets:
      - name: 管理员登录
        method: POST
        path: /admin/login
        body: {username: "{{username}}", password: "{{password}}"}
        expect_code: 200    # 业务码（不是 HTTP 状态码）；不写则只看 HTTP 状态码
      - name: 管理员列表
        method: GET
        path: /admin/list
        body: {pageNum: 1, pageSize: 10}
        auth: required      # 自动携带登录 token
        expect_code: 200
    thresholds:             # 全 0 / 不写 = 不做阈值判定
      p95_ms: 800           # P95 超过即判失败
      p99_ms: 1500
      max_error_rate: 0.01  # 比例；0.01 = 1%
      min_rps: 5            # 吞吐下限
  security:
    protected:              # 受保护接口（未授权访问检查的靶子）；不写则从 regression.yaml 派生
      - {name: 当前用户信息, method: GET, path: /admin/info}
```

## 安全检查项（6 项）

| 检查项 | 判定 | 说明 |
| --- | --- | --- |
| 未授权访问受保护接口 | FAIL 若可访问 | 不带 token 请求受保护接口，必须被拒绝 |
| 错误口令登录被拒绝 | FAIL 若下发 token | 错口令不得换到 token |
| SQL 注入探针未绕过认证 | FAIL 若绕过/回显库错误 | 4 组最小注入载荷（`or '1'='1`、`--`、`union select`、`#`），只读不破坏 |
| 错误响应不泄露堆栈信息 | FAIL 若命中堆栈特征 | 扫描 Traceback / `java.lang.` / `at com.` / `SQLSyntaxError` / 绝对路径等 |
| 错误口令响应不泄露用户是否存在 | WARN | 「存在的用户 + 错口令」与「不存在的用户 + 错口令」响应特征是否可区分（用户枚举） |
| 安全响应头检查 | FAIL 缺核心项 / WARN 其他 | 核心：`X-Content-Type-Options`、`X-Frame-Options`；`Access-Control-Allow-Origin: *` 与缺失 CSP/HSTS 记提示 |

**WARN 不影响门禁**（提示级，建议修复）；**FAIL 才拦门禁**。

## 部分重跑：不洗掉另一侧结果

只跑一侧时（`--only perf` / `--only security`），另一侧的既有结果会被**保留**，并在产物里
标注 `stale_sections` 与来源时间；门禁按两侧合并判定。
这与核心回归的「单场景重跑要合并回完整报告」是同一原则 ——
否则「只跑安全」会把你刚测出的性能数据静默抹掉，门禁结论也跟着失真。

## 三道防误判的闸门（血泪教训）

1. **防假绿 · 看业务码**：不少后端（mall-admin 即是）HTTP 状态码恒为 200，成败写在 body 的 `code` 里。
   只统计 HTTP 状态码会把 `code=500` 当成功。因此目标声明了 `expect_code`（或 regression 声明了
   `expect_json.code`）时按**业务码**判错。
2. **防假绿 · 环境不可达不判绿**：所有请求都连不上 → 全部 `SKIP`，`all_pass=False`，退出码 1。
   绝不让 CI 拿到"性能与安全通过"的虚假信号。
3. **防假红 · 基线校验**：地址写错、端口被别的服务占用、被代理拦截、凭据过期时，响应往往既没有安全响应头、
   也不返回 401。若直接判定就会得出「未授权访问 = 存在漏洞」这类**假漏洞**（比漏报更误导人）。
   因此先做**基线登录**：基线不成立 → 依赖认证的检查全部 `SKIP` 并说明原因。

## 两个环境相关的坑

- **必须绕开 HTTP 代理**：机器上若设置了 `HTTP_PROXY`，请求会多一跳代理与排队，
  p95/p99 和吞吐会被污染到毫无参考价值。执行器对本地/内网目标（`localhost`/`127.0.0.1`/私有网段/`.local`）
  强制直连（`Session.trust_env=False`），公网目标仍沿用环境变量里的代理。
- **必须加载 `.env`**：真实口令只在 `.env`（密钥分离，已 gitignore）。
  独立运行执行器（或 `extensions/regression/run_regression.py`）时若没人加载 `.env`，
  凭据为空 → 登录失败 → 受保护接口全部 401 → 表现为"性能/安全大面积失败"，极难排查。
  两个执行器的独立入口都已自动加载 `.env`（`setdefault` 语义，CI 显式注入的值优先）。

## 退出码与门禁

`all_pass = 性能通过 且 安全无 FAIL`；环境不可达时 `all_pass=False`。
退出码 `0 / 1` 可直接做流水线门禁。
