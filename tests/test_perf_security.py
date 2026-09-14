"""④ 性能与安全冒烟执行器的单元测试。

策略：用 stdlib 起一个**桩 HTTP 服务**（两种人格：合规 / 不安全），
不依赖 mall-admin，也不依赖外网，保证 CI 里稳定可跑。

重点覆盖三类"会给出错误结论"的坑（本项目反复踩过）：
  1. 假绿：HTTP 200 但业务码是失败 → 必须算失败（只统计状态码会漏）；
  2. 假绿：环境不可达 → 必须 SKIP 且门禁不通过；
  3. 假红：目标地址错误/代理拦截 → 必须走基线跳过，而不是报出"未授权访问"这类假漏洞。
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import run_perf_security as ps

# 性能指标必须绕开代理：测试目标在 127.0.0.1，若被代理拦截会得到 502 而非真实响应
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")


# ---------------------------------------------------------------------------
# 桩服务
# ---------------------------------------------------------------------------
class _Stub:
    """可编程的桩服务：routes[(method, path)] = fn(body, headers) -> (status, payload)。"""

    def __init__(self, routes, headers=None):
        outer = self

        class H(BaseHTTPRequestHandler):
            def _handle(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw) if raw else {}
                except Exception:
                    body = {"__raw__": raw.decode("utf-8", "replace")}
                fn = routes.get((method, self.path.split("?")[0]))
                if fn is None:
                    status, payload = 404, {"code": 404, "message": "not found"}
                else:
                    status, payload = fn(body, dict(self.headers))
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json;charset=UTF-8")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_DELETE(self):
                self._handle("DELETE")

            def log_message(self, *a):
                pass

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self._srv.server_address[1]}"
        # poll_interval 调小：shutdown() 默认要等 0.5s，测试里每个用例都关一次服务，累计很可观
        self._t = threading.Thread(target=self._srv.serve_forever,
                                   kwargs={"poll_interval": 0.02}, daemon=True)
        self._t.start()

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()


_SAFE_HEADERS = {"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"}

_GOOD_USER = "gooduser"
_GOOD_PWD = "goodpwd"


def _good_login(body, headers):
    """合规登录：正确口令发 token；错误口令一律同一套回显（不给用户枚举留口子）。"""
    u = str(body.get("username") or "")
    p = str(body.get("password") or "")
    if u == _GOOD_USER and p == _GOOD_PWD:
        return 200, {"code": 200, "message": "操作成功",
                     "data": {"tokenHead": "Bearer ", "token": "TOK-123"}}
    if any(s in u or s in p for s in ("'", "--", "union", "#")):
        # 注入探针：与普通错误一致，不绕行、不回显数据库错误
        return 200, {"code": 404, "message": "用户名或密码错误", "data": None}
    return 200, {"code": 500, "message": "用户名或密码错误", "data": None}


def _good_routes():
    def secret(body, headers):
        if "Authorization" in headers:
            return 200, {"code": 200, "data": {"list": [{"id": 1}]}}
        return 200, {"code": 401, "message": "暂未登录或token已经过期", "data": None}

    return {
        ("POST", "/login"): _good_login,
        ("GET", "/api/secret"): secret,
        ("GET", "/health"): lambda b, h: (200, {"code": 200, "message": "ok"}),
        # 错误路径：干净的业务回显，不泄露堆栈
        ("GET", "/__not_exists_probe__"): lambda b, h: (404, {"code": 404, "message": "not found"}),
        ("DELETE", "/login"): lambda b, h: (405, {"code": 405, "message": "method not allowed"}),
    }


def _bad_routes():
    """不安全人格：任意口令发 token、受保护接口无鉴权、错误回显 Java 堆栈、无安全响应头。"""

    def leaky_login(body, headers):
        return 200, {"code": 200, "message": "ok",
                     "data": {"tokenHead": "Bearer ", "token": "TOK-ANY"}}

    def open_secret(body, headers):
        return 200, {"code": 200, "data": {"list": [{"id": 1}, {"id": 2}]}}

    def toast(body, headers):
        return 500, {"code": 500, "message": "internal error",
                     "data": "java.lang.NullPointerException\n\tat com.demo.Svc.run(Svc.java:42)"}

    return {
        ("POST", "/login"): leaky_login,
        ("GET", "/api/secret"): open_secret,
        ("GET", "/health"): lambda b, h: (200, {"code": 200, "message": "ok"}),
        ("GET", "/__not_exists_probe__"): toast,
        ("DELETE", "/login"): toast,
    }


@pytest.fixture
def good_server():
    s = _Stub(_good_routes(), _SAFE_HEADERS)
    yield s
    s.close()


@pytest.fixture
def bad_server():
    s = _Stub(_bad_routes())
    yield s
    s.close()


def _project(base_url, **extra):
    p = {
        "project_id": "stub",
        "env": {"base_url": base_url,
                "auth": {"type": "form", "login_url": "/login", "token_field": "token",
                         "username_env": "STUB_USER", "password_env": "STUB_PWD"}},
        # 压测规模调小，保证测试快速（同时验证 project.yaml 的配置确实被读取）
        "perf_security": {"perf": {"users": 2, "iterations": 2, "warmup": 0}},
    }
    p.update(extra)
    return p


def _regression(protected_path="/api/secret"):
    return {"core_business": [
        {"name": "健康检查", "type": "api_smoke", "method": "GET", "path": "/health",
         "expect_status": 200},
        {"name": "登录", "type": "api_smoke", "method": "POST", "path": "/login",
         "body": {"username": "{{username}}", "password": "{{password}}"},
         "expect_status": 200, "expect_json": {"code": 200, "data.token": "__not_null__"}},
        {"name": "受保护查询", "type": "api_smoke", "method": "GET", "path": protected_path,
         "auth": "required", "expect_status": 200, "expect_json": {"code": 200}},
        {"name": "注册(写操作)", "type": "api_smoke", "method": "POST", "path": "/register",
         "expect_status": 200},
    ]}


@pytest.fixture
def stub_auth(monkeypatch):
    monkeypatch.setenv("STUB_USER", _GOOD_USER)
    monkeypatch.setenv("STUB_PWD", _GOOD_PWD)
    return {"type": "form", "login_url": "/login", "token_field": "token",
            "username": _GOOD_USER, "password": _GOOD_PWD, "token": ""}


def _cfg(users=2, iterations=2, **thr):
    import argparse
    return ps._perf_config(_project("http://x"), argparse.Namespace(users=users, iterations=iterations)) \
        if not thr else {
            "users": users, "iterations": iterations, "warmup": 0, "timeout": 5,
            "thresholds": {"p95_ms": thr.get("p95_ms", 0), "p99_ms": thr.get("p99_ms", 0),
                           "max_error_rate": thr.get("max_error_rate", 0),
                           "min_rps": thr.get("min_rps", 0)},
        }


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------
def test_percentile_known_values():
    vals = [float(i) for i in range(1, 101)]     # 1..100
    assert ps._percentile(vals, 50) == pytest.approx(50.5)
    assert ps._percentile(vals, 95) == pytest.approx(95.05, abs=0.2)
    assert ps._percentile(vals, 99) == pytest.approx(99.01, abs=0.2)


def test_percentile_edge_cases():
    assert ps._percentile([], 95) == 0.0
    assert ps._percentile([7.0], 95) == 7.0


def test_business_ok_none_expect_code_means_http_only():
    """未声明期望业务码 → 不做业务码判定（mall-admin 的 GET / 返回 code=401 也不该被判失败）。"""
    assert ps._business_ok({"code": 401, "data": None}, None) is None


def test_business_ok_detects_business_failure():
    assert ps._business_ok({"code": 500}, 200) is False
    assert ps._business_ok({"code": 200}, 200) is True


def test_business_ok_tolerates_success_sentinels():
    """不同后端口径不一（0 也是成功码），不该因此误判为失败。"""
    assert ps._business_ok({"code": 0}, 200) is True
    assert ps._business_ok({"success": True}, 200) is True
    assert ps._business_ok({"status": "success"}, 200) is True


class _Resp:
    def __init__(self, status, payload, text=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload, ensure_ascii=False)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def test_looks_rejected_variants():
    assert ps._looks_rejected(_Resp(401, {"code": 401}), {"code": 401}) is True
    assert ps._looks_rejected(_Resp(200, {"code": 401, "data": None}), {"code": 401, "data": None}) is True
    assert ps._looks_rejected(_Resp(200, {"code": 200, "data": {"id": 1}}),
                              {"code": 200, "data": {"id": 1}}) is False


def test_looks_rejected_ignores_normal_payload_mentioning_token():
    """正常响应体里出现 token 字样不该被误判为"已拒绝"。"""
    payload = {"code": 200, "data": {"token": "abc", "list": [1]}}
    assert ps._looks_rejected(_Resp(200, payload), payload) is False


def test_has_token_and_auth_header_tokenhead():
    payload = {"code": 200, "data": {"tokenHead": "Bearer ", "token": "T1"}}
    assert ps._has_token(payload, "token") is True
    assert ps._auth_header(payload, "token") == "Bearer T1"
    assert ps._auth_header({"code": 200, "data": {"token": "T2"}}, "token") == "Bearer T2"
    assert ps._auth_header({"code": 500, "data": None}, "token") is None


@pytest.mark.parametrize("path,want", [
    ("/admin/list", False),
    ("/admin/login", False),
    ("/admin/info", False),
    ("/admin/register", True),
    ("/user/delete", True),
    ("/upload/file", True),
])
def test_is_write_path(path, want):
    assert ps._is_write_path(path) is want


@pytest.mark.parametrize("url,want", [
    ("http://localhost:8080", True),
    ("http://127.0.0.1:9000", True),
    ("http://192.168.1.10:8080", True),
    ("http://10.0.0.5", True),
    ("http://api.example.com", False),
])
def test_is_local_url(url, want):
    assert ps._is_local_url(url) is want


def test_session_disables_proxy_for_local_target(monkeypatch):
    """性能指标不能走代理，否则 p95/吞吐被代理污染。"""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:62873")
    assert ps._session("http://127.0.0.1:8080").trust_env is False
    assert ps._session("http://api.example.com").trust_env is True


# ---------------------------------------------------------------------------
# 目标派生
# ---------------------------------------------------------------------------
def test_derive_targets_from_regression_skips_writes_and_keeps_expect_code():
    targets, src = ps._derive_targets(_project("http://x"), _regression())
    paths = [t["path"] for t in targets]
    assert "/register" not in paths                 # 写操作默认不压，避免造脏数据
    assert "/health" in paths
    assert "regression.yaml" in src
    by_path = {t["path"]: t for t in targets}
    assert by_path["/login"]["expect_code"] == 200  # 继承 expect_json.code
    assert by_path["/health"]["expect_code"] is None  # 未声明业务码 → 只看 HTTP


def test_derive_targets_declared_wins_and_write_requires_flag():
    proj = _project("http://x", perf_security={
        "perf": {"targets": [
            {"name": "读", "method": "GET", "path": "/api/secret"},
            {"name": "写", "method": "POST", "path": "/register"},
            {"name": "显式允许的写", "method": "POST", "path": "/register", "write": True},
        ]}})
    targets, src = ps._derive_targets(proj, _regression())
    assert "project.yaml" in src
    names = [t["name"] for t in targets]
    assert "写" not in names and "显式允许的写" in names


def test_derive_targets_fallback_without_regression():
    targets, src = ps._derive_targets(_project("http://x"), None)
    assert targets and "兜底" in src


# ---------------------------------------------------------------------------
# 性能冒烟
# ---------------------------------------------------------------------------
def test_run_perf_counts_business_code_failure_as_error(good_server, stub_auth):
    """核心防假绿：HTTP 200 但业务码 500，必须算失败。"""
    proj = _project(good_server.url)
    auth = stub_auth
    header, diag = ps._obtain_auth_header(good_server.url, auth)
    assert diag["ok"] is True
    # /login 用错误凭据压不出业务失败，这里换成一个必然返回 code=500 的目标
    bad = {"core_business": [
        {"name": "业务失败", "type": "api_smoke", "method": "POST", "path": "/login",
         "body": {"username": "nobody", "password": "wrong"},
         "expect_status": 200, "expect_json": {"code": 200}},
    ]}
    res = ps.run_perf(proj, good_server.url, auth, header, bad, _cfg())
    t = res["targets"][0]
    assert t["requests"] == 4
    assert t["ok"] == 0 and t["failed"] == 4
    assert t["result"] == "FAIL"
    assert res["passed"] is False
    assert any("业务码" in k for k in t["fail_kinds"])


def test_run_perf_pass_and_metrics_shape(good_server, stub_auth):
    proj = _project(good_server.url)
    header, _ = ps._obtain_auth_header(good_server.url, stub_auth)
    res = ps.run_perf(proj, good_server.url, stub_auth, header, _regression(), _cfg(2, 3))
    assert res["passed"] is True
    ov = res["overall"]
    assert ov["samples"] == 6 * len(res["targets"])   # 2 并发 × 3 次 × 每个目标
    assert ov["error_rate"] == 0.0
    assert ov["p95_ms"] >= ov["p50_ms"] >= 0
    names = {t["path"] for t in res["targets"]}
    assert "/register" not in names


def test_run_perf_threshold_violation_marks_fail(good_server, stub_auth):
    """阈值必须真的拦住：把 p95 阈值设为 0.0001ms，任何真实请求都不可能达标。"""
    proj = _project(good_server.url)
    header, _ = ps._obtain_auth_header(good_server.url, stub_auth)
    res = ps.run_perf(proj, good_server.url, stub_auth, header, _regression(),
                      _cfg(1, 1, p95_ms=0.0001))
    assert res["passed"] is False
    assert any("P95" in f for r in res["targets"] for f in r["threshold_fails"])


def test_run_perf_unreachable_is_skip_and_not_green(stub_auth):
    """环境不可达 → SKIP 且不判绿（防 CI 假绿）。"""
    dead = "http://127.0.0.1:1"      # 保留端口，必然连不上
    one_target = {"core_business": [
        {"name": "健康检查", "type": "api_smoke", "method": "GET", "path": "/health",
         "expect_status": 200}]}
    # timeout 压到 1s：连不通时不要在保留端口上等满默认超时（否则单测白等十几秒）
    cfg = {"users": 1, "iterations": 1, "warmup": 0, "timeout": 1,
           "thresholds": {"p95_ms": 0, "p99_ms": 0, "max_error_rate": 0, "min_rps": 0}}
    res = ps.run_perf(_project(dead), dead, stub_auth, None, one_target, cfg)
    assert all(t["result"] == "SKIP" for t in res["targets"])
    assert res["passed"] is False and res["skipped"] is True


# ---------------------------------------------------------------------------
# 安全冒烟
# ---------------------------------------------------------------------------
def test_run_security_compliant_app_has_no_failure(good_server, stub_auth):
    proj = _project(good_server.url)
    res = ps.run_security(proj, good_server.url, stub_auth, _regression())
    assert res["failed"] == 0
    assert res["passed"] is True
    by_name = {c["name"]: c for c in res["checks"]}
    assert by_name["未授权访问受保护接口"]["status"] == "PASS"
    assert by_name["错误口令登录被拒绝"]["status"] == "PASS"
    assert by_name["SQL 注入探针未绕过认证"]["status"] == "PASS"
    assert by_name["错误响应不泄露堆栈信息"]["status"] == "PASS"


def test_run_security_detects_insecure_app(bad_server, stub_auth):
    """不安全人格必须被抓出来：无鉴权 / 任意口令发 token / 堆栈泄露 / 缺安全头。"""
    proj = _project(bad_server.url)
    res = ps.run_security(proj, bad_server.url, stub_auth, _regression())
    assert res["passed"] is False
    assert res["failed"] >= 3
    by_name = {c["name"]: c for c in res["checks"]}
    assert by_name["未授权访问受保护接口"]["status"] == "FAIL"
    assert by_name["错误口令登录被拒绝"]["status"] == "FAIL"
    assert by_name["SQL 注入探针未绕过认证"]["status"] == "FAIL"
    assert by_name["错误响应不泄露堆栈信息"]["status"] == "FAIL"
    assert by_name["安全响应头检查"]["status"] == "FAIL"


def test_user_enumeration_is_warn_not_fail(good_server, monkeypatch, stub_auth):
    """用户枚举是提示级（非阻塞），不能把门禁判死。"""
    def enum_login(body, headers):
        u = str(body.get("username") or "")
        if u == _GOOD_USER:
            return 200, {"code": 500, "message": "密码不正确", "data": None}
        return 200, {"code": 404, "message": "用户名或密码错误", "data": None}

    routes = _good_routes()
    routes[("POST", "/login")] = enum_login
    s = _Stub(routes, _SAFE_HEADERS)
    try:
        res = ps.run_security(_project(s.url), s.url, stub_auth, None)
        chk = {c["name"]: c for c in res["checks"]}["错误口令响应不泄露用户是否存在"]
        assert chk["status"] == "WARN"
        assert res["passed"] is True          # 提示不影响门禁
    finally:
        s.close()


# ---------------------------------------------------------------------------
# run_all：门禁语义
# ---------------------------------------------------------------------------
def _write_project(tmp_path, base_url):
    pdir = tmp_path / "stub"
    pdir.mkdir()
    proj = _project(base_url)
    (pdir / "project.yaml").write_text(json.dumps(proj), encoding="utf-8")
    (pdir / "regression.yaml").write_text(json.dumps(_regression()), encoding="utf-8")
    return pdir


def test_run_all_green_on_compliant_stub(tmp_path, good_server, stub_auth):
    pdir = _write_project(tmp_path, good_server.url)
    out = pdir / "artifacts" / "perf_security.json"
    res = ps.run_all(pdir / "project.yaml", pdir / "regression.yaml", out)
    assert res["all_pass"] is True
    assert res["baseline"]["ok"] is True
    assert out.is_file()
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["all_pass"] is True and saved["perf"]["enabled"] is True
    # 明细样本不进 JSON，保持产物精简
    assert all("_ms" not in t for t in saved["perf"]["targets"])


def test_run_all_baseline_failure_skips_instead_of_false_vulnerability(tmp_path, monkeypatch):
    """错误地址/凭据失效：全部 SKIP 且不得报出假漏洞，门禁不通过。"""
    monkeypatch.setenv("STUB_USER", "who")
    monkeypatch.setenv("STUB_PWD", "nope")
    pdir = _write_project(tmp_path, "http://127.0.0.1:1")
    res = ps.run_all(pdir / "project.yaml", pdir / "regression.yaml")
    assert res["all_pass"] is False
    assert res["baseline"]["ok"] is False
    assert res["perf"]["skipped"] is True
    assert all(c["status"] == "SKIP" for c in res["security"]["checks"])
    assert res["security"]["failed"] == 0     # 关键：没有假漏洞


def test_run_all_only_security_skips_perf(good_server, tmp_path, stub_auth):
    pdir = _write_project(tmp_path, good_server.url)
    res = ps.run_all(pdir / "project.yaml", pdir / "regression.yaml", only="security")
    assert res["perf"]["enabled"] is False
    assert res["security"]["enabled"] is True


def test_run_all_partial_rerun_keeps_previous_section(good_server, tmp_path, stub_auth):
    """只跑安全时，上一次的性能结果必须保留（与 regression 的 rerun_one 同一原则）。

    否则「只跑安全」会把刚测出的性能数据静默洗掉，门禁结论跟着失真。
    """
    pdir = _write_project(tmp_path, good_server.url)
    out = pdir / "artifacts" / "perf_security.json"
    full = ps.run_all(pdir / "project.yaml", pdir / "regression.yaml", out)
    assert full["perf"]["enabled"] is True

    again = ps.run_all(pdir / "project.yaml", pdir / "regression.yaml", out, only="security")
    assert again["perf"]["enabled"] is True                 # 沿用上次
    assert again["stale_sections"] == ["perf"]
    assert again["perf_from_previous"]                      # 标注来源时间，可追溯
    assert again["all_pass"] is True                        # 两侧合并判定
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["perf"]["enabled"] is True


def test_load_dotenv_does_not_override_existing(monkeypatch, tmp_path):
    """.env 载入用 setdefault 语义（已存在的不覆盖），并认 STA_ROOT。

    实现已收敛到 extensions/common/auth.load_dotenv（唯一定义处，见
    docs/HARNESS_ARCHITECTURE_REVIEW.md §4.1）；本用例守的是"语义"，
    不再绑定某个模块内的兜底函数名（ps._load_dotenv 即共享实现）。
    """
    (tmp_path / ".env").write_text("STUB_USER=from_env\nSTUB_PWD=pw\n", encoding="utf-8")
    monkeypatch.setenv("STA_ROOT", str(tmp_path))
    monkeypatch.setenv("STUB_USER", "preset")
    ps._load_dotenv()
    assert os.environ["STUB_USER"] == "preset"       # 已存在的不覆盖
    assert os.environ["STUB_PWD"] == "pw"            # 缺失的补上
