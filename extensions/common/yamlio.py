"""统一的 YAML 读取（唯一定义处）。

曾在 `run_regression.py` / `run_web.py` / `run_perf_security.py` 各复刻一份
（后两处还是"独立运行时的兜底副本"）。三份逻辑相同，但"相同"是靠人肉维持的，
不是靠一个函数维持的 —— 本模块把它变成后者。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

try:
    import yaml
except ImportError:  # pragma: no cover - 延迟到调用点报错，保持报错口径一致
    yaml = None


def load_yaml(path: Any) -> Dict[str, Any]:
    """读取 YAML 文件为 dict；空文件 / 纯注释返回 `{}`。

    - 接受 `str` 或 `Path`（内部统一 `Path(path)`）；
    - 未安装 PyYAML 时抛 `RuntimeError`，文案与既有各扩展一致，避免调用方
      的错误提示口径分叉。
    """
    if yaml is None:
        raise RuntimeError("需要 PyYAML，请先安装依赖：pip install pyyaml")
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
