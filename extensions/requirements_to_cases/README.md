# 扩展②：需求分析 → 测试用例

把需求/PR 文本转成结构化用例（等价类 / 边界值 / 异常），零依赖即可运行，
也可挂 LLM 做更智能的生成。

## 运行（规则版，无需 LLM）
```bash
.venv/Scripts/python extensions/requirements_to_cases/generate_cases.py \
    --input extensions/requirements_to_cases/sample_requirements.md \
    --output extensions/requirements_to_cases/cases.md
```

## 运行（LLM 增强，需基座依赖 + ANTHROPIC_API_KEY / GOOGLE_API_KEY）
```bash
.venv/Scripts/python extensions/requirements_to_cases/generate_cases.py \
    --input sample_requirements.md --llm
```

## 输出
符合 `agent-skills/requirements-to-cases` 字段规范的 Markdown 用例表：
`id / 标题 / 模块 / 类型 / 优先级 / 前置 / 步骤 / 预期 / 可自动化`。

## 与智能体的关系
- 产物 `cases.md` 可经 `agent-explorer --missions` 调度；
- 方法论沉淀在 `agent-skills/requirements-to-cases/SKILL.md`，
  探索智能体可据此把需求直接转成 mission。

## 待办
- [ ] 接入基座 `pr_analyzer.py`，支持直接吃 PR diff 生成用例。
- [ ] LLM 版增加「场景法/判定表」覆盖，并输出可直接落地的 `test_*.py`。
- [ ] 与 ①接口自动化 打通：需求用例 → 自动生成 pytest 骨架。
