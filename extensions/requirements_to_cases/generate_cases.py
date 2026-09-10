"""需求/PR → 结构化测试用例生成器（规则版，零外部依赖）。

设计：
- 纯标准库实现，无需 LLM 即可跑（适合 CI / 离线）。
- 解析需求文本中的条目（编号/项目符号列表），为每条需求生成
  功能 / 边界 / 异常 三类用例，输出符合 agent-skills/requirements-to-cases 字段规范的 Markdown。
- 可选 LLM 增强：若环境变量 ANTHROPIC_API_KEY / GOOGLE_API_KEY 存在，
  且基座依赖已装，可改用基座 make_llm 做更智能的生成（见 _llm_generate）。

用法：
    python generate_cases.py --input sample_requirements.md --output cases.md
    python generate_cases.py --input pr_description.txt            # 输出到 stdout
"""
from __future__ import annotations

import argparse
import datetime
import re
import sys
from typing import Dict, List


def parse_requirements(text: str) -> List[str]:
    """从需求文本中抽取条目（编号 1. / 1) 或 - / * 开头的行）。"""
    items: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):        # 跳过 Markdown 标题
            continue
        m = re.match(r"^(\d+[.)]|[+\-*])\s+(.*)", line)
        if m:
            items.append(m.group(2).strip())
        elif len(items) == 0 and len(line) > 6:
            # 非列表但像一句需求描述，作为单条兜底
            items.append(line)
    return [i for i in items if i]


def _case(rid: str, title: str, rtype: str, priority: str, pre: str, steps: str, expected: str) -> Dict:
    return {
        "id": rid,
        "title": title,
        "module": "待定",
        "type": rtype,
        "priority": priority,
        "preconditions": pre,
        "steps": steps,
        "expected": expected,
        "automated": "可（pytest/Playwright）",
    }


def gen_cases(items: List[str]) -> List[Dict]:
    cases: List[str] = []  # type: ignore
    out: List[Dict] = []
    for i, req in enumerate(items, 1):
        rid = f"REQ-{i:03d}"
        base = req.rstrip("。. ")
        out.append(_case(f"{rid}-F", f"{base}（功能）", "功能", "P1",
                         "系统已部署且可访问", f"1. 按需求执行：{base}", "功能按预期正常完成，无报错"))
        out.append(_case(f"{rid}-B", f"{base}（边界）", "边界", "P2",
                         "准备边界输入数据", f"1. 使用边界值（最小/最大/临界）执行：{base}", "边界条件下系统处理正确，无越界/崩溃"))
        out.append(_case(f"{rid}-N", f"{base}（异常）", "异常", "P1",
                         "准备异常/非法输入", f"1. 输入非法/缺失数据执行：{base}", "系统给出明确错误提示，不发生脏数据/异常中断"))
    return out


def to_markdown(cases: List[Dict], source: str) -> str:
    lines = [
        f"# 测试用例（由需求生成）",
        f"- 来源：{source}",
        f"- 生成时间：{datetime.datetime.now():%Y-%m-%d %H:%M}",
        f"- 用例数：{len(cases)}",
        "",
        "| id | 标题 | 模块 | 类型 | 优先级 | 前置 | 步骤 | 预期 | 可自动化 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for c in cases:
        lines.append(
            f"| {c['id']} | {c['title']} | {c['module']} | {c['type']} | {c['priority']} "
            f"| {c['preconditions']} | {c['steps']} | {c['expected']} | {c['automated']} |"
        )
    return "\n".join(lines) + "\n"


def _llm_generate(text: str) -> str:
    """可选：用基座 make_llm 做智能生成（需已安装基座依赖且有 LLM key）。"""
    try:
        from agentic_explorer.utils.llm import make_llm
    except Exception as e:
        return f"# LLM 生成不可用：{e}\n\n请安装基座依赖并配置 LLM key，或用规则版（默认）。"
    llm = make_llm(temperature=0)
    prompt = (
        "你是资深测试工程师。把下面需求转成结构化测试用例（Markdown 表格，"
        "字段：id/标题/模块/类型/优先级/前置/步骤/预期/可自动化）。\n\n"
        f"需求：\n{text}"
    )
    resp = llm.invoke(prompt)
    return resp.content if hasattr(resp, "content") else str(resp)


def main() -> None:
    ap = argparse.ArgumentParser(description="需求 → 测试用例生成器")
    ap.add_argument("--input", "-i", help="需求文件路径（缺省读 stdin）")
    ap.add_argument("--output", "-o", help="输出 Markdown 路径（缺省 stdout）")
    ap.add_argument("--llm", action="store_true", help="启用 LLM 增强生成（需 key+基座依赖）")
    args = ap.parse_args()

    if args.input:
        with open(args.input, encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()

    if args.llm:
        result = _llm_generate(text)
    else:
        items = parse_requirements(text)
        if not items:
            result = "# 未解析到需求条目。请使用编号/项目符号列表书写需求。\n"
        else:
            result = to_markdown(gen_cases(items), args.input or "stdin")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(result)
        print(f"已写出 {args.output}")
    else:
        print(result)


if __name__ == "__main__":
    main()
