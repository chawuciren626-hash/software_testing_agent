"""控制台鉴权测试（默认关闭、配置 token 才生效）。

关注三件事：
1. **不配置时行为完全不变** —— 默认关闭是这块设计的核心承诺，不能被悄悄破坏。
2. 配置后**该挡的挡住**：`/api/*` 给 401 JSON（前端能处理），页面跳登录页。
3. **别把自己锁死**：登录页、健康检查必须免鉴权；`next` 不能变成开放重定向。
"""
from __future__ import annotations

import pytest

import app as web_app
import web_console.auth as auth

TOKEN = "s3cr3t-console-token"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    monkeypatch.delenv(auth.TOKEN_ENV, raising=False)
    return web_app.app.test_client()


@pytest.fixture
def authed_client(client, monkeypatch):
    monkeypatch.setenv(auth.TOKEN_ENV, TOKEN)
    return client


# --------------------------------------------------------------------------- #
# 默认关闭
# --------------------------------------------------------------------------- #
def test_disabled_by_default(client):
    assert auth.enabled() is False
    assert client.get("/api/projects").status_code == 200
    assert client.get("/").status_code == 200


def test_off_values_are_treated_as_disabled(monkeypatch):
    for v in ("", "off", "OFF", "false", "0", "none", "disabled"):
        monkeypatch.setenv(auth.TOKEN_ENV, v)
        assert auth.enabled() is False, f"{v!r} 应视为关闭"
    monkeypatch.setenv(auth.TOKEN_ENV, "  real-token  ")
    assert auth.enabled() is True and auth.token() == "real-token"  # 首尾空白要被剥掉


def test_login_page_redirects_home_when_disabled(client):
    """没开鉴权就别停在登录页 —— 否则用户会以为要登录却无从下手。"""
    r = client.get("/login")
    assert r.status_code == 302 and r.headers["Location"].endswith("/")


def test_auth_status_reports_disabled(client):
    d = client.get("/api/auth/status").get_json()
    assert d == {"ok": True, "enabled": False, "authed": False, "readonly": False}


# --------------------------------------------------------------------------- #
# 开启后的拦截
# --------------------------------------------------------------------------- #
def test_api_returns_401_json_when_not_logged_in(authed_client):
    r = authed_client.get("/api/projects")
    assert r.status_code == 401
    assert r.is_json and r.get_json()["ok"] is False     # 前端据此跳登录
    assert "未登录" in r.get_json()["error"]


def test_page_redirects_to_login_with_next(authed_client):
    r = authed_client.get("/")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_health_and_login_stay_open(authed_client):
    """健康检查若也要登录，监控会把"整个服务挂了"误报出来。"""
    assert authed_client.get("/healthz").status_code == 200
    assert authed_client.get("/login").status_code == 200
    assert authed_client.get("/api/auth/status").get_json()["enabled"] is True


def test_favicon_stays_open(authed_client):
    assert authed_client.get("/favicon.ico").status_code == 204


# --------------------------------------------------------------------------- #
# 登录 / 登出
# --------------------------------------------------------------------------- #
def test_wrong_token_rejected(authed_client):
    r = authed_client.post("/login", data={"token": "nope"})
    assert r.status_code == 401
    assert authed_client.get("/api/projects").status_code == 401   # 没有拿到会话
    assert authed_client.get("/api/auth/status").get_json()["authed"] is False


def test_correct_token_grants_session(authed_client):
    r = authed_client.post("/login", data={"token": TOKEN})
    assert r.status_code == 302
    assert authed_client.get("/api/auth/status").get_json()["authed"] is True
    assert authed_client.get("/api/projects").status_code == 200   # 全站放行


def test_logout_clears_session(authed_client):
    authed_client.post("/login", data={"token": TOKEN})
    assert authed_client.get("/api/projects").status_code == 200
    authed_client.get("/logout")
    assert authed_client.get("/api/projects").status_code == 401


def test_rotating_token_invalidates_old_session(authed_client, monkeypatch):
    """换 token 后旧会话必须失效 —— 否则"换了密码"形同虚设，登出旧设备做不到。"""
    authed_client.post("/login", data={"token": TOKEN})
    assert authed_client.get("/api/projects").status_code == 200
    monkeypatch.setenv(auth.TOKEN_ENV, "another-token")
    assert authed_client.get("/api/projects").status_code == 401


# --------------------------------------------------------------------------- #
# next 参数：不能变成开放重定向
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["//evil.example.com", "https://evil.example.com",
                                 "/login", "", None])
def test_next_is_clamped_to_site_root(authed_client, bad):
    data = {"token": TOKEN}
    if bad is not None:
        data["next"] = bad
    r = authed_client.post("/login", data=data)
    assert r.status_code == 302
    loc = r.headers["Location"]
    assert not loc.startswith("//") and "evil.example.com" not in loc


def test_next_keeps_legit_relative_path(authed_client):
    r = authed_client.post("/login", data={"token": TOKEN, "next": "/api/projects"})
    assert r.headers["Location"].endswith("/api/projects")


def test_login_page_never_echoes_the_token(authed_client):
    """token 不能出现在页面里 —— 那等于把钥匙贴在门上。"""
    html = authed_client.get("/login").get_data(as_text=True)
    assert TOKEN not in html
    assert "password" in html          # 输入框是密码类型


# --------------------------------------------------------------------------- #
# 前端接线
# --------------------------------------------------------------------------- #
def test_index_has_auth_hooks(client):
    html = client.get("/").get_data(as_text=True)
    assert "authLine" in html                 # 开启时显示"退出登录"
    assert "/api/auth/status" in html
    assert "r.status===401" in html           # 401 自动跳登录（防静默不更新）
