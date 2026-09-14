"""响应体占位替换与点路径取值（唯一定义处）。

`substitute` 曾在 `run_regression` / `run_web` / `run_perf_security` 各一份，
`dig` 在 `run_regression` / `run_perf_security` 各一份 —— 同样收敛到此处。
"""
from __future__ import annotations

from typing import Any, Dict


def substitute(body: Any, auth: Dict[str, Any]) -> Any:
    """把 `{{username}}` / `{{password}}` 占位替换为凭据（递归 dict / list）。"""
    if isinstance(body, str):
        return body.replace("{{username}}", auth["username"]).replace("{{password}}", auth["password"])
    if isinstance(body, dict):
        return {k: substitute(v, auth) for k, v in body.items()}
    if isinstance(body, list):
        return [substitute(v, auth) for v in body]
    return body


def dig(data: Any, dotted: str) -> Any:
    """按点路径取嵌套字段，如 `data.token` -> `json['data']['token']`。

    中途遇到非 dict（或键不存在）返回 `None` —— 表达"取不到"，不抛异常。
    """
    cur = data
    for part in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur
