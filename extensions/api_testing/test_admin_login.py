"""管理员登录接口用例（mall-admin: /admin/login）。

说明：
- 默认管理员凭据来自环境变量 APP_USERNAME / APP_PASSWORD（缺省 admin / macro123）。
- mall-admin 登录真实契约（已实测）：HTTP 状态码恒为 200，成败写在响应体：
    成功 {"code":200,"message":"操作成功","data":{"tokenHead":"Bearer ","token":"..."}}
    失败 {"code":500,"message":"密码不正确","data":null}
  因此断言必须校验 code 与 data.token——只看状态码会产生"密码错了也通过"的假绿。
- 服务不可达时由 conftest.require_server 自动跳过。
"""
import os

import pytest

ADMIN_USER = os.getenv("APP_USERNAME", "admin")
ADMIN_PASS = os.getenv("APP_PASSWORD", "macro123")


@pytest.mark.smoke
def test_login_success_returns_token(session, base_url):
    """登录成功应返回 200 且 data 含 token。"""
    r = session.post(
        f"{base_url}/admin/login",
        json={"username": ADMIN_USER, "password": ADMIN_PASS},
        timeout=5,
    )
    assert r.status_code == 200, f"期望 200，实际 {r.status_code}: {r.text[:200]}"
    body = r.json()
    # mall-admin: code==200 为成功；data 是含 token 的对象
    assert body.get("code") in (200, 0, "200", "0"), f"业务码异常: {body}"
    assert body.get("data"), f"登录成功但未返回 token: {body}"


@pytest.mark.negative
def test_login_wrong_password_rejected(session, base_url):
    """错误密码应被拒绝（非 200 / 业务码非成功）。"""
    r = session.post(
        f"{base_url}/admin/login",
        json={"username": ADMIN_USER, "password": "wrong-password-12345"},
        timeout=5,
    )
    assert r.status_code in (200, 400, 401), f"非预期状态码 {r.status_code}"
    body = r.json()
    code = body.get("code")
    # 失败：业务码非成功，或 HTTP 401
    assert (r.status_code != 200) or (code not in (200, 0, "200", "0")), \
        f"错误密码竟登录成功: {body}"


@pytest.mark.negative
@pytest.mark.parametrize("payload", [
    {"username": ADMIN_USER},                       # 缺密码
    {"password": ADMIN_PASS},                       # 缺用户名
    {},                                             # 全缺
])
def test_login_missing_params(session, base_url, payload):
    """缺少必填参数应被拒绝（参数校验）。"""
    r = session.post(f"{base_url}/admin/login", json=payload, timeout=5)
    assert r.status_code in (200, 400, 401, 422), f"非预期状态码 {r.status_code}"
    if r.status_code == 200:
        body = r.json()
        assert body.get("code") not in (200, 0, "200", "0"), \
            f"缺参却登录成功: {body}"
