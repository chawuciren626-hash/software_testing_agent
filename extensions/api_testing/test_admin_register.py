"""管理员注册接口用例（mall-admin: /admin/register）。

设计要点：
- 使用 conftest.unique_suffix 生成动态唯一用户名，保证多次运行不冲突（数据隔离）。
- 覆盖：正常注册、重复用户名冲突、参数缺失。
- 服务不可达时由 require_server 自动跳过。

注：mall-admin 不同版本 register 契约可能不同（有的需要验证码/角色）。
下列断言做了宽松处理，请按真实返回收敛。
"""
import os

import pytest

ADMIN_PASS = os.getenv("APP_PASSWORD", "changeme")


@pytest.mark.smoke
def test_register_success(session, base_url, unique_suffix):
    """用唯一用户名注册应成功。"""
    username = f"qa_{unique_suffix}"
    r = session.post(
        f"{base_url}/admin/register",
        json={
            "username": username,
            "password": ADMIN_PASS,
            "email": f"{username}@example.com",
        },
        timeout=5,
    )
    assert r.status_code in (200, 201), f"期望 200/201，实际 {r.status_code}: {r.text[:200]}"
    body = r.json()
    assert body.get("code") in (200, 0, "200", "0"), f"业务码异常: {body}"


@pytest.mark.negative
def test_register_duplicate_username(session, base_url, unique_suffix):
    """重复用户名应被拒绝（先注册一次，再用同用户名注册）。"""
    username = f"dup_{unique_suffix}"
    first = session.post(
        f"{base_url}/admin/register",
        json={"username": username, "password": ADMIN_PASS},
        timeout=5,
    )
    if first.status_code not in (200, 201):
        pytest.skip(f"首次注册未成功（可能接口契约不同），跳过重复校验: {first.text[:120]}")

    second = session.post(
        f"{base_url}/admin/register",
        json={"username": username, "password": ADMIN_PASS},
        timeout=5,
    )
    assert second.status_code in (200, 400, 409), f"非预期状态码 {second.status_code}"
    if second.status_code == 200:
        body = second.json()
        assert body.get("code") not in (200, 0, "200", "0"), \
            f"重复用户名竟注册成功: {body}"


@pytest.mark.negative
def test_register_missing_username(session, base_url):
    """缺用户名应被拒绝。"""
    r = session.post(
        f"{base_url}/admin/register",
        json={"password": ADMIN_PASS},
        timeout=5,
    )
    assert r.status_code in (200, 400, 409, 422), f"非预期状态码 {r.status_code}"
    if r.status_code == 200:
        body = r.json()
        assert body.get("code") not in (200, 0, "200", "0"), f"缺用户名却注册成功: {body}"
