---
name: requirements-to-cases
description: 需求/PR → 结构化测试用例的方法论与生成流程。覆盖等价类划分、边界值、场景法、判定表，以及输出用例的字段规范。供探索智能体把需求转成 mission 或 pytest 用例时调用。
version: 0.1.0

---

# 需求分析 → 测试用例 技能

## 适用场景
- 拿到需求文档、用户故事、PR diff，需要产出高质量、可评审的测试用例。
- 把自然语言需求转成结构化用例（标题/前置/步骤/预期/优先级/类型）。

## 用例设计方法（按需组合）
1. **等价类划分**：有效/无效等价类，减少冗余。
2. **边界值分析**：上点/离点/内点（如长度、数值、分页）。
3. **场景法**：正常流 + 异常流 + 备选流（登录/注册/下单主链路）。
4. **判定表**：多条件组合（权限、状态机）。

## 用例字段规范
| 字段 | 说明 |
|---|---|
| id | 唯一编号（如 REQ-LOGIN-001） |
| title | 一句话标题 |
| module | 所属模块 |
| type | 功能/边界/异常/性能/安全 |
| priority | P0/P1/P2 |
| preconditions | 前置条件 |
| steps | 步骤列表 |
| expected | 预期结果 |
| automated | 是否可自动化（pytest/Playwright） |

## 生成流程（见 extensions/requirements_to_cases/generate_cases.py）
1. 输入：需求文本 / PR diff / OpenAPI。
2. 经 LLM 或规则模板抽取上述字段。
3. 输出：Markdown 用例表 + 可落地的 `test_*.py` 或 `missions/*.yaml`。

## 与基座的对接
- 产出的 `missions/*.yaml` 可直接被 `agent-explorer --missions` 调度；
- 产出的 `test_*.py` 进入 `extensions/api_testing/` 或 Web 测试目录。
