# 核心业务回归（extensions/regression）

把"核心业务回归测试"做成**声明式编排**：每个项目用 `regression.yaml` 描述核心业务场景，
执行器 `run_regression.py` 对被测项目环境发起真实请求、校验返回状态码，并产出结构化结果。

## 为什么这样设计
- **声明式**：测试同学/项目经理只需填写"哪些接口是核心、期望状态码"，无需写代码。
- **密钥分离**：`project.yaml` 只声明环境变量名（如 `MALL_ADMIN_USER`），真实口令走 `.env`（不入库）。
- **可编排**：核心场景与"全流程测试"解耦——`project run <id>` 跑全流程，`project regression <id>` 只跑核心回归（适合常态化回归门禁）。

## regression.yaml 字段
```yaml
project_id: mall-admin
core_business:
  - name: 健康检查
    type: api_smoke          # 固定
    method: GET
    path: /
    expect_status: 200
  - name: 管理员登录冒烟
    type: api_smoke
    method: POST
    path: /admin/login
    body:                   # 支持 {{username}}/{{password}} 占位，运行期替换为凭据
      username: "{{username}}"
      password: "{{password}}"
    expect_status: 200
  - name: 管理员列表(需登录)
    type: api_smoke
    method: GET
    path: /admin/list
    auth: required           # 需携带登录后 token
    expect_status: 200
```

## 运行
```bash
# 单独跑某项目核心回归
python extensions/regression/run_regression.py \
  --project projects/mall-admin/project.yaml \
  --regression projects/mall-admin/regression.yaml \
  --json projects/mall-admin/artifacts/regression.json

# 或在项目根目录用统一入口
python project_manager.py regression mall-admin
```

## 认证类型（project.yaml 的 env.auth.type）
- `none`：无需认证
- `form`：POST `login_url` 登录，从响应取 `token_field` 作为 Bearer 注入到 `auth: required` 的用例
- `bearer`：直接使用 `token_env` 指定的静态 token 环境变量

> 后续可扩展：把首次全流程跑通的高优先级用例自动沉淀为回归项（auto-capture）。
