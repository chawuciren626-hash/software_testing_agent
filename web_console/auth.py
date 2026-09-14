"""控制台鉴权（**默认关闭**，配置 token 才生效）。

为什么这样设计
--------------
控制台默认只监听 `127.0.0.1`，本地单人使用，强制鉴权只会增加摩擦、降低可用性。
但一旦你把端口暴露到局域网（同事试用、内网演示、放在测试机上），
它会变成一个**任何人都能触发任意测试任务、还能读到 .env 里凭据状态**的入口。
所以做成"可开启"：不配 token → 行为与之前**完全一致**；配了 token → 全站需要登录。

开启方式
--------
在仓库根 `.env` 里加一行（该文件不入库）：

    STA_CONSOLE_TOKEN=<足够随机的长字符串>

重启控制台即可。想关掉就删掉这行（或写成 `off`）。

设计取舍
--------
- **token 只从环境变量读**（`.env` 由 `project_manager._load_dotenv()` 载入），
  不写进任何前端文件、不写日志、不在启动信息里回显。
- **每次请求实时读取**，不是启动时快照 —— 改完 `.env` 重启即可，也便于测试。
- 用 `hmac.compare_digest` 做定长比较，避免计时侧信道。
- 区分响应类型：`/api/*` 返回 401 JSON（前端好处理），页面跳转到 `/login`。
- 登录失败只提示"token 不正确"，不区分"没配"和"配错"。
  （配了 token 就不该再暗示"其实没开鉴权"。）
- 会话密钥由 token 派生（`sha256`），不额外落盘；换 token 会让旧会话自动失效，符合预期。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Any, Optional

from flask import Flask, jsonify, redirect, request, session, url_for

# 只读状态由 guard 提供（单向依赖：auth → guard；guard 不反向 import auth，
# 否则会循环导入。guard 的 _actor 直接读 session 键，不需要导入本模块）。
from web_console import guard

TOKEN_ENV = "STA_CONSOLE_TOKEN"

# 这些值视为"没开鉴权"，方便用 off/false 显式关闭
_OFF_VALUES = {"", "off", "0", "false", "no", "none", "disabled", "null"}

# 不需要登录即可访问。少一个就会把自己锁死：
#   /login      —— 登录页本身
#   /healthz    —— 探活（若也要登录，监控会把"服务挂了"误报出来）
#   /api/auth/status —— 登录页/前端靠它判断"要不要登录、是不是已经登录了"
_EXEMPT_PREFIXES = ("/login", "/logout", "/favicon.ico", "/healthz",
                    "/api/auth/status", "/static/")


def token() -> str:
    """当前配置的 token（每次调用实时读环境变量，避免启动快照不好测）。"""
    return (os.getenv(TOKEN_ENV) or "").strip()


def enabled() -> bool:
    """是否开启了鉴权。未配置或配置为 off/false 等 → 关闭。"""
    return token().lower() not in _OFF_VALUES


def _authed() -> bool:
    """会话里记录的是当前有效的 token 哈希（换 token 后旧会话自动失效）。"""
    return session.get("sta_authed") == _fingerprint()


def _fingerprint() -> str:
    return hashlib.sha256(token().encode("utf-8")).hexdigest()


def _safe_next(raw: Optional[str]) -> str:
    """只接受站内相对路径，避免 `next=//evil.com` 之类的开放重定向。"""
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    if raw.startswith("/login"):
        return "/"
    return raw


def _is_exempt(path: str) -> bool:
    return any(path.startswith(p) for p in _EXEMPT_PREFIXES)


def _wants_json(path: str) -> bool:
    """`/api/*` 用 401 JSON（前端 fetch 好处理）；其余走跳转（浏览器直接打开更自然）。"""
    return path.startswith("/api/")


_LOGIN_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>登录 · 测试智能体控制台</title>
<style>
  :root{color-scheme:dark}
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:#0b1020;color:#e6ecff;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
  .box{width:360px;background:#141a2e;border:1px solid #26304d;border-radius:14px;padding:28px 26px;
       box-shadow:0 20px 50px rgba(0,0,0,.45)}
  h1{margin:0 0 4px;font-size:17px}
  p.sub{margin:0 0 20px;color:#8d9ab8;font-size:12.5px}
  label{display:block;font-size:12px;color:#aeb9cc;margin-bottom:6px}
  input{width:100%;padding:10px 12px;border-radius:9px;border:1px solid #2c3757;background:#0f1526;
        color:#e6ecff;font:inherit;outline:none}
  input:focus{border-color:#6366f1}
  button{width:100%;margin-top:16px;padding:10px;border:0;border-radius:9px;cursor:pointer;
         background:#6366f1;color:#fff;font:inherit;font-weight:600}
  button:hover{background:#5457e5}
  .err{margin:14px 0 0;padding:9px 11px;border-radius:8px;background:rgba(239,68,68,.13);
       border:1px solid rgba(239,68,68,.35);color:#fca5a5;font-size:12.5px}
  .hint{margin:18px 0 0;padding-top:14px;border-top:1px solid #26304d;color:#7c8aa8;font-size:12px}
  code{background:#0f1526;padding:1px 5px;border-radius:4px;color:#a5b4fc}
</style></head><body>
<form class="box" method="post" action="/login">
  <h1>软件测试智能体 · 控制台</h1>
  <p class="sub">该控制台已开启访问鉴权</p>
  <label for="tok">访问 Token</label>
  <input id="tok" name="token" type="password" autocomplete="current-password"
         placeholder="填写 STA_CONSOLE_TOKEN 的值" autofocus>
  <button type="submit">进入控制台</button>
  {error}
  <div class="hint">Token 在仓库根 <code>.env</code> 的 <code>STA_CONSOLE_TOKEN</code> 中配置；
    不想用鉴权就删掉该行并重启。</div>
</form></body></html>"""


def install(app: Flask) -> None:
    """把鉴权挂到 Flask 应用上（未配置 token 时几乎无副作用）。"""
    # 会话密钥由 token 派生：不落盘、换 token 即让旧会话失效。
    # 未开启鉴权时用一个进程内随机值（此时根本不用 session）。
    app.secret_key = (hashlib.sha256(f"sta-console::{token()}".encode("utf-8")).hexdigest()
                      if enabled() else secrets.token_hex(32))
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_NAME="sta_console",
    )

    @app.before_request
    def _guard() -> Any:
        if not enabled():
            return None
        path = request.path or "/"
        if _is_exempt(path) or _authed():
            return None
        if _wants_json(path):
            return jsonify({"ok": False, "error": "未登录或登录已失效，请重新登录"}), 401
        return redirect(url_for("login", next=path))

    @app.get("/login")
    def login() -> Any:
        if not enabled():
            return redirect("/")      # 没开鉴权就别停在登录页
        if _authed():
            return redirect(_safe_next(request.args.get("next")))
        return _LOGIN_HTML.replace("{error}", "")

    @app.post("/login")
    def login_post() -> Any:
        if not enabled():
            return redirect("/")
        # compare_digest：定长比较，避免按字符逐位比较泄露信息
        ok = hmac.compare_digest(request.form.get("token", ""), token())
        if not ok:
            return _LOGIN_HTML.replace(
                "{error}", '<p class="err">Token 不正确，请检查 <code>.env</code> 里的配置。</p>'), 401
        session["sta_authed"] = _fingerprint()
        return redirect(_safe_next(request.form.get("next") or request.args.get("next")))

    @app.get("/logout")
    def logout() -> Any:
        session.clear()
        return redirect(url_for("login"))

    @app.get("/healthz")
    def healthz() -> Any:
        """健康检查不鉴权：否则监控/探活会因为"没登录"而误判服务挂了。"""
        return jsonify({"ok": True, "auth_enabled": enabled()})

    @app.get("/api/auth/status")
    def auth_status() -> Any:
        """前端据此决定是否显示「退出登录」与只读横幅。需免鉴权，否则登录页拿不到状态。"""
        return jsonify({"ok": True, "enabled": enabled(), "authed": _authed(),
                        "readonly": guard.readonly()})
