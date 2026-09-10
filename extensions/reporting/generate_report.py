"""测试报告聚合器：把分散的测试产物汇总为一个 HTML 入口。

扫描：
- ./report_* 目录中的 test_report.md（基座 Web 探索测试产物）
- ./allure-results（extensions/api_testing 的 Allure 原始结果，若存在）

生成 test_report_index.html 作为总览入口（含目录、各 mission 摘要、Allure 链接）。
纯标准库实现，可在 CI 末尾调用。
"""
from __future__ import annotations

import datetime
import glob
import html
import os
import re

ROOT = os.getcwd()


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def collect_explorer_reports() -> list[tuple[str, str]]:
    out = []
    for d in sorted(glob.glob("report_*")):
        md = os.path.join(d, "test_report.md")
        if os.path.isfile(md):
            out.append((d, _read(md)))
    return out


def collect_allure() -> str | None:
    if os.path.isdir("allure-results"):
        n = len([p for p in os.listdir("allure-results") if p.endswith(".json") or p.endswith(".xml")])
        return f"allure-results/ （{n} 个结果文件）"
    return None


def _first_lines(md: str, n: int = 12) -> str:
    lines = [l for l in md.splitlines() if l.strip()][:n]
    return "\n".join(lines)


def render() -> str:
    reports = collect_explorer_reports()
    allure = collect_allure()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    parts = [
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>",
        "<title>软件测试智能体 - 测试报告总览</title>",
        "<style>body{font-family:-apple-system,Segoe UI,Roboto,'Microsoft YaHei',sans-serif;margin:32px;color:#1f2937;}"
        "h1{color:#4f46e5;} .card{border:1px solid #e5e7eb;border-radius:10px;padding:16px;margin:16px 0;}"
        ".meta{color:#6b7280;font-size:13px;} pre{background:#f8fafc;padding:12px;border-radius:8px;overflow:auto;}"
        ".tag{display:inline-block;background:#eef2ff;color:#4338ca;border-radius:6px;padding:2px 8px;font-size:12px;}</style>",
        "</head><body>",
        f"<h1>软件测试智能体 · 测试报告总览</h1>",
        f"<div class='meta'>生成时间：{now} · 探索测试产物：{len(reports)} 个"
        + (f" · Allure：{html.escape(allure)}" if allure else " · Allure：无") + "</div>",
    ]

    if allure:
        parts.append("<div class='card'><b>Allure 报告</b><br/>执行 "
                     "<code>allure serve allure-results</code> 或 "
                     "<code>allure generate allure-results -o allure-report</code> 查看。</div>")

    if not reports:
        parts.append("<div class='card'>暂无 report_* 产物。先运行 "
                     "<code>agent-explorer --missions missions/...</code> 或接口自动化 pytest。</div>")
    else:
        for d, md in reports:
            snippet = html.escape(_first_lines(md))
            parts.append(
                f"<div class='card'><span class='tag'>{html.escape(d)}</span>"
                f"<pre>{snippet}</pre>"
                f"<div class='meta'>完整内容见 <code>{html.escape(d)}/test_report.md</code></div></div>"
            )

    parts.append("</body></html>")
    return "\n".join(parts)


def main() -> None:
    out = os.path.join(ROOT, "test_report_index.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(render())
    print(f"已生成 {out}")


if __name__ == "__main__":
    main()
