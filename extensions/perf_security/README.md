# 扩展④：性能 / 安全 探索测试（规划与骨架）

长期目标：在基座「适配器契约 + 多人格智能体」之上，扩展性能与安全探索能力。

## 性能测试（适配器 + mission）
- 适配器思路：新增 `extensions/perf_security/locustfile_api.py` 对 mall-admin 关键接口（登录、注册、列表查询）做并发压测。
- 与基座关系：性能结果可作为高级人格的「上下文」注入，或作为 `missions/perf_*.yaml` 调度。
- 进阶：把压测脚本包装成 Skill，供探索智能体在 `--pr-url` 变更热点接口时自动触发回归压测。

## 安全测试（人格 + mission）
- 鉴权：未带 token 访问受保护接口应 401；越权访问应被拒。
- 注入：登录/注册等输入点做 SQL/命令注入探针（最小可行集，避免破坏数据）。
- 实现：以 `missions/security_*.yaml` 描述检查项，或由安全人格智能体驱动。

## 已落骨架
- `locustfile_api.py`：登录接口并发压测骨架（需 `pip install locust`）。
- `security_mission.yaml`：安全探索 mission 模板（供 `agent-explorer --missions` 调度）。

## 待办
- [ ] 性能：补列表/下单等高频接口；定义 SLA（P95、错误率）门禁。
- [ ] 安全：明确可执行的探针清单与「只读/不破坏」约束；接入基座人格。
- [ ] 与 ③报告 打通：性能/安全结果进入 `test_report_index.html`。
