"""mall-admin 接口并发压测骨架（Locust）。

运行：
    pip install locust
    locust -f extensions/perf_security/locustfile_api.py --host http://localhost:8080 --users 20 --spawn-rate 5 --run-time 1m

说明：
- 默认管理员凭据来自环境变量 APP_USERNAME / APP_PASSWORD。
- 仅做只读/轻量写探测，避免对数据造成破坏；生产压测请改用独立环境。
"""
import os
import random

from locust import HttpUser, task, between


USER = os.getenv("APP_USERNAME", "admin")
PASS = os.getenv("APP_PASSWORD", "changeme")


class MallAdminUser(HttpUser):
    wait_time = between(1, 3)
    token = None

    def on_start(self):
        # 登录拿 token（mall-admin: data 为 token 字符串）
        r = self.client.post(
            "/admin/login",
            json={"username": USER, "password": PASS},
            catch_response=True,
        )
        if r.status_code == 200:
            try:
                self.token = r.json().get("data")
            except Exception:
                self.token = None

    @task(3)
    def login(self):
        self.client.post("/admin/login", json={"username": USER, "password": PASS})

    @task(2)
    def info(self):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        self.client.get("/admin/info", headers=headers, catch_response=True)

    @task(1)
    def register_unique(self):
        # 用随机用户名注册，避免冲突（压测用，谨慎用于生产数据）
        suffix = random.randint(100000, 999999)
        self.client.post(
            "/admin/register",
            json={"username": f"perf_{suffix}", "password": PASS},
            catch_response=True,
        )
