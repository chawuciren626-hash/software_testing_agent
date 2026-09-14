"""cases.md Markdown 表格行解析（唯一定义处）。

曾被复刻成三份，且**语义并不一致**（这是本次收敛时新发现的，报告原文只写了
"≥2 处"）：

| 位置 | 遇到非表格行 | 单元格少于表头 | 表头空列 |
|---|---|---|---|
| `extensions/memory/focus.py:_parse_rows` | `break`（停止） | **补齐**空串 | 保留 |
| `project_manager.py:_parse_cases` | `continue`（跳过继续） | 丢弃该行 | 过滤掉 |
| `extensions/requirements_to_cases/case_quality.py:parse_rows` | `continue` | 丢弃该行（阈值 `min(2,n)`） | 保留 |

对**真实生成**的 cases.md（单张满列连续表）三者结果相同；差异只在畸形输入下显现。
本次统一到下面这一套（最保守、最不丢信息）：
- 表格起点 = "表头行 + 紧随其后的 `|---|` 分隔行"；
- 从表头下一行起读取**连续**表格行，遇到第一个非表格行即**停止** ——
  表格是连续的；`break` 可避免误吞正文后面可能出现的另一张表；
- 单元格 `strip`；行单元格少于表头时右侧**补空串**，保证每行字段与表头对齐；
- 表头过滤空列名（避免产生 `""` 键）。
"""
from __future__ import annotations

from typing import Dict, List


def parse_rows(md_text: str) -> List[Dict[str, str]]:
    """从 cases.md 解析用例行；无表格返回 `[]`。语义见模块 docstring。"""
    rows: List[Dict[str, str]] = []
    if not md_text:
        return rows
    lines = md_text.splitlines()

    start = -1
    for i, ln in enumerate(lines):
        if ln.startswith("|") and "---" in ln:
            start = i - 1
            break
    if start < 0:
        return rows

    headers = [h.strip() for h in lines[start].split("|")[1:-1] if h.strip()]
    if not headers:
        return rows

    for ln in lines[start + 2:]:
        if not ln.strip() or not ln.startswith("|"):
            break                       # 表格结束（遇到空行 / 非表格行即停）
        cells = [c.strip() for c in ln.split("|")[1:-1]]
        if len(cells) < len(headers):
            cells += [""] * (len(headers) - len(cells))
        rows.append(dict(zip(headers, cells)))
    return rows
