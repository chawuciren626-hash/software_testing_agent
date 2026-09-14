"""AI 探索接入 `project_manager` 的接线守护测试（审阅报告 §3.3 / §7 序 6）。

这一层要钉死的是**接入纪律**（不是探索逻辑本身）：
1. **子进程 + 契约**：`project_manager` 只通过 `run_agentic.py` 的子进程与
   `artifacts/agentic.json` 打交道，不 import 基座（决策 D2）；
2. **三层兜底**：入口崩了/契约没写出来 → `project_manager` 自己合成 degraded，
   **绝不**默默返回空；
3. **不许读到上一轮的旧契约**（跑之前先清），否则"这次没跑"会被伪装成"跑成功了"；
4. **降级不改退出码**（`run --explore`）；显式子命令 `explore` 用 3 区分"未执行"与"失败"；
5. 报告链要看得见（卡片 + summary 项），且降解原因**要在报告里写出来**。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

import project_manager as pm


# --------------------------------------------------------------------------- #
# 存在性与解释器解析
# --------------------------------------------------------------------------- #
def test_wiring_points_exist():
    for name in ("_step_agentic", "_read_agentic", "_agent_python",
                 "_agentic_contract", "_agentic_card_html", "_agentic_href",
                 "cmd_explore"):
        assert hasattr(pm, name), f"缺少接线点 {name}"


def test_agent_python_prefers_sta_agent_python(monkeypatch):
    """探索可以跑在**独立环境**里（这正是 D2 的目的），故允许单独指定解释器。"""
    monkeypatch.setenv("STA_AGENT_PYTHON", "/custom/python")
    assert pm._agent_python() == "/custom/python"


def test_agent_python_falls_back_to_python_exe(monkeypatch):
    monkeypatch.delenv("STA_AGENT_PYTHON", raising=False)
    assert pm._agent_python() == pm.python_exe()


def test_cli_surface_has_explore_flag_and_subcommand():
    ap = pm.build_parser()
    # `run --explore`
    ns = ap.parse_args(["run", "demo", "--explore"])
    assert ns.explore is True and ns.explore_max_steps == 30
    # `explore` 子命令
    ex = ap.parse_args(["explore", "demo", "--timeout", "5", "--json"])
    assert ex.cmd == "explore" and ex.timeout == 5.0 and ex.json is True
    assert ex.func is pm.cmd_explore


# --------------------------------------------------------------------------- #
# 子进程契约 + 兜底
# --------------------------------------------------------------------------- #
def test_step_agentic_writes_degraded_contract_without_mission(tmp_path):
    """真实子进程边界：缺 mission.yaml → degraded(not_configured) 契约落盘。"""
    pdir = tmp_path / "proj"
    (pdir / "artifacts").mkdir(parents=True)
    p = pm._step_agentic(pdir)
    assert p["status"] == "degraded" and p["reason"] == "not_configured"
    on_disk = pm._read_agentic(pdir)
    assert on_disk and on_disk["reason"] == "not_configured"


def test_step_agentic_discards_stale_contract_when_entry_writes_nothing(tmp_path, monkeypatch):
    """跑之前清旧契约：否则**入口崩溃（什么都没写）时**会读到上一轮的成功，
    把"这次没跑"伪装成"这次跑成功了"。

    关键在于让"入口写不出契约"真的发生（解释器不存在），否则降级本身会覆盖旧文件，
    这条测试就会退化成"覆盖测试"——**变异验证正是这样发现它原本没在守着**。
    """
    pdir = tmp_path / "proj"
    (pdir / "artifacts").mkdir(parents=True)
    (pdir / "mission.yaml").write_text("missions: []\n", encoding="utf-8")
    stale = pm._agentic_contract().build_ok(provider="claude", thread_id="OLD", bugs=["old"])
    pm._agentic_contract().write_result(pdir / "artifacts" / pm.AGENTIC_FILE, stale)
    assert pm._read_agentic(pdir)["thread_id"] == "OLD"        # 前提：旧契约确实在

    monkeypatch.setenv("STA_AGENT_PYTHON", str(tmp_path / "no_such_python"))
    p = pm._step_agentic(pdir)

    assert p["status"] == "degraded" and p["reason"] == "error"
    assert p["thread_id"] is None                    # 绝不能是上一轮的 OLD
    assert "OLD" not in json.dumps(pm._read_agentic(pdir), ensure_ascii=False)


def test_step_agentic_overwrites_stale_contract_on_real_run(tmp_path):
    """降级也会覆盖旧契约（与上一条互补：一个是"写不出"，一个是"写得出"）。"""
    pdir = tmp_path / "proj"
    (pdir / "artifacts").mkdir(parents=True)
    stale = pm._agentic_contract().build_ok(provider="claude", thread_id="OLD", bugs=["old"])
    pm._agentic_contract().write_result(pdir / "artifacts" / pm.AGENTIC_FILE, stale)

    p = pm._step_agentic(pdir)          # 无 mission → 必走降级并落盘
    assert p["status"] == "degraded"
    assert pm._read_agentic(pdir)["thread_id"] is None


def test_step_agentic_synthesizes_degraded_when_entry_cannot_start(tmp_path, monkeypatch):
    """② 进程级兜底：解释器都不存在 → 合成 degraded，而不是返回空。"""
    pdir = tmp_path / "proj"
    (pdir / "artifacts").mkdir(parents=True)
    (pdir / "mission.yaml").write_text("missions: []\n", encoding="utf-8")
    monkeypatch.setenv("STA_AGENT_PYTHON", str(tmp_path / "no_such_python"))

    p = pm._step_agentic(pdir)
    assert p["status"] == "degraded" and p["reason"] == "error"
    assert "未能从独立入口取得契约" in p["message"]
    # 兜底结果**也要落盘**：否则报告/看板读不到这一环
    assert pm._read_agentic(pdir)["reason"] == "error"


def test_step_agentic_missing_mission_is_recorded_not_silent(tmp_path):
    """契约必须在场 —— 报告才看得出"少了这一环"，而不是"没这回事"。"""
    pdir = tmp_path / "proj"
    (pdir / "artifacts").mkdir(parents=True)
    pm._step_agentic(pdir)
    assert (pdir / "artifacts" / "agentic.json").is_file()


def test_read_agentic_tolerates_broken_json(tmp_path):
    pdir = tmp_path / "proj"
    (pdir / "artifacts").mkdir(parents=True)
    assert pm._read_agentic(pdir) is None
    (pdir / "artifacts" / "agentic.json").write_text("{broken", encoding="utf-8")
    assert pm._read_agentic(pdir) is None
    (pdir / "artifacts" / "agentic.json").write_text("[1]", encoding="utf-8")
    assert pm._read_agentic(pdir) is None


# --------------------------------------------------------------------------- #
# 报告链：卡片 / 摘要 / 链接
# --------------------------------------------------------------------------- #
def test_agentic_href_converts_to_artifacts_relative(tmp_path):
    pdir = tmp_path / "proj"
    (pdir / "artifacts").mkdir(parents=True)
    assert pm._agentic_href(pdir, str(pdir / "artifacts" / "agentic.log")) == "agentic.log"
    assert pm._agentic_href(pdir, str(pdir / "artifacts" / "agentic_run" / "report_x")) \
        == str(Path("agentic_run") / "report_x")
    # 指向 artifacts 之外的（本就不该链接）→ None
    assert pm._agentic_href(pdir, str(tmp_path / "elsewhere.log")) is None
    assert pm._agentic_href(pdir, None) is None


def test_agentic_card_shows_degrade_reason_and_forbids_green(tmp_path):
    pdir = tmp_path / "proj"
    (pdir / "artifacts").mkdir(parents=True)
    ag = pm._agentic_contract().build_degraded(
        pm._agentic_contract().DegradeReason.NO_LLM_KEY, "缺 key")
    html = pm._agentic_card_html(pdir, ag)
    assert "已降级" in html and "缺少 LLM 凭据" in html
    assert "未执行" in html and "不等于" in html


def test_agentic_card_shows_findings_on_success(tmp_path):
    pdir = tmp_path / "proj"
    (pdir / "artifacts").mkdir(parents=True)
    ag = pm._agentic_contract().build_ok(provider="claude", thread_id="t1",
                                        end_reason="completed", goal_verdict="achieved",
                                        actions=4, bugs=["按钮无响应"])
    html = pm._agentic_card_html(pdir, ag)
    assert "已执行" in html and "按钮无响应" in html and "completed" in html


def test_step_report_embeds_agentic_card_and_summary(tmp_path):
    pdir = tmp_path / "proj"
    (pdir / "artifacts").mkdir(parents=True)
    pm._step_agentic(pdir)          # 写一份 degraded 契约
    reg = {"total": 0, "passed": 0, "failed": 0, "all_pass": False, "results": []}
    out = pm._step_report("demo", pdir, None, reg)
    html = out.read_text(encoding="utf-8")
    assert "AI 探索测试（可降级阶段）" in html
    assert "AI 探索" in html and "已降级" in html


def test_step_report_without_agentic_artifact_has_no_card(tmp_path):
    """没跑过就不该凭空出现卡片（与 ④/⑤ 卡片同一口径）。"""
    pdir = tmp_path / "proj"
    (pdir / "artifacts").mkdir(parents=True)
    reg = {"total": 0, "passed": 0, "failed": 0, "all_pass": False, "results": []}
    html = pm._step_report("demo", pdir, None, reg).read_text(encoding="utf-8")
    assert "AI 探索测试（可降级阶段）" not in html


# --------------------------------------------------------------------------- #
# 退出码语义：未执行（3）≠ 门禁失败（1）
# --------------------------------------------------------------------------- #
def _ns(**kw):
    base = dict(id="demo", mission=None, max_steps=30, timeout=None,
                headed=False, json=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_cmd_explore_exits_three_when_degraded(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "PROJECTS_DIR", tmp_path)
    pdir = tmp_path / "demo"
    (pdir / "artifacts").mkdir(parents=True)
    (pdir / "project.yaml").write_text(
        "id: demo\nenv:\n  base_url: http://localhost:8080\n", encoding="utf-8")

    with pytest.raises(SystemExit) as ei:
        pm.cmd_explore(_ns())
    assert ei.value.code == 3                       # 未执行 ≠ 失败
    assert (pdir / "artifacts" / "agentic.json").is_file()
    # 留案到 run_meta（增量更新），报告也刷新
    meta = pm._read_run_meta(pdir)
    assert meta and meta["agentic"]["executed"] is False
    assert meta["agentic"]["reason"] == "not_configured"
    assert (pdir / "artifacts" / "report.html").is_file()
