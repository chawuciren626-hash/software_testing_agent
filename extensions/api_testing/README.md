# 扩展①：接口(API)自动化测试

基于 `pytest + requests` 的接口自动化，**不依赖 LLM**，可立即对 mall-admin 跑。

## 目录
- `conftest.py`：`base_url`、`session`、服务可达性自动跳过、`unique_suffix` 数据隔离夹具。
- `test_admin_login.py`：登录成功/错误密码/缺参 用例。
- `test_admin_register.py`：注册成功/重复用户名/缺参 用例（用 `unique_suffix` 隔离数据）。
- `requirements.txt`：依赖。

## 运行
```bash
# 安装依赖（在仓库根 .venv 内）
.venv/Scripts/pip install -r extensions/api_testing/requirements.txt

# 指定被测地址与凭据（可选）
export BASE_URL="http://localhost:8080"
export APP_USERNAME="admin"
export APP_PASSWORD="changeme"

# 运行（服务不可达会自动 skip）
.venv/Scripts/python -m pytest extensions/api_testing -v

# 生成 Allure 原始结果（可选）
.venv/Scripts/python -m pytest extensions/api_testing --alluredir=allure-results
```

## 与智能体的关系
- 本目录可独立进 CI（见 `extensions/reporting/.github/workflows/test.yml`）。
- 同时把方法论沉淀到 `agent-skills/api-test-design/SKILL.md`，
  探索智能体可经 `fetch_agent_skill` 加载它来生成新的 `test_*.py`。

## 待办
- [ ] 确认 mall-admin 真实返回契约（code/message/data 字段、token 位置），收敛断言。
- [ ] 补充鉴权后接口（如 `/admin/info` 需带 token）的链路用例。
- [ ] 接入 Allure 报告（见 extensions/reporting）。
