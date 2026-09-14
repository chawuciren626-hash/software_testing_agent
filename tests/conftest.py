"""P0 测试公共配置：把仓库根及关键子目录加入 sys.path，便于无包导入。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 仓库根（tests/ 的上级）
# "src" 是基座包 `agentic_explorer` 的根（命名空间包）。加入是为了让**零依赖**的
# `orchestration/guardrails.py` 能在不装 langgraph/langchain 的 CI 硬门禁里被直接测试。
for sub in ("", "src", "web_console", "extensions", "extensions/regression",
           "extensions/perf_security",
           "extensions/web_testing", "extensions/requirements_to_cases",
           "extensions/reporting", "tests/eval"):
    p = os.path.join(ROOT, sub)
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)
