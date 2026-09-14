"""AI 探索**独立入口**的守护测试（审阅报告 §3.3 / §7 序 6）。

覆盖策略（为什么这么切）：
- **降级四条路（无 key / 超时 / 异常 / 缺任务）必须真被验证** —— 但真跑需要
  key + 活的被测应用 + 浏览器，等于这四条路永远没人验。所以：
  · 把"起子进程"做成缝（`_spawn_bworld`），超时/异常在**测试内注入**；
  · 把"探 key"做成缝（`probe_provider`），无 key 在**测试内注入**；
  · "缺任务"路径**不需要任何缝**，直接用真实进程边界验证。
- **ok 路径用假的基座产物验证**：注入一个会写 `report_x/result.json` 的假 spawn，
  钉死"契约字段映射"，不依赖真跑。

⚠️ 一个必须记住的环境陷阱：仓库根 `.env` 里可能有**真实**凭据，而
`common.auth.load_dotenv` 用 `setdefault` 注入 —— 且 Windows 上
`os.environ['K'] = ''` 会把变量**删掉**（putenv 语义），于是 setdefault 又会把
真值灌回来。所以**不要**试图靠"清空环境变量"来模拟无 key，那样在 Windows 上失效。
本文件的正确做法是走 `probe_provider` 这个缝。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import agentic_contract as C
import run_agentic as ra

ENTRY = Path(ra.__file__)


def _mission(pdir: Path) -> Path:
    m = pdir / "mission.yaml"
    m.parent.mkdir(parents=True, exist_ok=True)
    m.write_text("missions:\n  - thread_id: t_01\n    prompt: go\n", encoding="utf-8")
    return m


# --------------------------------------------------------------------------- #
# 纯函数：命令构造 / 环境翻译
# --------------------------------------------------------------------------- #
def test_bworld_cmd_shape():
    cmd = ra.bworld_cmd("py", Path("m.yaml"), 12)
    assert cmd[:3] == ["py", "-m", "agentic_explorer.main"]
    assert "--missions" in cmd and "m.yaml" in cmd
    assert cmd[cmd.index("--max-steps") + 1] == "12"
    assert "--headed" not in cmd
    assert "--headed" in ra.bworld_cmd("py", Path("m.yaml"), 1, headed=True)


def test_bworld_env_maps_project_target_and_credentials(tmp_path, monkeypatch):
    (tmp_path / "project.yaml").write_text(
        "env:\n"
        "  base_url: http://localhost:8080\n"
        "  auth:\n"
        "    type: token\n"
        "    username_env: U\n"
        "    password_env: P\n",
        encoding="utf-8")
    # 凭据真值来自**进程环境**（口径唯一：resolve_auth 按 *_env 名去 os.getenv 取）
    monkeypatch.setenv("U", "alice")
    monkeypatch.setenv("P", "secret")
    env = ra.bworld_env(tmp_path, {})
    assert env["APP_URL"] == "http://localhost:8080"
    assert env["APP_USERNAME"] == "alice"
    assert env["APP_PASSWORD"] == "secret"


def test_bworld_env_never_overrides_explicit_values(tmp_path):
    """CI / 命令行显式注入的同名变量优先 —— 映射只做 setdefault。"""
    (tmp_path / "project.yaml").write_text(
        "env:\n  base_url: http://from-yaml\n", encoding="utf-8")
    env = ra.bworld_env(tmp_path, {"APP_URL": "http://from-env"})
    assert env["APP_URL"] == "http://from-env"


def test_bworld_env_tolerates_missing_or_broken_project_yaml(tmp_path):
    assert ra.bworld_env(tmp_path, {}) .get("PYTHONIOENCODING") == "utf-8"
    (tmp_path / "project.yaml").write_text("{{{{ not yaml", encoding="utf-8")
    env = ra.bworld_env(tmp_path, {})      # 坏 YAML 不能让阶段崩掉
    assert "APP_URL" not in env


def test_bworld_env_points_app_config_even_without_project_yaml(tmp_path):
    """config.yaml 与 project.yaml 互不依赖：缺后者不该顺带丢掉前者。"""
    (tmp_path / "config.yaml").write_text("app: {}\n", encoding="utf-8")
    env = ra.bworld_env(tmp_path, {})
    assert Path(env["APP_CONFIG"]) == tmp_path / "config.yaml"


# --------------------------------------------------------------------------- #
# 「无 key」判定必须与基座**同源**
# --------------------------------------------------------------------------- #
def test_probe_provider_delegates_to_base(monkeypatch):
    """钉死"同源"：不是自己又写一套 key 判定，而是**调用**基座的判定函数。"""
    from agentic_explorer.utils import llm as base_llm
    monkeypatch.setattr(base_llm, "get_active_provider", lambda: "claude")
    provider, source = ra.probe_provider()
    assert provider == "claude"
    assert "agentic_explorer.utils.llm" in source


def test_probe_provider_unknown_when_base_cannot_decide(monkeypatch):
    from agentic_explorer.utils import llm as base_llm
    monkeypatch.setattr(base_llm, "get_active_provider", lambda: "unknown")
    assert ra.probe_provider()[0] == "unknown"


def test_probe_provider_survives_base_raising(monkeypatch):
    from agentic_explorer.utils import llm as base_llm

    def boom():
        raise RuntimeError("no creds")

    monkeypatch.setattr(base_llm, "get_active_provider", boom)
    provider, detail = ra.probe_provider()
    assert provider == "unknown" and "no creds" in detail


# --------------------------------------------------------------------------- #
# 降级：缺任务 / 无 key（缝注入，确定性）
# --------------------------------------------------------------------------- #
def test_stage_degrades_not_configured_without_mission(tmp_path):
    p = ra.run_stage(project_dir=tmp_path)
    assert p["status"] == "degraded" and p["reason"] == "not_configured"
    # 契约必须落盘 —— 调用方只认文件
    assert C.load_result(C.result_path(tmp_path))["reason"] == "not_configured"


def test_stage_degrades_no_llm_key(tmp_path, monkeypatch):
    _mission(tmp_path)
    monkeypatch.setattr(ra, "probe_provider", lambda: ("unknown", "stub"))
    p = ra.run_stage(project_dir=tmp_path)
    assert p["status"] == "degraded" and p["reason"] == "no_llm_key"
    assert "GOOGLE_API_KEY" in p["message"] or "ANTHROPIC_API_KEY" in p["message"]


# --------------------------------------------------------------------------- #
# 降级：超时 / 异常（缝注入）
# --------------------------------------------------------------------------- #
def test_stage_degrades_on_timeout(tmp_path, monkeypatch):
    _mission(tmp_path)
    monkeypatch.setattr(ra, "probe_provider", lambda: ("claude", "stub"))

    def timeout_spawn(cmd, *, cwd, env, timeout, log_file):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(ra, "_spawn_bworld", timeout_spawn)
    p = ra.run_stage(project_dir=tmp_path, timeout=9)
    assert p["status"] == "degraded" and p["reason"] == "timeout"
    assert "9" in p["message"]


def test_stage_degrades_when_spawn_fails(tmp_path, monkeypatch):
    _mission(tmp_path)
    monkeypatch.setattr(ra, "probe_provider", lambda: ("claude", "stub"))

    def broken_spawn(cmd, *, cwd, env, timeout, log_file):
        raise FileNotFoundError("no such interpreter")

    monkeypatch.setattr(ra, "_spawn_bworld", broken_spawn)
    p = ra.run_stage(project_dir=tmp_path)
    assert p["status"] == "degraded" and p["reason"] == "error"
    assert "FileNotFoundError" in p["message"]


def test_stage_degrades_when_child_exits_without_result(tmp_path, monkeypatch):
    """子进程跑完但没产出结构化结果 —— **绝不能**写成成功。"""
    _mission(tmp_path)
    monkeypatch.setattr(ra, "probe_provider", lambda: ("claude", "stub"))
    monkeypatch.setattr(ra, "_spawn_bworld",
                        lambda cmd, **k: 1)          # 退出码 1、啥也没写
    p = ra.run_stage(project_dir=tmp_path)
    assert p["status"] == "degraded" and p["reason"] == "error"
    assert "退出码 1" in p["message"]


def test_run_stage_never_raises_even_if_inner_explodes(tmp_path, monkeypatch):
    """"绝不抛异常"是承诺，不是希望 —— 内部炸了也要收进契约。"""
    def boom(**kwargs):
        raise ZeroDivisionError("boom")

    monkeypatch.setattr(ra, "_run_stage", boom)
    p = ra.run_stage(project_dir=tmp_path)
    assert p["status"] == "degraded" and p["reason"] == "error"
    assert "ZeroDivisionError" in p["message"]
    assert C.load_result(C.result_path(tmp_path))["reason"] == "error"


# --------------------------------------------------------------------------- #
# ok 路径：用假的基座产物钉字段映射（不依赖真跑）
# --------------------------------------------------------------------------- #
BW_RESULT = {
    "thread_id": "pr_7_explorer_01",
    "end_reason": "completed",
    "goal_verdict": "achieved",
    "reasons": ["2 条确定性断言全部通过"],
    "turns": 9, "tokens": 1234, "actions": 12,
    "bugs": 2, "bug_items": ["登录按钮无响应", "表单缺少校验提示"],
    "limits": {"max_turns": 120},
}


def _fake_spawn_ok(cmd, *, cwd, env, timeout, log_file):
    rd = Path(cwd) / "report_pr_7_explorer_01"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "result.json").write_text(json.dumps(BW_RESULT, ensure_ascii=False), encoding="utf-8")
    (rd / "action_tape.jsonl").write_text('{"action":"click"}\n', encoding="utf-8")
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("done\n", encoding="utf-8")
    return 0


def test_ok_path_maps_every_contract_field(tmp_path, monkeypatch):
    _mission(tmp_path)
    monkeypatch.setattr(ra, "probe_provider", lambda: ("gemini", "stub"))
    monkeypatch.setattr(ra, "_spawn_bworld", _fake_spawn_ok)
    p = ra.run_stage(project_dir=tmp_path)

    assert p["status"] == "ok" and C.is_ok(p)
    assert p["thread_id"] == "pr_7_explorer_01"
    assert p["end_reason"] == "completed" and p["goal_verdict"] == "achieved"
    assert (p["turns"], p["tokens"], p["actions"]) == (9, 1234, 12)
    assert p["bug_count"] == 2 and "登录按钮无响应" in p["bugs"]
    assert p["report_dir"].endswith("report_pr_7_explorer_01")
    assert p["action_tape"].endswith("action_tape.jsonl")
    assert p["log"].endswith("agentic.log")
    # 落盘的就是返回的
    assert C.load_result(C.result_path(tmp_path))["thread_id"] == "pr_7_explorer_01"


def test_ok_path_tolerates_old_base_without_bug_items(tmp_path, monkeypatch):
    """老版本基座只给计数：退回计数并**如实说明**，不编造缺陷原文。"""
    _mission(tmp_path)
    monkeypatch.setattr(ra, "probe_provider", lambda: ("gemini", "stub"))

    def old_spawn(cmd, *, cwd, env, timeout, log_file):
        rd = Path(cwd) / "report_old"
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "result.json").write_text(
            json.dumps({"thread_id": "old", "bugs": 3}), encoding="utf-8")
        return 0

    monkeypatch.setattr(ra, "_spawn_bworld", old_spawn)
    p = ra.run_stage(project_dir=tmp_path)
    assert p["status"] == "ok" and p["bug_count"] == 3
    assert len(p["bugs"]) == 1 and "未提供原文" in p["bugs"][0]
    assert p["end_reason"] is None            # 老基座没这个字段 → 不猜


def test_find_bworld_result_picks_latest(tmp_path):
    for tid, mt in (("a", 1000), ("b", 2000)):
        d = tmp_path / f"report_{tid}"
        d.mkdir()
        f = d / "result.json"
        f.write_text("{}", encoding="utf-8")
        import os
        os.utime(f, (mt, mt))
    assert ra.find_bworld_result(tmp_path).parent.name == "report_b"
    assert ra.find_bworld_result(tmp_path / "empty") is None


# --------------------------------------------------------------------------- #
# CLI：真实进程边界（不需要 key，因为"缺任务"先于"查 key"判定）
# --------------------------------------------------------------------------- #
def _run_cli(args, cwd, env=None):
    return subprocess.run([sys.executable, str(ENTRY)] + args,
                          capture_output=True, text=True, encoding="utf-8",
                          cwd=str(cwd), env=env)


def test_cli_degrades_loudly_and_exits_zero(tmp_path):
    cat = tmp_path / "proj"
    (cat / "artifacts").mkdir(parents=True)
    r = _run_cli(["--project-dir", str(cat)], cwd=tmp_path)
    assert r.returncode == 0
    payload = json.loads((cat / "artifacts" / "agentic.json").read_text(encoding="utf-8"))
    assert payload["status"] == "degraded" and payload["reason"] == "not_configured"
    # 「降级必须出声」——在真实进程边界上验证 stderr
    assert "降级" in r.stderr and "未执行" in r.stderr
    assert "不得把本阶段当作已通过" in r.stderr


def test_cli_fail_on_degraded_returns_three(tmp_path):
    cat = tmp_path / "proj"
    (cat / "artifacts").mkdir(parents=True)
    r = _run_cli(["--project-dir", str(cat), "--fail-on-degraded"], cwd=tmp_path)
    assert r.returncode == 3        # 与"门禁失败=1"区分开：未执行 ≠ 失败
