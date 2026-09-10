"""P0 测试公共配置：把仓库根及关键子目录加入 sys.path，便于无包导入。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 仓库根（tests/ 的上级）
for sub in ("", "web_console", "extensions/regression",
           "extensions/requirements_to_cases", "tests/eval"):
    p = os.path.join(ROOT, sub)
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)
