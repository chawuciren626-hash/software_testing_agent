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
