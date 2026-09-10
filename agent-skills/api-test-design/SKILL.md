---
name: api-test-design
description: API/接口自动化测试设计与执行方法论。覆盖 pytest+requests 工程结构、数据隔离(unique_suffix)、mall-admin 接口用例、断言与报告。供探索智能体在生成接口测试 mission 时调用。
---

# API 测试设计技能

## 适用场景
- 对 REST/HTTP 接口做自动化测试（如 mall-admin 的 `/admin/login`、`/admin/register`）。
- 需要可重复、数据隔离、可提交的接口测试用例。

## 工程结构（见 extensions/api_testing/）
- `conftest.py`：`base_url` 配置 + `unique_suffix` fixture（动态唯一后缀，隔离 register/delete/update 用例数据）。
- `test_admin_login.py`：登录成功/失败/参数异常用例。
- `test_admin_register.py`：注册用例，使用 `unique_suffix` 避免用户名冲突。
- `requirements.txt`：`requests`、`pytest`（可选 `pytest-allure`）。

## 关键设计原则
1. **数据隔离**：写操作（注册/删除/更新）用例用 `unique_suffix` 生成动态唯一数据，互不污染。
2. **断言分层**：状态码 + 业务码 + 关键字段；不只看 200。
3. **服务不可达保护**：`conftest` 探测 `base_url` 健康，不可达时用例 `skip`，不红不阻塞。
4. **可提交**：纯 pytest，不依赖 LLM，可进 CI。

## 使用方式（供智能体）
- 直接读本 SKILL.md 获取方法论；
- 或运行 `extensions/api_testing/run_api_tests.sh` 触发 `pytest`（待补充脚本）。
- 智能体可据此生成新的 `test_*.py` 并加入 `missions/api_smoke.yaml` 调度。

## 常用接口（mall-admin）
| 接口 | 方法 | 说明 |
|---|---|---|
| `/admin/login` | POST | 管理员登录，返回 token |
| `/admin/register` | POST | 管理员注册（需唯一用户名） |
| `/admin/info` | GET | 当前管理员信息（需鉴权） |
