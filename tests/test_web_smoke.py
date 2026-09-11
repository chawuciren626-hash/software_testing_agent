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
