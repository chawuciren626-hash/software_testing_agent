"""Web 控制台冒烟测试（Flask 测试客户端，不启真实服务、不依赖被测后端）。"""
import os

import app as web_app
import generate_cases as gc

client = web_app.app.test_client()


def test_index_ok():
    r = client.get("/")
    assert r.status_code == 200


def test_llm_status_schema():
    r = client.get("/api/llm/status")
    assert r.status_code == 200
    data = r.get_json()
    assert isinstance(data, dict)
    assert "available" in data


def test_projects_api_returns_list():
    r = client.get("/api/projects")
    assert r.status_code == 200
    data = r.get_json()
    assert isinstance(data, dict)
    assert isinstance(data.get("projects"), list)


def _mk_tmp_project(tmp_path, pid="demo"):
    proj = tmp_path / pid
    proj.mkdir()
    (proj / "project.yaml").write_text(f"project_id: {pid}\n", encoding="utf-8")
    return proj


def test_cases_endpoint_llm_true(monkeypatch, tmp_path):
    _mk_tmp_project(tmp_path)
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    monkeypatch.setitem(os.environ, "LLM_API_KEY", "dummy")  # 让 llm_configured()=True
    monkeypatch.setattr(
        gc, "llm_generate",
        lambda *a, **k: (
            "| id | 标题 | 模块 | 类型 | 优先级 | 前置 | 步骤 | 预期 | 可自动化 |\n"
            "|---|---|---|---|---|---|---|---|---|\n"
            "| REQ-001-F | 登录（功能） | 认证 | 功能 | P1 | 已部署 | 1. 登录 | 成功 | 可（pytest） |"
        ),
    )
    r = client.post("/api/projects/demo/cases",
                    json={"requirements": "1. 用户可登录", "llm": True})
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] and d["llm_used"] is True and "REQ-001-F" in d["markdown"]


def test_cases_endpoint_llm_false_uses_rule(monkeypatch, tmp_path):
    _mk_tmp_project(tmp_path)
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    monkeypatch.setitem(os.environ, "LLM_API_KEY", "dummy")
    called = {"llm": False}
    monkeypatch.setattr(gc, "llm_generate", lambda *a, **k: called.__setitem__("llm", True) or "| x |")
    r = client.post("/api/projects/demo/cases", json={"requirements": "1. 用户可登录", "llm": False})
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] and d["llm_used"] is False and called["llm"] is False
    assert "| REQ-001-F |" in d["markdown"]  # 规则版产物


def test_lessons_endpoint_with_and_without_file(monkeypatch, tmp_path):
    proj = _mk_tmp_project(tmp_path)
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    # 无 lessons.md -> 返回 has=False 且 content 为空
    r0 = client.get("/api/projects/demo/lessons")
    assert r0.status_code == 200
    d0 = r0.get_json()
    assert d0["ok"] and d0["has"] is False and d0["content"] == ""
    # 写入 lessons.md -> 返回内容
    (proj / "lessons.md").write_text("# 情景记忆\n1. **登录失败锁定** —— 历史失败 3 次\n", encoding="utf-8")
    r1 = client.get("/api/projects/demo/lessons")
    assert r1.status_code == 200
    d1 = r1.get_json()
    assert d1["ok"] and d1["has"] is True and "登录失败锁定" in d1["content"]


def test_lessons_endpoint_unknown_project(monkeypatch, tmp_path):
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    r = client.get("/api/projects/nope/lessons")
    assert r.status_code == 404


_FULL_TABLE = (
    "| id | 标题 | 模块 | 类型 | 优先级 | 前置 | 步骤 | 预期 | 可自动化 |\n"
    "|---|---|---|---|---|---|---|---|---|\n"
    "| REQ-001-F | 登录（功能） | 认证 | 功能 | P1 | 已部署 | 1. 登录 | 成功 | 可（pytest） |"
)


def test_cases_endpoint_agentic_true(monkeypatch, tmp_path):
    _mk_tmp_project(tmp_path)
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    monkeypatch.setitem(os.environ, "LLM_API_KEY", "dummy")
    monkeypatch.setattr(gc, "agentic_generate", lambda *a, **k: _FULL_TABLE)
    r = client.post("/api/projects/demo/cases",
                    json={"requirements": "1. 用户可登录", "llm": True, "agentic": True})
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] and d["agentic_used"] is True and "REQ-001-F" in d["markdown"]


def test_files_endpoint_includes_run_meta(monkeypatch, tmp_path):
    proj = _mk_tmp_project(tmp_path)
    (proj / "artifacts").mkdir()
    (proj / "artifacts" / "run_meta.json").write_text(
        '{"ts":"2026-09-11 14:00","mode":"规则版","cases":9}', encoding="utf-8")
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    r = client.get("/api/projects/demo/files")
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] and d["run_meta"]["mode"] == "规则版" and d["run_meta"]["cases"] == 9


def test_files_endpoint_run_meta_absent_is_none(monkeypatch, tmp_path):
    _mk_tmp_project(tmp_path)
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    d = client.get("/api/projects/demo/files").get_json()
    assert d["ok"] and d["run_meta"] is None


def test_run_endpoint_passes_llm_agentic_flags(monkeypatch, tmp_path):
    _mk_tmp_project(tmp_path)
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    monkeypatch.setitem(os.environ, "LLM_API_KEY", "dummy")  # 让 _llm_available()=True
    captured = {}

    def fake_spawn(kind, pid, args, scene=None, extra_args=None):
        captured.update(kind=kind, pid=pid, args=args, extra_args=extra_args)
        return "tid123"

    monkeypatch.setattr(web_app, "_spawn_task", fake_spawn)
    r = client.post("/api/projects/demo/run", json={"llm": True, "agentic": True})
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] and d["llm"] is True and d["agentic"] is True
    assert captured["extra_args"] == ["--llm", "--agentic"]


def test_run_endpoint_defaults_to_rule_mode(monkeypatch, tmp_path):
    _mk_tmp_project(tmp_path)
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    captured = {}
    monkeypatch.setattr(
        web_app, "_spawn_task",
        lambda kind, pid, args, scene=None, extra_args=None:
            captured.update(extra_args=extra_args) or "tid")
    r = client.post("/api/projects/demo/run")
    assert r.status_code == 200
    assert captured["extra_args"] == []
    assert r.get_json()["llm"] is False


def test_run_endpoint_agentic_requires_llm(monkeypatch, tmp_path):
    _mk_tmp_project(tmp_path)
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    captured = {}
    monkeypatch.setattr(
        web_app, "_spawn_task",
        lambda kind, pid, args, scene=None, extra_args=None:
            captured.update(extra_args=extra_args) or "tid")
    r = client.post("/api/projects/demo/run", json={"agentic": True})  # 未同时给 llm
    assert r.status_code == 200
    assert captured["extra_args"] == []  # agentic 必须配合 llm


def test_cases_endpoint_injects_lessons(monkeypatch, tmp_path):
    proj = _mk_tmp_project(tmp_path)
    (proj / "lessons.md").write_text(
        "# 情景记忆\n1. **登录失败锁定** —— 历史失败 3 次\n", encoding="utf-8")
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    captured = {}

    def fake_gen(text, **k):
        captured["extra"] = k.get("extra_context")
        return _FULL_TABLE

    monkeypatch.setattr(gc, "generate_from_text", fake_gen)
    r = client.post("/api/projects/demo/cases", json={"requirements": "1. 用户可登录"})
    assert r.status_code == 200
    d = r.get_json()
    assert d["lessons_injected"] is True
    assert "登录失败锁定" in (captured.get("extra") or "")


# ---------------------------------------------------------------------------
# ④ 性能与安全冒烟（Web 层）
# ---------------------------------------------------------------------------
def test_perf_security_endpoint_maps_options_to_flags(monkeypatch, tmp_path):
    _mk_tmp_project(tmp_path)
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    captured = {}
    monkeypatch.setattr(
        web_app, "_spawn_task",
        lambda kind, pid, args, scene=None, extra_args=None:
            captured.update(kind=kind, pid=pid, args=args, extra_args=extra_args) or "tid")
    r = client.post("/api/projects/demo/perf-security",
                    json={"only": "security", "users": 12, "iterations": 8})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    assert captured["kind"] == "perf-security"
    assert captured["args"] == ["perf-security", "demo"]
    assert captured["extra_args"] == ["--only", "security", "--users", "12", "--iterations", "8"]


def test_perf_security_endpoint_defaults_and_ignores_bad_values(monkeypatch, tmp_path):
    _mk_tmp_project(tmp_path)
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    captured = {}
    monkeypatch.setattr(
        web_app, "_spawn_task",
        lambda kind, pid, args, scene=None, extra_args=None:
            captured.update(extra_args=extra_args) or "tid")
    # only 非法 / 数值非法 → 全部忽略，走 project.yaml 里的默认配置
    r = client.post("/api/projects/demo/perf-security",
                    json={"only": "hack", "users": "abc", "iterations": -3})
    assert r.status_code == 200
    assert captured["extra_args"] == []


def test_perf_security_endpoint_unknown_project_404(monkeypatch, tmp_path):
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    r = client.post("/api/projects/nope/perf-security")
    assert r.status_code == 404


def test_files_endpoint_exposes_perf_security(monkeypatch, tmp_path):
    proj = _mk_tmp_project(tmp_path)
    (proj / "artifacts").mkdir()
    (proj / "artifacts" / "perf_security.json").write_text(
        '{"all_pass": true, "summary": "ok"}', encoding="utf-8")
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    d = client.get("/api/projects/demo/files").get_json()
    assert d["perf_security"]["all_pass"] is True


def test_files_endpoint_perf_security_absent_is_none(monkeypatch, tmp_path):
    _mk_tmp_project(tmp_path)
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    d = client.get("/api/projects/demo/files").get_json()
    assert d["perf_security"] is None


def test_projects_api_includes_perf_security_field(monkeypatch, tmp_path):
    proj = _mk_tmp_project(tmp_path)
    (proj / "artifacts").mkdir()
    (proj / "artifacts" / "perf_security.json").write_text(
        '{"all_pass": false, "summary": "x"}', encoding="utf-8")
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    items = client.get("/api/projects").get_json()["projects"]
    assert len(items) == 1
    assert items[0]["perf_security"]["all_pass"] is False


def test_index_has_perf_security_ui_hooks():
    """前端必须真的提供入口与渲染函数，否则接口再好用户也用不上。"""
    html = client.get("/").get_data(as_text=True)
    for token in ("perf-security", "renderPerfSecPanel", "d_perfsec", "runPerfSecurity"):
        assert token in html, f"前端缺少 {token}"


# ---------------------------------------------------------------------------
# ⑤ Web UI 冒烟（Web 层）
# ---------------------------------------------------------------------------
def _mk_tmp_project_with_web(tmp_path, pid="demo", scenes: str = "scenarios: []\n"):
    proj = _mk_tmp_project(tmp_path, pid)
    (proj / "web.yaml").write_text(f"web:\n  {scenes}", encoding="utf-8")
    return proj


def test_web_endpoint_maps_options_to_flags(monkeypatch, tmp_path):
    _mk_tmp_project_with_web(tmp_path)
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    captured = {}
    monkeypatch.setattr(
        web_app, "_spawn_task",
        lambda kind, pid, args, scene=None, extra_args=None:
            captured.update(kind=kind, pid=pid, args=args, extra_args=extra_args) or "tid")
    r = client.post("/api/projects/demo/web",
                    json={"only": "smoke", "browser": "firefox", "headed": True})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    assert captured["kind"] == "web"
    assert captured["args"] == ["web", "demo"]
    assert captured["extra_args"] == ["--only", "smoke", "--headed", "--browser", "firefox"]


def test_web_endpoint_defaults_and_ignores_bad_values(monkeypatch, tmp_path):
    _mk_tmp_project_with_web(tmp_path)
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    captured = {}
    monkeypatch.setattr(
        web_app, "_spawn_task",
        lambda kind, pid, args, scene=None, extra_args=None:
            captured.update(extra_args=extra_args) or "tid")
    # 非法浏览器 / only 为空 / headed 非真 → 全部忽略，用 web.yaml 的配置
    r = client.post("/api/projects/demo/web",
                    json={"only": "  ", "browser": "ie6", "headed": "no"})
    assert r.status_code == 200
    assert captured["extra_args"] == []


def test_web_endpoint_unknown_project_404(monkeypatch, tmp_path):
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    r = client.post("/api/projects/nope/web")
    assert r.status_code == 404


def test_web_endpoint_without_web_yaml_returns_400(monkeypatch, tmp_path):
    """缺 web.yaml 必须显式报错，而不是静默起一个必然失败的任务。"""
    _mk_tmp_project(tmp_path)
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    r = client.post("/api/projects/demo/web")
    assert r.status_code == 400
    assert "web.yaml" in r.get_json()["error"]


def test_files_endpoint_exposes_web(monkeypatch, tmp_path):
    proj = _mk_tmp_project(tmp_path)
    (proj / "artifacts").mkdir()
    (proj / "artifacts" / "web.json").write_text(
        '{"all_pass": true, "summary": "ok"}', encoding="utf-8")
    (proj / "web.yaml").write_text("web:\n  scenarios: []\n", encoding="utf-8")
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    d = client.get("/api/projects/demo/files").get_json()
    assert d["web"]["all_pass"] is True
    assert "web" in d["files"]          # web.yaml 可读可编辑


def test_files_endpoint_web_absent_is_none(monkeypatch, tmp_path):
    _mk_tmp_project(tmp_path)
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    d = client.get("/api/projects/demo/files").get_json()
    assert d["web"] is None
    assert d["files"]["web"] == ""


def test_projects_api_includes_web_field(monkeypatch, tmp_path):
    proj = _mk_tmp_project(tmp_path)
    (proj / "artifacts").mkdir()
    (proj / "artifacts" / "web.json").write_text(
        '{"all_pass": false, "summary": "x"}', encoding="utf-8")
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    items = client.get("/api/projects").get_json()["projects"]
    assert len(items) == 1
    assert items[0]["web"]["all_pass"] is False


def test_automation_api_includes_web_scenarios(monkeypatch, tmp_path):
    """/api/automation 要把 web.yaml 的场景清单带上，并标出「无断言」的场景。"""
    proj = _mk_tmp_project(tmp_path)
    (proj / "web.yaml").write_text(
        "web:\n"
        "  scenarios:\n"
        "    - name: 登录\n"
        "      tags: [smoke]\n"
        "      steps:\n"
        "        - goto: /login\n"
        "        - expect_visible: \"body\"\n"
        "    - name: 只点不断言\n"
        "      steps:\n"
        "        - goto: /\n"
        "        - click: \"#x\"\n",
        encoding="utf-8")
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    items = client.get("/api/automation").get_json()["automations"]
    assert len(items) == 1
    ws = items[0]["web_scenarios"]
    assert [s["name"] for s in ws] == ["登录", "只点不断言"]
    assert ws[0]["assertions"] == 1 and ws[0]["tags"] == ["smoke"]
    assert ws[1]["assertions"] == 0     # 前端据此打「无断言」标记
    assert items[0]["has_web_yaml"] is True


def test_automation_api_without_web_yaml(monkeypatch, tmp_path):
    _mk_tmp_project(tmp_path)
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    items = client.get("/api/automation").get_json()["automations"]
    assert items[0]["web_scenarios"] == []
    assert items[0]["has_web_yaml"] is False


# ---- 失败证据静态路由（截图 / 可复现脚本） ----
def test_evidence_routes_serve_shot_and_spec(monkeypatch, tmp_path):
    proj = _mk_tmp_project(tmp_path)
    shots = proj / "artifacts" / "web_shots"
    shots.mkdir(parents=True)
    (shots / "a-FAIL.png").write_bytes(b"\x89PNG")
    (proj / "artifacts" / "web_repro_a.spec.ts").write_text("// x", encoding="utf-8")
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    assert client.get("/reports/demo/web_shots/a-FAIL.png").status_code == 200
    assert client.get("/reports/demo/web_repro_a.spec.ts").status_code == 200


def test_evidence_routes_reject_non_evidence_suffix(monkeypatch, tmp_path):
    """白名单后缀：不允许把证据路由变成任意文件读取入口。"""
    proj = _mk_tmp_project(tmp_path)
    (proj / "artifacts").mkdir()
    (proj / "artifacts" / "report.py").write_text("print(1)", encoding="utf-8")
    (proj / "project.yaml").write_text("project_id: demo\n", encoding="utf-8")
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    assert client.get("/reports/demo/report.py").status_code == 404
    # 上跳穿越同样必须被拒
    assert client.get("/reports/demo/..%2f..%2fproject.yaml").status_code == 404
    assert client.get("/reports/demo/..%5cproject.yaml").status_code == 404


def test_evidence_routes_do_not_shadow_report_html(monkeypatch, tmp_path):
    proj = _mk_tmp_project(tmp_path)
    (proj / "artifacts").mkdir()
    (proj / "artifacts" / "report.html").write_text("<h1>ok</h1>", encoding="utf-8")
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    r = client.get("/reports/demo/report.html")
    assert r.status_code == 200
    assert b"ok" in r.get_data()


def test_index_has_web_smoke_ui_hooks():
    """Web 冒烟的前端入口/渲染/证据链接必须都在，否则功能等于没接。"""
    html = client.get("/").get_data(as_text=True)
    for token in ("web:p=>", "renderWebPanel", "runWeb", 'id="d_web"',
                  'id="d_webyaml"', 'id="btnWebWrap"', 'data-tab="web"',
                  "repUrl", "webYamlValue", "has_web_yaml"):
        assert token in html, f"前端缺少 {token}"
    # 详情弹窗是静态 HTML：里面不能残留模板插值，否则会原样显示成 ${svg('...')} Web 冒烟
    modal = html.split('id="detailMask"')[1].split("<!-- 完整报告弹窗")[0]
    assert "${svg(" not in modal, "详情弹窗静态 HTML 里残留了模板插值（会原样显示）"


# ---- 门禁总览（/api/gates，与 CLI gate_notify 同源） ----
def _write_gate(proj, name, all_pass, summary=""):
    import json as _json
    art = proj / "artifacts"
    art.mkdir(exist_ok=True)
    (art / name).write_text(
        _json.dumps({"all_pass": all_pass, "summary": summary}, ensure_ascii=False),
        encoding="utf-8")


def test_gates_api_shape(monkeypatch, tmp_path):
    proj = _mk_tmp_project(tmp_path)
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    d = client.get("/api/gates").get_json()
    assert d["ok"] is True
    assert isinstance(d["rows"], list) and d["rows"]
    assert set(d["summary"]) >= {"projects", "pass", "fail", "not_run", "all_pass"}
    assert d["text"].strip()


def test_gates_api_reuses_cli_semantics(monkeypatch, tmp_path):
    """控制台与 CLI 必须同一口径，否则出现「控制台说绿、CI 说红」两套结论。"""
    proj = _mk_tmp_project(tmp_path)
    # 有 regression.yaml（= 声明了门禁）但没产物 → 未执行，不能算绿
    (proj / "regression.yaml").write_text("core_business: []\n", encoding="utf-8")
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    d = client.get("/api/gates").get_json()
    row = d["rows"][0]
    assert row["gates"]["regression"]["status"] == "not_run"
    assert row["verdict"] == "not_run"          # 未执行 ≠ 通过
    assert d["summary"]["all_pass"] is False


def test_gates_api_three_states(monkeypatch, tmp_path):
    proj = _mk_tmp_project(tmp_path)
    (proj / "regression.yaml").write_text("core_business: []\n", encoding="utf-8")
    (proj / "web.yaml").write_text("web:/n  scenarios: []\n", encoding="utf-8")
    _write_gate(proj, "regression.json", True)
    _write_gate(proj, "perf_security.json", False, "1 项失败")
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    g = client.get("/api/gates").get_json()["rows"][0]["gates"]
    assert g["regression"]["status"] == "pass"
    assert g["perf_security"]["status"] == "fail"
    assert g["web"]["status"] == "not_run"          # web.yaml 声明了但没跑
    assert g["perf_security"]["summary"] == "1 项失败"


def test_gates_api_project_filter(monkeypatch, tmp_path):
    """CI 用法：只看本次参与门禁的项目，否则其余项目全记「未执行」→ 永远红。"""
    a = _mk_tmp_project(tmp_path, "gate-a")
    _write_gate(a, "regression.json", True)
    (a / "regression.yaml").write_text("core_business: []\n", encoding="utf-8")
    _mk_tmp_project(tmp_path, "never-ran")
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)

    allrows = client.get("/api/gates").get_json()
    assert len(allrows["rows"]) == 2 and allrows["summary"]["all_pass"] is False
    one = client.get("/api/gates?project=gate-a").get_json()
    assert len(one["rows"]) == 1 and one["rows"][0]["pid"] == "gate-a"


def test_index_has_gates_ui_hooks():
    """门禁页的前端入口/渲染函数必须在，否则接口再好用户也用不上。"""
    html = client.get("/").get_data(as_text=True)
    for token in ('data-page="gates"', 'id="page-gates"', "renderGates", "gateSummary",
                  "gateList", "gateText", "GATE_MARK", "copyGateText"):
        assert token in html, f"前端缺少 {token}"


# ---- 门禁总览（/api/gates，与 CLI gate_notify 同源） ----
def _write_gate(proj, name, all_pass, summary=""):
    import json as _json
    art = proj / "artifacts"
    art.mkdir(exist_ok=True)
    (art / name).write_text(
        _json.dumps({"all_pass": all_pass, "summary": summary}, ensure_ascii=False),
        encoding="utf-8")


def test_gates_api_shape(monkeypatch, tmp_path):
    proj = _mk_tmp_project(tmp_path)
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    d = client.get("/api/gates").get_json()
    assert d["ok"] is True
    assert isinstance(d["rows"], list) and d["rows"]
    assert set(d["summary"]) >= {"projects", "pass", "fail", "not_run", "all_pass"}
    assert d["text"].strip()


def test_gates_api_reuses_cli_semantics(monkeypatch, tmp_path):
    """控制台与 CLI 必须同一口径，否则出现「控制台说绿、CI 说红」两套结论。"""
    proj = _mk_tmp_project(tmp_path)
    # 有 regression.yaml（= 声明了门禁）但没产物 → 未执行，不能算绿
    (proj / "regression.yaml").write_text("core_business: []\n", encoding="utf-8")
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    d = client.get("/api/gates").get_json()
    row = d["rows"][0]
    assert row["gates"]["regression"]["status"] == "not_run"
    assert row["verdict"] == "not_run"          # 未执行 ≠ 通过
    assert d["summary"]["all_pass"] is False


def test_gates_api_three_states(monkeypatch, tmp_path):
    proj = _mk_tmp_project(tmp_path)
    (proj / "regression.yaml").write_text("core_business: []\n", encoding="utf-8")
    (proj / "web.yaml").write_text("web:\n  scenarios: []\n", encoding="utf-8")
    _write_gate(proj, "regression.json", True)
    _write_gate(proj, "perf_security.json", False, "1 项失败")
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    g = client.get("/api/gates").get_json()["rows"][0]["gates"]
    assert g["regression"]["status"] == "pass"
    assert g["perf_security"]["status"] == "fail"
    assert g["web"]["status"] == "not_run"          # web.yaml 声明了但没跑
    assert g["perf_security"]["summary"] == "1 项失败"


def test_gates_api_project_filter(monkeypatch, tmp_path):
    """CI 用法：只看本次参与门禁的项目，否则其余项目全记「未执行」→ 永远红。"""
    a = _mk_tmp_project(tmp_path, "gate-a")
    _write_gate(a, "regression.json", True)
    (a / "regression.yaml").write_text("core_business: []\n", encoding="utf-8")
    _mk_tmp_project(tmp_path, "never-ran")
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)

    allrows = client.get("/api/gates").get_json()
    assert len(allrows["rows"]) == 2 and allrows["summary"]["all_pass"] is False
    one = client.get("/api/gates?project=gate-a").get_json()
    assert len(one["rows"]) == 1 and one["rows"][0]["pid"] == "gate-a"


def test_index_has_gates_ui_hooks():
    """门禁页的前端入口/渲染函数必须在，否则接口再好用户也用不上。"""
    html = client.get("/").get_data(as_text=True)
    for token in ('data-page="gates"', 'id="page-gates"', "renderGates", "gateSummary",
                  "gateList", "gateText", "GATE_MARK", "copyGateText"):
        assert token in html, f"前端缺少 {token}"


def test_gates_api_skips_disabled_projects(monkeypatch, tmp_path):
    """与项目页/看板口径一致：停用项目不该继续把门禁拖红（否则是看不到的"幽灵红"）。"""
    keep = _mk_tmp_project(tmp_path, "active")
    (keep / "regression.yaml").write_text("core_business: []\n", encoding="utf-8")
    _write_gate(keep, "regression.json", True)

    retired = _mk_tmp_project(tmp_path, "retired")
    (retired / "regression.yaml").write_text("core_business: []\n", encoding="utf-8")
    _write_gate(retired, "regression.json", False, "早就没维护了")
    (retired / ".disabled").write_text("", encoding="utf-8")

    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    d = client.get("/api/gates").get_json()
    assert [r["pid"] for r in d["rows"]] == ["active"]
    assert d["disabled"] == ["retired"]
    assert "已停用" in d["text"]          # 跳过要如实交代，不能静默隐藏
    assert d["summary"]["disabled"] == 1

    inc = client.get("/api/gates?include_disabled=1").get_json()
    assert sorted(r["pid"] for r in inc["rows"]) == ["active", "retired"]
    assert inc["disabled"] == []


def test_index_has_disabled_note_hook():
    """前端要能显示"跳过了哪些停用项目"，否则用户会以为全都算过了。"""
    html = client.get("/").get_data(as_text=True)
    assert "gateDisabledNote" in html
    assert "d.disabled" in html


def test_gates_api_skips_disabled_projects(monkeypatch, tmp_path):
    """与项目页/看板口径一致：停用项目不该继续把门禁拖红（否则是看不到的"幽灵红"）。"""
    keep = _mk_tmp_project(tmp_path, "active")
    (keep / "regression.yaml").write_text("core_business: []\n", encoding="utf-8")
    _write_gate(keep, "regression.json", True)

    retired = _mk_tmp_project(tmp_path, "retired")
    (retired / "regression.yaml").write_text("core_business: []\n", encoding="utf-8")
    _write_gate(retired, "regression.json", False, "早就没维护了")
    (retired / ".disabled").write_text("", encoding="utf-8")

    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path)
    d = client.get("/api/gates").get_json()
    assert [r["pid"] for r in d["rows"]] == ["active"]
    assert d["disabled"] == ["retired"]
    assert "已停用" in d["text"]          # 跳过要如实交代，不能静默隐藏
    assert d["summary"]["disabled"] == 1

    inc = client.get("/api/gates?include_disabled=1").get_json()
    assert sorted(r["pid"] for r in inc["rows"]) == ["active", "retired"]
    assert inc["disabled"] == []


def test_index_has_disabled_note_hook():
    """前端要能显示"跳过了哪些停用项目"，否则用户会以为全都算过了。"""
    html = client.get("/").get_data(as_text=True)
    assert "gateDisabledNote" in html
    assert "d.disabled" in html
