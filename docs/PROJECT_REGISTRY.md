# 多项目对接（Project Registry）

让"软件测试智能体"对接公司各类项目：新建项目 = 填写项目信息（测试环境地址、需求等），
即可对该项目执行**全流程测试**与**核心业务回归**。

## 目录结构
```
projects/
  <project_id>/
    project.yaml        # 项目元数据 + 测试环境地址 + 认证方式（入库）
    requirements.md     # 项目需求（驱动全流程测试，入库）
    regression.yaml     # 核心业务回归场景声明（入库）
    artifacts/          # 生成产物（用例/报告/Allure，不入库）
```

## 统一入口
```bash
python project_manager.py create            # 交互式填写项目信息（也支持全参传入）
python project_manager.py list              # 列出已接入项目
python project_manager.py info <id>         # 查看项目详情
python project_manager.py run <id>          # 全流程：需求->用例 -> 接口自动化 -> 核心回归 -> 报告
python project_manager.py regression <id>   # 仅核心业务回归（常态化门禁）
python project_manager.py dashboard         # 跨项目总览看板
```

CI / 批量接入可全参建项目（不会触发交互等待）：
```bash
python project_manager.py create --id mall-admin --name "商城后台管理系统" \
    --base-url http://localhost:8080 --owner 张三 --auth-type form \
    --login-url /admin/login --user-env APP_USERNAME --pass-env APP_PASSWORD \
    --requirements-file path/to/requirements.md
```

## 核心回归项类型（regression.yaml）

| type | 关键字段 | 说明 |
|---|---|---|
| `api_smoke` | `method` / `path` / `expect_status` / `body` / `auth` | 声明式 HTTP 检查；`body` 支持 `{{username}}`、`{{password}}` 占位 |
| `pytest_marker` | `target` / `marker` | **复用已有 pytest 用例**，按 marker 筛选（如 `-m smoke`）。推荐 |
| `pytest_node` | `node` | 复用已有 pytest 用例，指定 node id（`path::test_name`） |

推荐 `pytest_marker` / `pytest_node`：让核心回归与已写好的自动化用例**同源**，
不必重复声明，也不会出现"回归脚本与用例脱节"的漂移问题。

`api_smoke` 中 `auth: required` 表示该项需携带登录 token（由 `project.yaml` 的 form 登录自动获取）。

> ⚠ **务必配 `expect_json`**：不少后端（如 mall-admin）HTTP 状态码恒为 200，成败写在响应体的业务码里。
> 只断言 `expect_status` 会产生"密码错了也判通过"的**假绿**。
> `expect_json` 支持点路径（如 `data.token`），取值 `__not_null__` 表示该字段必须非空。

示例：
```yaml
core_business:
  - name: 健康检查
    type: api_smoke
    method: GET
    path: /
    expect_status: 200

  - name: 管理员登录冒烟
    type: api_smoke
    method: POST
    path: /admin/login
    body:
      username: "{{username}}"
      password: "{{password}}"
    expect_status: 200
    expect_json:
      code: 200                   # 业务成功码（不是 HTTP 状态码）
      data.token: "__not_null__"

  - name: 管理员列表查询(需登录)
    type: api_smoke
    method: GET
    path: /admin/list
    body:
      pageNum: 1
      pageSize: 10
    auth: required
    expect_status: 200
    expect_json:
      code: 200
      data.list: "__not_null__"

  - name: 登录/注册核心用例(复用 pytest)
    type: pytest_marker
    target: extensions/api_testing
    marker: smoke
```

## 门禁语义
- 单项结果分 `PASS` / `FAIL` / `SKIP`。
- **无 FAIL 且确有实际执行（passed > 0）才算通过。**
- 环境不可达导致全部 SKIP 时**不判绿**，并打印告警——避免 CI 给出虚假的安全信号。
- 退出码：通过 `0`，未通过 `1`，可直接用于流水线门禁。

## project.yaml 字段
| 字段 | 说明 |
|---|---|
| `project_id` | 英文唯一标识（目录名） |
| `name` / `description` / `owner` | 元数据 |
| `env.base_url` | 测试环境地址（接口自动化与回归都指向它） |
| `env.auth.type` | `none` / `form` / `bearer` |
| `env.auth.login_url` | form 登录接口路径 |
| `env.auth.username_env` / `password_env` | 凭据环境变量**名**（真实值放 `.env`，不入库） |
| `env.auth.token_field` | 登录响应中 token 字段名（默认 `token`） |
| `requirements_file` / `regression_file` | 相对路径 |

## 密钥分离约定
- `project.yaml` **只存环境变量名**，不存真实口令/token。
- 真实值写在仓库根 `.env`（已被 `.gitignore` 忽略），`project_manager` 启动时自动载入：
  ```
  APP_USERNAME=admin
  APP_PASSWORD=changeme
  ```

## 与现有能力的关系
- 全流程的"需求→用例"复用 `extensions/requirements_to_cases`。
- 全流程的"接口自动化"复用 `extensions/api_testing`（pytest + requests，经 `BASE_URL` 指向本项目环境）。
- "核心回归"由 `extensions/regression/run_regression.py` 执行，可复用上述 pytest 用例。
- 单项目报告：`projects/<id>/artifacts/report.html`；跨项目看板：`projects_dashboard.html`。

## 后续演进
- **自动沉淀**：首次全流程跑通的高优先级用例自动加入 `regression.yaml`（auto-capture）。
- **Web 表单**：在 CLI 之上提供可视化建项目界面。
- **CI 集成**：流水线中对所有接入项目批量执行 `regression`，看板作为交付质量门禁。
