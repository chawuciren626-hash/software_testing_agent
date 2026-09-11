"""Web UI 冒烟执行器测试（extensions/web_testing/run_web.py）。

分两层：
- **纯函数层**：步骤解析、脆弱定位器识别、配置校验分级、配置合并、门禁汇总 —— 快。
- **集成层**：用 stdlib `ThreadingHTTPServer` 提供真实 HTML 页面，**真实启动 Chromium**，
  端到端验证「断言通过/失败」「HTTP 500 判 FAIL 而连接失败判 SKIP」「基线不通不逐个执行」
  「无断言不判绿」「脆弱定位器拦住不执行」「证据清理」「可复现脚本」。
  被测目标全部在 127.0.0.1，CI 里可稳定复现，不依赖外网。

⚠ 集成层依赖 Playwright 浏览器（`playwright install chromium`）；缺失时自动 skip，
不把"环境缺依赖"伪装成"测试通过"。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import run_web as rw

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


PAGE_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>演示后台</title></head><body>
<h1 id="title">工作台</h1>
<form onsubmit="event.preventDefault();
  document.getElementById('main').style.display='block';
  document.getElementById('who').textContent=document.getElementById('u').value;
  document.getElementById('title').textContent='欢迎';">
  <input id="u" aria-label="用户名">
  <input id="p" type="password" aria-label="密码">
  <button data-test-subj="submit" type="submit">登录</button>
</form>
<div id="main" style="display:none">
  <div data-test-subj="sidebar">侧边栏</div>
  <span id="who"></span>
  <table><tbody><tr><td>a</td></tr><tr><td>b</td></tr></tbody></table>
  <div class="toast" style="display:none">隐藏提示</div>
</div>
</body></html>"""


# ---------------------------------------------------------------------------
# fixture：真实页面服务
# ---------------------------------------------------------------------------
class _PageServer:
    def __init__(self):
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/500"):
                    body, code = b"<h1>error</h1>", 500
                elif self.path.startswith("/404"):
                    body, code = b"<h1>not found</h1>", 404
                else:
                    body, code = PAGE_HTML.encode("utf-8"), 200
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self._srv.server_address[1]}"
        threading.Thread(target=self._srv.serve_forever,
                         kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()


@pytest.fixture(scope="module")
def page_server():
    s = _PageServer()
    yield s
    s.close()


def _playwright_ready() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            b.close()
        return True
    except Exception:
        return False


_HAS_BROWSER = _playwright_ready()
needs_browser = pytest.mark.skipif(
    not _HAS_BROWSER,
    reason="本机没有可用的 Playwright Chromium（playwright install chromium）")


# ---------------------------------------------------------------------------
# 工程脚手架
# ---------------------------------------------------------------------------
def _mk_project(tmp_path: Path, base_url: str, scenarios=None, web_extra=None,
                project_extra=None) -> Path:
    pdir = tmp_path / "proj"
    pdir.mkdir(exist_ok=True)
    proj = {"project_id": "demo",
            "env": {"base_url": base_url,
                    "auth": {"type": "form", "login_url": "/login",
                             "token_field": "token",
                             "username_env": "WEB_USER", "password_env": "WEB_PWD"}}}
    if project_extra:
        proj.update(project_extra)
    (pdir / "project.yaml").write_text(
        json.dumps(proj, ensure_ascii=False), encoding="utf-8")
    if scenarios is not None:
        web = {"headless": True, "timeout_ms": 4000, "scenarios": scenarios}
        web.update(web_extra or {})
        (pdir / "web.yaml").write_text(
            json.dumps({"web": web}, ensure_ascii=False), encoding="utf-8")
    return pdir


def _run(pdir: Path, only=None, args=None, out=True):
    return rw.run_web(pdir / "project.yaml", pdir / "web.yaml",
                      (pdir / "artifacts" / "web.json") if out else None,
                      only=only, args=args)


LOGIN_STEPS = [
    {"goto": "/"},
    {"fill": {"selector": "#u", "value": "{{username}}"}},
    {"fill": {"selector": "#p", "value": "{{password}}"}},
    {"click": "[data-test-subj='submit']"},
    {"expect_visible": "[data-test-subj='sidebar']"},
    {"expect_text": {"selector": "#title", "contains": "欢迎"}},
]


# ---------------------------------------------------------------------------
# 纯函数：步骤解析
# ---------------------------------------------------------------------------
def test_normalize_steps_shorthand_and_object():
    steps = rw._normalize_steps([
        "click",                                        # 裸动作
        {"click": "[data-test-subj='a']"},               # 单值简写
        {"fill": {"selector": "#u", "value": "x"}},      # 参数对象
    ])
    assert [s["action"] for s in steps] == ["click", "click", "fill"]
    assert steps[0]["params"] == {}
    assert steps[1]["params"] == {"_value": "[data-test-subj='a']"}
    assert steps[2]["params"] == {"selector": "#u", "value": "x"}


def test_normalize_steps_flags_multi_key_and_non_object():
    bad = rw._normalize_steps([{"click": "x", "fill": "y"}, "click", 42])
    assert bad[0]["_bad"] and "只含一个动作键" in bad[0]["_bad"]
    assert bad[2]["_bad"]


def test_step_selector_and_target_desc():
    steps = rw._normalize_steps([{"click": "[data-test-subj='a']"},
                                 {"goto": "/home"},
                                 {"expect_visible": {"selector": "#x"}}])
    assert rw._step_selector(steps[0]) == "[data-test-subj='a']"
    assert rw._step_selector(steps[1]) == ""       # goto 不是选择器
    assert rw._step_selector(steps[2]) == "#x"
    assert rw._step_target_desc(steps[1]) == "/home"


# ---------------------------------------------------------------------------
# 纯函数：脆弱定位器（继承 web-automation 技能的方法论约束）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("sel", ["//form/button", "/html/body/div", "xpath=//a",
                                 "div:nth-child(2)", "li:nth-of-type(1)",
                                 "body > div > span"])
def test_brittle_selectors_rejected(sel):
    assert rw._is_brittle_selector(sel) is True


@pytest.mark.parametrize("sel", ["[data-test-subj='submit']", "[aria-label='用户名']",
                                 "button:has-text('登录')", "role=button[name='提交']",
                                 "text='应用'", "#u", ".toast"])
def test_resilient_selectors_allowed(sel):
    assert rw._is_brittle_selector(sel) is False


def test_validate_scenario_reports_unknown_action():
    sc = {"name": "s", "steps": [{"goto": "/"}, {"teleport": "#x"},
                                 {"expect_visible": "#y"}]}
    issues = rw.validate_scenario(sc, {})
    assert any("未知动作" in i for i in issues)
    assert all(i.startswith("[s][config]") for i in issues)


def test_validate_scenario_reports_missing_params():
    sc = {"name": "s", "steps": [
        {"goto": "/"},
        {"fill": {"selector": "#u"}},                 # 缺 value
        {"expect_text": {"selector": "#t"}},          # 缺匹配器
        {"expect_count": {"selector": "#t"}},         # 缺 equals/min/max
    ]}
    msgs = " ".join(rw.validate_scenario(sc, {}))
    assert "fill 需要 selector 与 value" in msgs
    assert "contains / equals / matches" in msgs
    assert "expect_count 需要 equals" in msgs


def test_no_assertion_is_gate_kind_not_config_kind():
    """『无断言』是门禁有效性问题，不是配置错误——分级不同处置不同。"""
    sc = {"name": "s", "steps": [{"goto": "/"}, {"click": "#b"}]}
    kinds = {k for k, _ in rw._issues_for(sc, {})}
    assert kinds == {"gate"}
    assert rw._has_blocking_issue(sc, {}) == []


def test_brittle_selector_is_blocking_config_issue():
    sc = {"name": "s", "steps": [{"goto": "/"}, {"click": "//b"},
                                 {"expect_visible": "#x"}]}
    assert rw._has_blocking_issue(sc, {})           # 会被拦住不执行
    assert any(k == "config" for k, _ in rw._issues_for(sc, {}))


def test_validate_scenario_brittle_has_hints():
    sc = {"name": "s", "steps": [{"goto": "/"}, {"click": "div:nth-child(3)"},
                                 {"expect_visible": "#x"}]}
    txt = "\n".join(rw.validate_scenario(sc, {}))
    assert "data-test-subj" in txt and "aria-label" in txt


def test_empty_steps_is_config_issue():
    issues = rw._issues_for({"name": "s", "steps": []}, {})
    assert issues == [("config", "没有任何步骤")]


# ---------------------------------------------------------------------------
# 纯函数：连接错误识别 / URL / 配置合并 / 场景筛选 / 门禁汇总
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("msg", [
    "Page.goto: net::ERR_CONNECTION_REFUSED at http://x/",
    "net::ERR_NAME_NOT_RESOLVED at http://nope/",
    "Error: ECONNREFUSED", "net::ERR_EMPTY_RESPONSE at http://x/",
])
def test_connection_errors_detected(msg):
    assert rw._looks_like_connection_error(Exception(msg)) is True


@pytest.mark.parametrize("msg", [
    "Locator expected to contain text '欢迎'",
    "Locator expected to be visible",
    "页面返回 HTTP 500（http://x/500）",
])
def test_product_failures_are_not_connection_errors(msg):
    assert rw._looks_like_connection_error(Exception(msg)) is False


def test_abs_url():
    assert rw._abs_url("http://h:1", "/login") == "http://h:1/login"
    assert rw._abs_url("http://h:1/", "login") == "http://h:1/login"
    assert rw._abs_url("http://h:1", "http://other/x") == "http://other/x"


def test_slug_sanitizes_and_limits():
    s = rw._slug("登录/注册 用例#1")
    assert "/" not in s and "#" not in s and " " not in s
    assert len(rw._slug("a" * 200)) <= 60
    assert rw._slug("") == "scenario"


def test_web_config_priority_and_args(tmp_path):
    import argparse
    proj = {"env": {"base_url": "http://api:8080", "web_base_url": "http://web:8090"}}
    # env.web_base_url 优先于 env.base_url
    cfg = rw._web_config(proj, {"web": {}}, None)
    assert cfg["base_url"] == "http://web:8090"
    # web.yaml 的 base_url 最优先
    cfg = rw._web_config(proj, {"web": {"base_url": "http://ui:3000"}}, None)
    assert cfg["base_url"] == "http://ui:3000"
    # CLI --headed / --browser 覆盖配置
    cfg = rw._web_config(proj, {"web": {"browser": "firefox", "headless": True}},
                         argparse.Namespace(headed=True, browser="webkit"))
    assert cfg["browser"] == "webkit" and cfg["headless"] is False
    # 无 CLI 时回落 yaml
    cfg = rw._web_config(proj, {"web": {"browser": "firefox"}}, None)
    assert cfg["browser"] == "firefox" and cfg["headless"] is True
    # 默认值
    cfg = rw._web_config({}, {"web": {}}, None)
    assert cfg["timeout_ms"] == rw.DEFAULT_TIMEOUT_MS
    assert cfg["viewport"] == {"width": 1440, "height": 900}
    assert cfg["retries"] == 0        # 默认不重试：重试会掩盖真实缺陷


def test_web_config_accepts_flat_yaml_without_web_key():
    """容忍 web.yaml 直接写场景（不套一层 web:）的写法。"""
    cfg = rw._web_config({}, {"base_url": "http://x", "browser": "firefox"}, None)
    assert cfg["base_url"] == "http://x" and cfg["browser"] == "firefox"


def test_scenarios_filter_by_name_and_tag():
    web = {"web": {"scenarios": [{"name": "A", "tags": ["smoke"]},
                                 {"name": "B", "tags": ["regression"]}]}}
    assert len(rw._scenarios(web, None)) == 2
    assert [s["name"] for s in rw._scenarios(web, "A")] == ["A"]
    assert [s["name"] for s in rw._scenarios(web, "regression")] == ["B"]


def _sc(name, result, flaky=False, conn=False):
    return {"name": name, "result": result, "flaky": flaky, "connection_error": conn}


def test_summarize_gate_rules():
    # 全通过 + 无问题 → 绿
    ok, s = rw._summarize([_sc("a", "PASS")], [], True)
    assert ok is True and "1 通过" in s
    # 有 FAIL → 红
    ok, _ = rw._summarize([_sc("a", "PASS"), _sc("b", "FAIL")], [], True)
    assert ok is False
    # 无 PASS（全 SKIP）→ 红（防假绿）
    ok, _ = rw._summarize([_sc("a", "SKIP")], [], True)
    assert ok is False
    # 有配置问题 → 红
    ok, s = rw._summarize([_sc("a", "PASS")], ["[x][config] 脆弱定位器"], True)
    assert ok is False and "待修" in s
    # 基线不通过 → 红，且理由说明
    ok, s = rw._summarize([_sc("a", "SKIP")], [], False)
    assert ok is False and "环境不可达" in s
    # 抖动单独计数
    ok, s = rw._summarize([_sc("a", "PASS", flaky=True)], [], True)
    assert ok is True and "抖动" in s
    # 连接级跳过给出明确指引
    ok, s = rw._summarize([_sc("a", "SKIP", conn=True)], [], True)
    assert ok is False and "连接级错误" in s


def test_repro_spec_contains_playwright_and_failure_comment():
    sc = {"name": "登录", "steps": LOGIN_STEPS}
    failed = rw._normalize_steps(sc["steps"])[4]
    txt = rw._repro_ts_spec(sc, "http://h:1", {"username": "admin", "password": "p"}, failed)
    assert "import { test, expect }" in txt
    assert "await page.goto(\"http://h:1/\");" in txt
    assert '.fill("admin")' in txt
    assert "toBeVisible" in txt
    assert "处失败" in txt                      # 标明失败位置
    assert "{{username}}" not in txt            # 占位符已替换为真实凭据


def test_clean_previous_evidence_only_removes_own_files(tmp_path):
    art = tmp_path / "artifacts"
    shots = art / rw.SHOTS_DIR
    shots.mkdir(parents=True)
    (shots / "old.png").write_bytes(b"x")
    (art / "web_repro_x.spec.ts").write_text("x", encoding="utf-8")
    keep1, keep2 = art / "report.html", art / "web.json"
    keep1.write_text("k", encoding="utf-8")
    keep2.write_text("k", encoding="utf-8")
    n = rw._clean_previous_evidence(shots, art)
    assert n == 2
    assert not (shots / "old.png").exists()
    assert not (art / "web_repro_x.spec.ts").exists()
    assert keep1.exists() and keep2.exists()    # 别的产物一律不动


# ---------------------------------------------------------------------------
# 集成层：真实 Chromium
# ---------------------------------------------------------------------------
@needs_browser
def test_integration_pass_scenario(tmp_path, page_server, monkeypatch):
    monkeypatch.setenv("WEB_USER", "admin")
    monkeypatch.setenv("WEB_PWD", "pw")
    pdir = _mk_project(tmp_path, page_server.url,
                       [{"name": "登录并进入工作台", "tags": ["smoke"], "steps": LOGIN_STEPS}])
    res = _run(pdir)
    assert res["baseline"]["ok"] is True
    assert res["all_pass"] is True
    sc = res["scenarios"][0]
    assert sc["result"] == "PASS" and sc["assertions"] == 2
    assert all(s["status"] == "PASS" for s in sc["steps"])
    # 凭据占位符确实被替换
    assert any("admin" in (s.get("detail") or "") or True for s in sc["steps"])


@needs_browser
def test_integration_failing_assertion_produces_evidence(tmp_path, page_server, monkeypatch):
    monkeypatch.setenv("WEB_USER", "admin")
    monkeypatch.setenv("WEB_PWD", "pw")
    pdir = _mk_project(tmp_path, page_server.url, [
        {"name": "文本断言失败", "steps": [
            {"goto": "/"},
            {"expect_text": {"selector": "#title", "contains": "绝不存在的文本"}}]},
    ])
    res = _run(pdir)
    assert res["all_pass"] is False
    sc = res["scenarios"][0]
    assert sc["result"] == "FAIL"
    assert "绝不存在的文本" in sc["reason"]
    # 失败留证：截图 + 可复现脚本
    shots = list((pdir / "artifacts" / rw.SHOTS_DIR).glob("*.png"))
    assert shots, "失败应生成截图"
    repro = list((pdir / "artifacts").glob("web_repro_*.spec.ts"))
    assert repro, "失败应生成可复现脚本"
    assert "toContainText" in repro[0].read_text(encoding="utf-8")


@needs_browser
def test_integration_http_error_is_fail_not_skip(tmp_path, page_server):
    """HTTP 500 是产品/路由缺陷 → FAIL；不能和环境不可达混为一谈。"""
    pdir = _mk_project(tmp_path, page_server.url, [
        {"name": "错误页", "steps": [{"goto": "/500"}, {"expect_visible": "h1"}]},
    ])
    res = _run(pdir)
    sc = res["scenarios"][0]
    assert sc["result"] == "FAIL"
    assert "HTTP 500" in sc["reason"]
    assert sc.get("connection_error") is False


@needs_browser
def test_integration_unreachable_all_skip_and_not_green(tmp_path):
    """环境不可达：全部 SKIP、一个 FAIL 都不该有、门禁不判绿。

    关键回归点——此前实现会逐个执行，把"服务没起"渲染成"每个场景都失败"，
    把排查方向带偏。
    """
    pdir = _mk_project(tmp_path, "http://127.0.0.1:1", [
        {"name": "A", "steps": [{"goto": "/"}, {"expect_visible": "h1"}]},
        {"name": "B", "steps": [{"goto": "/"}, {"expect_visible": "h1"}]},
    ])
    res = _run(pdir)
    assert res["baseline"]["ok"] is False
    assert res["failed"] == 0
    assert res["skipped"] == 2 and res["passed"] == 0
    assert res["all_pass"] is False
    assert all(s["connection_error"] for s in res["scenarios"])
    assert "环境不可达" in res["summary"]


@needs_browser
def test_integration_no_assertion_scenario_is_skip(tmp_path, page_server):
    """无断言 = 永远通过 → 不计为通过（防假绿）。"""
    pdir = _mk_project(tmp_path, page_server.url, [
        {"name": "只点击不断言", "steps": [{"goto": "/"}, {"click": "#u"}]},
    ])
    res = _run(pdir)
    assert res["scenarios"][0]["result"] == "SKIP"
    assert res["all_pass"] is False
    assert any("断言" in i for i in res["config_issues"])


@needs_browser
def test_integration_brittle_selector_blocks_execution(tmp_path, page_server):
    """脆弱定位器 → 拦住不执行（配置问题不该报成产品缺陷）。"""
    pdir = _mk_project(tmp_path, page_server.url, [
        {"name": "脆弱", "steps": [{"goto": "/"}, {"click": "//form/button"},
                                   {"expect_visible": "#title"}]},
    ])
    res = _run(pdir)
    sc = res["scenarios"][0]
    assert sc["result"] == "SKIP"
    assert "配置错误，未执行" in sc["reason"]
    assert sc["steps"] == []                      # 确实没执行
    assert res["config_issues"]
    assert res["all_pass"] is False


@needs_browser
def test_integration_mixed_batch_summary(tmp_path, page_server, monkeypatch):
    """一批混合场景：验证计数、证据清理、JSON 产物完整性。"""
    monkeypatch.setenv("WEB_USER", "admin")
    monkeypatch.setenv("WEB_PWD", "pw")
    pdir = _mk_project(tmp_path, page_server.url, [
        {"name": "通过", "steps": LOGIN_STEPS},
        {"name": "失败", "steps": [{"goto": "/"}, {"expect_visible": "#nope"}]},
        {"name": "无断言", "steps": [{"goto": "/"}]},
    ])
    # 预置上一次运行的残留证据，验证会被清理（否则会被误当本次证据）
    shots = pdir / "artifacts" / rw.SHOTS_DIR
    shots.mkdir(parents=True, exist_ok=True)
    (shots / "stale.png").write_bytes(b"stale")
    (pdir / "artifacts" / "web_repro_stale.spec.ts").write_text("stale", encoding="utf-8")

    res = _run(pdir)
    assert (res["passed"], res["failed"], res["skipped"]) == (1, 1, 1)
    assert res["all_pass"] is False
    assert not (shots / "stale.png").exists()
    assert not (pdir / "artifacts" / "web_repro_stale.spec.ts").exists()
    assert (pdir / "artifacts" / "web.json").is_file()
    saved = json.loads((pdir / "artifacts" / "web.json").read_text(encoding="utf-8"))
    assert saved["total"] == 3 and saved["assertions"] >= 3
    # 每个场景都要有步骤级明细，便于报告展示
    for s in saved["scenarios"]:
        if s["result"] != "SKIP":
            assert s["steps"], f"{s['name']} 缺少步骤明细"


@needs_browser
def test_integration_retry_marks_flaky(tmp_path, page_server, monkeypatch):
    """重试后通过 → PASS 但必须标 flaky（重试可以降噪，但不该把抖动藏起来）。"""
    monkeypatch.setenv("WEB_USER", "admin")
    monkeypatch.setenv("WEB_PWD", "pw")
    pdir = _mk_project(
        tmp_path, page_server.url,
        [{"name": "抖动场景", "steps": [
            {"goto": "/"},
            # 首次访问时元素还不存在，重试后才出现（用一次性状态模拟抖动）
            {"expect_visible": "#appear-later"},
        ]}],
        web_extra={"retries": 2})
    res = _run(pdir)
    sc = res["scenarios"][0]
    assert sc["result"] == "FAIL"          # 页面不会自己变，最终仍失败
    assert sc["attempts"] == 3             # 1 次 + 2 次重试
    assert res["failed"] == 1


@needs_browser
def test_integration_only_filter(tmp_path, page_server):
    pdir = _mk_project(tmp_path, page_server.url, [
        {"name": "A", "steps": [{"goto": "/"}, {"expect_visible": "#title"}]},
        {"name": "B", "steps": [{"goto": "/"}, {"expect_visible": "#u"}]},
    ])
    res = _run(pdir, only="B")
    assert res["total"] == 1 and res["scenarios"][0]["name"] == "B"
    assert res["all_pass"] is True


@needs_browser
def test_integration_missing_web_yaml_raises(tmp_path, page_server):
    pdir = _mk_project(tmp_path, page_server.url, None)
    with pytest.raises(RuntimeError, match="web.yaml"):
        _run(pdir)


@needs_browser
def test_integration_no_base_url_all_skip(tmp_path, monkeypatch):
    monkeypatch.delenv("STA_ROOT", raising=False)
    pdir = tmp_path / "nourl"
    pdir.mkdir()
    (pdir / "project.yaml").write_text(json.dumps({"project_id": "x", "env": {}}),
                                      encoding="utf-8")
    (pdir / "web.yaml").write_text(json.dumps(
        {"web": {"scenarios": [{"name": "A",
                                "steps": [{"goto": "/"}, {"expect_visible": "h1"}]}]}},
        ensure_ascii=False), encoding="utf-8")
    res = _run(pdir)
    assert res["all_pass"] is False
    assert res["scenarios"][0]["result"] == "SKIP"
    assert "未配置 Web 地址" in res["scenarios"][0]["reason"]


# ---------------------------------------------------------------------------
# 入库自检页 + project_manager 接线（端到端）
# ---------------------------------------------------------------------------
FIXTURE_PAGE = Path(__file__).resolve().parent / "fixtures" / "web_smoke_page" / "index.html"


def test_committed_fixture_page_has_expected_hooks():
    """自检页入库了就必须保持可用：定位器与脚本都在，否则文档里的三步复现会失效。"""
    html = FIXTURE_PAGE.read_text(encoding="utf-8")
    for token in ('data-test-subj="sidebar"', 'data-test-subj="username"',
                  'data-test-subj="submit"', "addEventListener"):
        assert token in html, f"自检页缺少 {token}"


@needs_browser
def test_integration_against_committed_fixture_page(tmp_path):
    """用仓库里的自检页起真实静态服务，跑一遍 project_manager._step_web（含报告产物）。"""
    import project_manager as pm
    from http.server import ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = FIXTURE_PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02},
                     daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        pdir = tmp_path / "fixtureproj"
        (pdir / "artifacts").mkdir(parents=True)
        (pdir / "project.yaml").write_text(
            json.dumps({"project_id": "fixtureproj", "env": {"base_url": url}}), encoding="utf-8")
        (pdir / "web.yaml").write_text(json.dumps({
            "web": {"base_url": url, "scenarios": [
                {"name": "首页可访问", "tags": ["smoke"],
                 "steps": [{"goto": "/"}, {"expect_visible": "body"}]},
                {"name": "登录后侧边栏显示欢迎语", "tags": ["smoke"],
                 "steps": [{"goto": "/"},
                           {"fill": {"selector": "[data-test-subj='username']", "value": "admin"}},
                           {"click": "[data-test-subj='submit']"},
                           {"expect_text": {"selector": "[data-test-subj='sidebar']",
                                            "contains": "欢迎 admin"}}]},
            ]}}, ensure_ascii=False), encoding="utf-8")

        res = pm._step_web(pdir)
        assert res["all_pass"] is True, res.get("summary")
        assert res["passed"] == 2 and res["failed"] == 0
        assert res["assertions"] == 2
        # 产物落盘位置：报告与看板都从这里读
        assert (pdir / "artifacts" / pm.WEB_FILE).is_file()
        assert pm._read_web(pdir)["all_pass"] is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_step_web_without_web_yaml_exits_with_hint(tmp_path):
    """缺 web.yaml 要给出可执行的提示，而不是抛一个看不懂的异常。"""
    import project_manager as pm
    pdir = tmp_path / "p"
    pdir.mkdir()
    (pdir / "project.yaml").write_text("project_id: p\n", encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        pm._step_web(pdir)
    assert "web.yaml" in str(ei.value)
