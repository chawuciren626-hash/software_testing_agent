"""控制台 L 层最小拦截守护测试（审阅报告 §4.3）：只读模式 + 审计。

三条核心承诺，每条都要有会变红的守护：
1. **默认不改变行为** —— 不开只读时，写操作照常到达处理函数（不是被误拦）。
2. **只读真拦得住** —— `STA_CONSOLE_READONLY=1` 下一切 `/api/*` 写操作 403，读操作 200。
3. **写操作留得下痕** —— `audit.jsonl` 每条含 谁 / 何时 / 哪个项目 / 结果；
   被拦下的尝试**也要**留痕（那正是最想知道的事）；审计写失败**不能**弄挂业务。

⚠️ 测试不得触发真实任务：一律用"项目不存在 → 404"的写操作来验证"走到了处理函数"，
避免 `create` 起子进程污染真实 `projects/`。
"""
from __future__ import annotations

import json

import pytest

import app as web_app
import web_console.auth as auth
import web_console.guard as guard

TOKEN = "s3cr3t-console-token"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(web_app.pm, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.delenv(guard.READONLY_ENV, raising=False)
    monkeypatch.setenv(guard.AUDIT_FILE_ENV, str(tmp_path / "audit.jsonl"))
    return web_app.app.test_client()


@pytest.fixture
def read_audit(tmp_path):
    def _read():
        p = tmp_path / "audit.jsonl"
        if not p.is_file():
            return []
        return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return _read


# --------------------------------------------------------------------------- #
# 开关口径（与 auth.py 的 TOKEN_ENV 保持一致）
# --------------------------------------------------------------------------- #
def test_readonly_off_by_default(monkeypatch):
    monkeypatch.delenv(guard.READONLY_ENV, raising=False)
    assert guard.readonly() is False


@pytest.mark.parametrize("val", ["", " ", "off", "OFF", "0", "false", "no", "none", "disabled", "null"])
def test_readonly_off_values(monkeypatch, val):
    monkeypatch.setenv(guard.READONLY_ENV, val)
    assert guard.readonly() is False, f"{val!r} 应视为关闭"


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "  1  "])
def test_readonly_on_values(monkeypatch, val):
    monkeypatch.setenv(guard.READONLY_ENV, val)
    assert guard.readonly() is True, f"{val!r} 应视为开启"


# --------------------------------------------------------------------------- #
# 默认不改变行为：写操作要能到达处理函数（否则"拦"变成了"误伤"）
# --------------------------------------------------------------------------- #
def test_mutation_reaches_handler_when_not_readonly(client):
    r = client.post("/api/projects/ghost/disable")
    assert r.status_code == 404, "非只读时应走到处理函数（项目不存在 → 404），而不是 403"


def test_delete_still_needs_confirm_when_not_readonly(client):
    """破坏性操作的"二次确认"仍在：不带 confirm 的删除必须 400，而不是直接删。"""
    r = client.delete("/api/projects/ghost")
    assert r.status_code == 400
    assert "confirm" in r.get_json()["error"]


# --------------------------------------------------------------------------- #
# 只读模式：写全拦、读放行
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("method,path", [
    ("POST", "/api/projects"),
    ("DELETE", "/api/projects/ghost?confirm=1"),
    ("POST", "/api/projects/ghost/disable"),
    ("POST", "/api/projects/ghost/run"),
    ("POST", "/api/projects/ghost/regression"),
    ("POST", "/api/projects/ghost/perf-security"),
    ("POST", "/api/projects/ghost/web"),
    ("POST", "/api/projects/ghost/files"),
    ("POST", "/api/projects/ghost/cases"),
    ("POST", "/api/models"),
    ("POST", "/api/skills/anything/toggle"),
])
def test_readonly_blocks_all_api_mutations(client, monkeypatch, method, path):
    monkeypatch.setenv(guard.READONLY_ENV, "1")
    r = client.open(path, method=method, json={})
    assert r.status_code == 403, f"{method} {path} 在只读模式下必须被拒"
    assert r.is_json and r.get_json()["ok"] is False
    assert "只读" in r.get_json()["error"]


def test_readonly_allows_reads(client, monkeypatch):
    monkeypatch.setenv(guard.READONLY_ENV, "1")
    assert client.get("/api/projects").status_code == 200
    assert client.get("/api/auth/status").status_code == 200
    assert client.get("/healthz").status_code == 200
    assert client.get("/").status_code == 200


def test_readonly_does_not_block_non_api_post(client, monkeypatch):
    """`/login` 是登录流程，不是"改状态"的业务动作 —— 只读下必须还能登录，否则看都看不了。"""
    monkeypatch.setenv(guard.READONLY_ENV, "1")
    r = client.post("/login", data={"token": "x"})
    assert r.status_code != 403


# --------------------------------------------------------------------------- #
# 审计：谁 / 何时 / 哪个项目 / 结果
# --------------------------------------------------------------------------- #
def test_audit_records_mutation_with_required_fields(client, read_audit):
    r = client.post("/api/projects/ghost/disable")
    assert r.status_code == 404
    lines = read_audit()
    assert len(lines) == 1, "一次写操作应恰好留一条痕"
    rec = lines[0]
    assert rec["event"] == "api_mutation"
    assert rec["method"] == "POST"
    assert rec["path"] == "/api/projects/ghost/disable"
    assert rec["pid"] == "ghost"            # 对哪个项目
    assert rec["status"] == 404             # 结果
    assert rec["actor"] == "anonymous"      # 谁（未开鉴权 → 如实记 anonymous）
    assert rec["ts"] and rec["run_id"]      # 何时 / 可关联同一次运行


def test_audit_records_blocked_attempt_once(client, read_audit, monkeypatch):
    """被只读拦下的尝试**也要**留痕（这正是最想知道的事），且不能重复记两条。"""
    monkeypatch.setenv(guard.READONLY_ENV, "1")
    r = client.post("/api/projects/ghost/run")
    assert r.status_code == 403
    lines = read_audit()
    assert len(lines) == 1, f"应只留一条（before_request 记过，after_request 不再重复）：{lines}"
    assert lines[0]["event"] == "blocked_readonly"
    assert lines[0]["pid"] == "ghost" and lines[0]["status"] == 403


def test_audit_skips_reads(client, read_audit):
    client.get("/api/projects")
    client.get("/api/auth/status")
    assert read_audit() == [], "GET 是「看」不是「改」，不应产审计行（否则读请求会把审计刷屏）"


def test_audit_failure_does_not_break_the_request(client, monkeypatch, tmp_path):
    """审计写不进去（路径非法）时，业务**照常完成** —— 留痕失败不能反过来弄挂业务。

    同时它也不能静默：该分支走 log.warning（守护见 test_obs.py 的静默 except 扫描）。
    """
    blocker = tmp_path / "afile"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv(guard.AUDIT_FILE_ENV, str(blocker / "audit.jsonl"))
    r = client.post("/api/projects/ghost/disable")
    assert r.status_code == 404, "审计写失败不应把请求变成 500"


def test_audit_actor_is_token_prefix_when_authed(client, read_audit, monkeypatch):
    """开了鉴权时"谁"要落到具体会话（token 指纹前 8 位），而不是笼统 anonymous。"""
    monkeypatch.setenv(auth.TOKEN_ENV, TOKEN)
    client.post("/login", data={"token": TOKEN})
    client.post("/api/projects/ghost/disable")
    lines = read_audit()
    assert lines and lines[-1]["actor"].startswith("token:")
    assert TOKEN not in json.dumps(lines), "审计里绝不能出现 token 本体"


@pytest.mark.parametrize("path,expected", [
    ("/api/projects/mall-admin/run", "mall-admin"),
    ("/api/projects/a%20b/files", "a b"),
    ("/api/projects", "-"),
    ("/api/models", "-"),
    ("/api/skills/x/toggle", "-"),
])
def test_target_extracts_project(path, expected):
    assert guard._target(path) == expected


# --------------------------------------------------------------------------- #
# auth/status 暴露只读状态 + 前端横幅
# --------------------------------------------------------------------------- #
def test_auth_status_exposes_readonly(client, monkeypatch):
    assert client.get("/api/auth/status").get_json()["readonly"] is False
    monkeypatch.setenv(guard.READONLY_ENV, "1")
    assert client.get("/api/auth/status").get_json()["readonly"] is True


def test_index_has_readonly_banner(client):
    html = client.get("/").get_data(as_text=True)
    assert "roBanner" in html, "只读状态下要能提前告知，别等点了按钮才吃 403"


# --------------------------------------------------------------------------- #
# .env 明文凭据提示（§4.3③）
# --------------------------------------------------------------------------- #
def test_models_save_warns_about_plaintext_credentials(client, monkeypatch, tmp_path):
    """写入 .env 后必须提示"含明文凭据、勿提交"。把 ROOT 重定向到 tmp，别碰真 .env。"""
    monkeypatch.setattr(web_app, "ROOT", tmp_path)
    monkeypatch.setattr(web_app, "DATA_ROOT", tmp_path)
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
    r = client.post("/api/models", json={"api_key": "sk-test-123", "provider": "openai"})
    d = r.get_json()
    assert d["ok"] is True
    assert "明文凭据" in d["secret_note"] and "勿提交" in d["secret_note"]
    assert "LLM_API_KEY" in d["updated"]
    # 确实写进了（重定向后的）.env，且值是对的
    assert 'LLM_API_KEY="sk-test-123"' in (tmp_path / ".env").read_text(encoding="utf-8")


def test_git_ignored_detects_env(tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "DATA_ROOT", tmp_path)
    assert web_app._is_git_ignored(tmp_path / ".env") is False     # 没有 .gitignore
    (tmp_path / ".gitignore").write_text("*.log\n.env\n", encoding="utf-8")
    assert web_app._is_git_ignored(tmp_path / ".env") is True
