"""认证上下文解析 + .env 加载（唯一定义处）。

密钥分离约定
------------
`project.yaml` 只声明环境变量**名**（`username_env` / `password_env` / `token_env`），
真实口令 / token 写在仓库根 `.env`（已被 gitignore，不入库）。本模块负责把 .env 载入
`os.environ`，并按变量名解析出认证上下文。

为什么必须有一个"且只有一个"版本
--------------------------------
`resolve_auth` 曾在 `run_regression.py` / `run_web.py` / `run_perf_security.py`
各有一份；`load_dotenv` 还额外在 `project_manager.py` 有一份。任一处改了取值规则
（例如新增一种认证类型），其余几处**不会跟着变**，也不会报错 —— 典型的静默分叉。
见 `docs/HARNESS_ARCHITECTURE_REVIEW.md` §4.1。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional


def _repo_root() -> Path:
    """仓库根：extensions/common/auth.py -> parents[2]。"""
    return Path(__file__).resolve().parents[2]


def load_dotenv(env_file: Optional[Path] = None) -> Optional[Path]:
    """把 .env 载入 `os.environ`，返回实际载入的文件路径（没有可读文件时 `None`）。

    查找顺序：
      1. 显式传入 `env_file` 时，**只**读该文件；
      2. 否则依次尝试 `$STA_ROOT/.env`、`<仓库根>/.env`，**第一个存在的生效**。

    写入用 `setdefault` 语义：**不覆盖已存在的环境变量**，CI 里显式注入的值优先。
    """
    candidates = []
    if env_file is not None:
        candidates.append(Path(env_file))
    else:
        if os.environ.get("STA_ROOT"):
            candidates.append(Path(os.environ["STA_ROOT"]) / ".env")
        candidates.append(_repo_root() / ".env")

    for p in candidates:
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        return p
    return None


def resolve_auth(project: Dict[str, Any]) -> Dict[str, Any]:
    """从 `project.yaml` 解析认证上下文（口令 / token 取自环境变量）。

    始终返回固定 6 键，缺失时用安全默认值：
    `type=none` / `login_url=""` / `token_field="token"` / 其余 `""`。
    这样调用方不必到处写 `.get(..., "")`，也不会因为少一个键而 KeyError。
    """
    env = project.get("env", {}) or {}
    auth = env.get("auth", {}) or {}
    return {
        "type": auth.get("type", "none"),
        "login_url": auth.get("login_url", ""),
        "token_field": auth.get("token_field", "token"),
        "username": os.getenv(auth["username_env"], "") if auth.get("username_env") else "",
        "password": os.getenv(auth["password_env"], "") if auth.get("password_env") else "",
        "token": os.getenv(auth["token_env"], "") if auth.get("token_env") else "",
    }
