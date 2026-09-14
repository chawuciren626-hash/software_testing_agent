"""L 层最小拦截（审阅报告 §4.3）：只读模式 + 审计。

为什么需要（先说清取舍）
------------------------
控制台默认只监听 `127.0.0.1`、单人本地使用，**不做强制鉴权**是合理的
（设计说明见 `auth.py`）。可一旦把端口暴露到局域网（同事试用 / 内网演示 /
放在测试机上），就冒出两个之前没堵的缺口：

1. **谁都点得动**：任何能打开页面的人都能触发全流程 / 回归 / 压测，还能删项目。
2. **出事了查不到**：删了哪个项目、谁触发的、什么时候 —— 当前不留任何痕迹。

本模块只补**最小**的两件，不引入身份体系（那是 `auth.py` 的职责）：

- **审计（audit）**：所有会改状态的请求（`POST`/`PUT`/`PATCH`/`DELETE` 打到 `/api/*`）
  追加一行到 `audit.jsonl` —— 谁、何时、对哪个项目、结果（HTTP 状态码）。
- **只读模式（readonly）**：`STA_CONSOLE_READONLY=1` → 允许看（GET），
  禁止一切改状态的动作。演示 / 试用时的安全档。

案例对照：Claude Code 的 `allow/ask/deny` 分级 —— 高危动作应当由**确定性规则**
拦截，而不是指望使用者自律。

诚实标注（不假装全覆盖）
------------------------
- **未开鉴权时没有强身份**：`actor` 只能记 `anonymous` + 远端 IP。这是如实记录，
  不假装"有身份"。要真身份就开 `auth.py` 的 token。
- **审计不是防篡改**：本地 append-only 文本，有文件权限的人能改。
  它的价值是"有据可查 + 事后复盘"，不是"不可抵赖"。
- **只读模式不是沙箱**：它拦的是**本控制台入口**，拦不住直接跑 CLI 的人。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote

from flask import Flask, g, has_request_context, jsonify, request, session

from common.obs import get_logger, get_run_id

log = get_logger("web_console.guard")

# 只读模式开关（=1 开启；off/false/0/空 视为关闭，与 auth.py 的 TOKEN_ENV 口径一致）
READONLY_ENV = "STA_CONSOLE_READONLY"
# 审计文件路径覆盖（默认落在 DATA_ROOT/audit.jsonl）。留这个口子是为了让测试
# 能把审计写到 tmp_path，而不是污染仓库根。
AUDIT_FILE_ENV = "STA_AUDIT_FILE"

# 与 auth.py 保持同一套"显式关闭"的取值口径，避免两处对 off 的理解不一致。
_OFF_VALUES = {"", "off", "0", "false", "no", "none", "disabled", "null"}

# "会改状态"的方法；GET/HEAD/OPTIONS 属于"看"，不留审计（否则日志被读请求刷屏）
_MUTATING = ("POST", "PUT", "PATCH", "DELETE")

_data_dir: Optional[Path] = None


def readonly() -> bool:
    """是否处于只读模式（每次实时读环境变量，改完重启即生效，也便于测试）。"""
    return (os.getenv(READONLY_ENV) or "").strip().lower() not in _OFF_VALUES


def audit_file() -> Path:
    """审计文件路径：优先 `STA_AUDIT_FILE`，否则落在安装时给的 data_root。"""
    override = (os.getenv(AUDIT_FILE_ENV) or "").strip()
    if override:
        return Path(override)
    return (_data_dir or Path.cwd()) / "audit.jsonl"


def record(event: str, **fields: Any) -> None:
    """追加一条审计行（JSON Lines）。

    **审计写失败不能反过来弄挂业务** —— 所以只出声、不抛异常。
    但也**绝不能静默**：静默的审计 = 以为有记录其实没有，比没有审计更危险。
    """
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "event": event,
        "run_id": get_run_id(),
        **fields,
    }
    try:
        p = audit_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # 必须出声：写不进审计等于这个操作没被留痕
        log.warning("[audit] 写入失败（该操作未被留痕）：%s", e)


def _actor() -> str:
    """谁在操作。

    开了鉴权 → 会话里的 token 指纹前 8 位（不含 token 本体，避免泄密）；
    未开鉴权 → `anonymous`（如实标注，不编造身份）。
    这里直接读 session 键而不 import auth，是为了避免 auth ↔ guard 循环导入；
    键名 `sta_authed` 与 auth.py 对齐。
    """
    if not has_request_context():
        return "anonymous"
    fp = session.get("sta_authed")
    return f"token:{fp[:8]}" if fp else "anonymous"


def _target(path: str) -> str:
    """从 `/api/projects/<pid>/...` 里取出 pid，供审计回答"对哪个项目"。"""
    m = re.match(r"^/api/projects/([^/]+)", path)
    return unquote(m.group(1)) if m else "-"


def install(app: Flask, data_root: Path) -> None:
    """把只读拦截与审计挂到 Flask 应用上。

    **必须在 `auth.install(app)` 之后调用**：before_request 按注册顺序执行，
    鉴权应先于只读判定 —— 否则未登录的人会先撞上 403（只读）而不是 401（未登录），
    前端就拿不到"该去登录"的信号。
    """
    global _data_dir
    _data_dir = data_root

    @app.before_request
    def _readonly_gate() -> Any:
        if not readonly():
            return None
        path = request.path or "/"
        if request.method in _MUTATING and path.startswith("/api/"):
            record("blocked_readonly", method=request.method, path=path,
                   pid=_target(path), status=403, actor=_actor(),
                   remote=request.remote_addr or "-")
            log.warning("[readonly] 已拒绝写操作 %s %s（%s=1）",
                        request.method, path, READONLY_ENV)
            g.sta_audited = True     # 已在 before_request 记过，after_request 不再重复
            return jsonify({
                "ok": False,
                "error": "控制台处于只读模式（STA_CONSOLE_READONLY=1），已拒绝该操作。"
                         "如确需执行，请去掉该环境变量后重启控制台。",
            }), 403
        return None

    @app.after_request
    def _audit_mutation(resp: Any) -> Any:
        path = request.path or "/"
        if (request.method in _MUTATING and path.startswith("/api/")
                and not getattr(g, "sta_audited", False)):
            record("api_mutation", method=request.method, path=path,
                   pid=_target(path), status=resp.status_code,
                   actor=_actor(), remote=request.remote_addr or "-")
        return resp
