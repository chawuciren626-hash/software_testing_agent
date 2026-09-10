"""接口自动化测试公共夹具（pytest + requests）。

设计要点：
1. base_url 来自环境变量 BASE_URL，默认 http://localhost:8080（mall-admin）。
2. require_server：会话级自动夹具，探测被测服务是否可达（返回 <400）。
   不可达时整批用例 skip，避免 CI 红而不阻塞。
3. unique_suffix：动态唯一后缀，用于隔离 register/delete/update 等写操作用例的数据。
4. session：复用的 requests.Session，默认 JSON 头。
"""
import os
import random
import string

import pytest
import requests

BASE_URL = os.getenv("BASE_URL", "http://localhost:8080").rstrip("/")


@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL


@pytest.fixture(scope="session")
def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


def _server_ok(url: str) -> bool:
    try:
        r = requests.get(url, timeout=3)
        return r.status_code < 400
    except Exception:
        return False


@pytest.fixture(scope="session", autouse=True)
def require_server(base_url):
    """服务不可达时整批跳过（如 mall-admin 未启动）。"""
    if not _server_ok(base_url):
        pytest.skip(
            f"mall-admin 不可达（{base_url}），跳过接口用例；"
            f"启动后端（Spring Boot :8080）后重跑。"
        )


@pytest.fixture
def unique_suffix() -> str:
    """生成 8 位随机后缀，用于隔离写操作用例的数据。"""
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
