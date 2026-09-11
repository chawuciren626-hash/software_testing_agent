"""project_manager 纯函数单测（不依赖 mall-admin / 网络）。"""
import sys
from pathlib import Path

import project_manager as pm


def test_parse_cases_meta_and_rows():
    md = """# 测试用例（由需求生成）
- 来源：sample_requirements.md
- 生成时间：2026-09-10 12:00
- 用例数：6

| id | 标题 | 模块 | 类型 | 优先级 | 前置 | 步骤 | 预期 | 可自动化 |
|---|---|---|---|---|---|---|---|---|
| REQ-001-F | 登录（功能） | 待定 | 功能 | P1 | 已部署 | 1.登录 | 成功 | pytest/Playwright |
| REQ-001-B | 登录（边界） | 待定 | 边界 | P2 | 准备 | 1.边界 | 正确 | 可 |
"""
    meta, rows = pm._parse_cases(md)
    assert meta["count"] == 6
    assert meta["source"] == "sample_requirements.md"
    assert len(rows) == 2
    assert rows[0]["id"] == "REQ-001-F"
    # 缺表格时应返回空行
    meta2, rows2 = pm._parse_cases("# 无表格\n- 用例数：0\n")
    assert rows2 == []


def test_cases_html_auto_badge_for_framework():
    md = """# 测试用例
- 用例数：1

| id | 标题 | 模块 | 类型 | 优先级 | 前置 | 步骤 | 预期 | 可自动化 |
|---|---|---|---|---|---|---|---|---|
| REQ-001-F | 登录（功能） | 待定 | 功能 | P1 | 已部署 | 1.登录 | 成功 | pytest/Playwright |
"""
    html, count = pm._cases_html(md)
    assert count == 1
    # 框架名（pytest/Playwright）应识别为"可自动化"绿色徽章
    assert "auto-yes" in html
    assert "可自动化" in html
    # 分组卡片已生成
    assert "req-card" in html


def test_cases_html_empty_fallback():
    html, count = pm._cases_html("完全不是表格的文本")
    assert count == 0
    assert "<pre>" in html


def test_step_report_gate_badge(tmp_path):
    # 成功门禁：all_pass=True -> "gate ok"
    pdir = tmp_path / "proj"
    (pdir / "artifacts").mkdir(parents=True)
    reg_ok = {
        "passed": 3, "total": 3, "failed": 0, "skipped": 0, "all_pass": True,
        "results": [{"name": "登录", "result": "PASS", "method": "POST",
                     "url": "http://x/login", "status_code": 200,
                     "expect": "200", "detail": ""}],
    }
    out_ok = pm._step_report("proj", pdir, None, reg_ok)
    text_ok = out_ok.read_text(encoding="utf-8")
    assert "gate ok" in text_ok
    assert "存在失败" not in text_ok

    # 失败门禁：all_pass=False -> "gate bad"
    reg_bad = dict(reg_ok, passed=2, failed=1, total=3, all_pass=False)
    reg_bad["results"][0] = dict(reg_bad["results"][0], result="FAIL", detail="code=500")
    out_bad = pm._step_report("proj", pdir, None, reg_bad)
    text_bad = out_bad.read_text(encoding="utf-8")
    assert "gate bad" in text_bad


def test_run_meta_roundtrip(tmp_path):
    pdir = tmp_path / "proj"
    (pdir / "artifacts").mkdir(parents=True)
    assert pm._read_run_meta(pdir) is None
    pm._write_run_meta(pdir, mode="智能体多步自审编排", use_llm=True, agentic=True,
                       lessons_injected=True, requirements=3, cases=9)
    m = pm._read_run_meta(pdir)
    assert m["mode"] == "智能体多步自审编排"
    assert m["lessons_injected"] is True and m["cases"] == 9 and m["requirements"] == 3
    assert (pdir / "artifacts" / pm.RUN_META_FILE).is_file()


def test_step_report_shows_generation_mode(tmp_path):
    pdir = tmp_path / "proj"
    (pdir / "artifacts").mkdir(parents=True)
    pm._write_run_meta(pdir, mode="LLM 增强", lessons_injected=True, cases=9)
    reg = {"passed": 1, "total": 1, "failed": 0, "skipped": 0, "all_pass": True, "results": []}
    txt = pm._step_report("proj", pdir, None, reg).read_text(encoding="utf-8")
    assert "生成模式" in txt and "LLM 增强" in txt and "注入历史易错点" in txt


def test_step_report_without_run_meta_has_no_mode_item(tmp_path):
    pdir = tmp_path / "proj"
    (pdir / "artifacts").mkdir(parents=True)
    reg = {"passed": 0, "total": 0, "failed": 0, "skipped": 0, "all_pass": False, "results": []}
    txt = pm._step_report("proj", pdir, None, reg).read_text(encoding="utf-8")
    assert "生成模式" not in txt


# ---------------------------------------------------------------------------
# ④ 性能与安全：报告呈现与"未执行"语义
# ---------------------------------------------------------------------------
def _write_perf_sec(pdir, all_pass=True, perf_skipped=False):
    import json as _json
    payload = {
        "project_id": "proj", "base_url": "http://localhost:8080",
        "generated_at": "2026-09-11 12:00:00",
        "baseline": {"ok": not perf_skipped, "reason": "基线登录成功，环境与凭据有效"},
        "all_pass": all_pass,
        "summary": "性能 通过（P95 20ms / 错误率 0.00% / 100 rps） · 安全 4 通过 / 0 失败",
        "perf": {"enabled": True, "passed": all_pass, "skipped": perf_skipped,
                 "reason": "环境不可达",
                 "config": {"users": 8, "iterations": 5, "warmup": 2, "thresholds": {}},
                 "target_source": "由 regression.yaml 派生",
                 "overall": {"samples": 40, "error_rate": 0.0, "p50_ms": 12.0,
                             "p95_ms": 20.0, "p99_ms": 25.0, "rps": 100.0},
                 "targets": []},
        "security": {"enabled": True, "passed_count": 4, "failed": 0, "warned": 2, "skipped": 0,
                     "reason": "4 项通过", "checks": []},
    }
    (pdir / "artifacts" / "perf_security.json").write_text(
        _json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_step_report_renders_perf_security_card(tmp_path):
    pdir = tmp_path / "proj"
    (pdir / "artifacts").mkdir(parents=True)
    _write_perf_sec(pdir)
    reg = {"passed": 1, "total": 1, "failed": 0, "skipped": 0, "all_pass": True,
           "results": [{"name": "登录", "method": "POST", "url": "/login",
                        "status_code": 200, "expect": 200, "result": "PASS"}]}
    txt = pm._step_report("proj", pdir, None, reg).read_text(encoding="utf-8")
    assert "性能与安全冒烟" in txt
    assert "性能指标" in txt and "安全检查" in txt
    assert "P95" in txt


def test_step_report_without_perf_security_has_no_card(tmp_path):
    pdir = tmp_path / "proj"
    (pdir / "artifacts").mkdir(parents=True)
    reg = {"passed": 1, "total": 1, "failed": 0, "skipped": 0, "all_pass": True,
           "results": [{"name": "登录", "result": "PASS"}]}
    txt = pm._step_report("proj", pdir, None, reg).read_text(encoding="utf-8")
    assert "性能与安全冒烟" not in txt


def test_step_report_marks_regression_not_run(tmp_path):
    """只跑了性能安全时，核心回归必须显示"尚未执行"，而不是"存在失败"（错误门禁结论）。"""
    pdir = tmp_path / "proj"
    (pdir / "artifacts").mkdir(parents=True)
    _write_perf_sec(pdir)
    txt = pm._step_report("proj", pdir, None, {}).read_text(encoding="utf-8")
    assert "尚未执行" in txt
    assert "存在失败" not in txt


def test_step_report_shows_perf_skipped_note(tmp_path):
    """环境不可达 → 性能为 SKIP，报告要说明原因，不能静默显示为通过。"""
    pdir = tmp_path / "proj"
    (pdir / "artifacts").mkdir(parents=True)
    _write_perf_sec(pdir, all_pass=False, perf_skipped=True)
    reg = {"passed": 1, "total": 1, "failed": 0, "skipped": 0, "all_pass": True,
           "results": [{"name": "登录", "result": "PASS"}]}
    txt = pm._step_report("proj", pdir, None, reg).read_text(encoding="utf-8")
    assert "未执行" in txt and "环境不可达" in txt
    assert "未通过" in txt


def test_read_perf_security_handles_missing_and_broken(tmp_path):
    pdir = tmp_path / "proj"
    (pdir / "artifacts").mkdir(parents=True)
    assert pm._read_perf_security(pdir) is None
    (pdir / "artifacts" / "perf_security.json").write_text("{bad json", encoding="utf-8")
    assert pm._read_perf_security(pdir) is None


def test_load_projects_skips_disabled(tmp_path, monkeypatch):
    # 准备两个项目目录，其中一个标记停用
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "project.yaml").write_text("project_id: a\n", encoding="utf-8")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "project.yaml").write_text("project_id: b\n", encoding="utf-8")
    (tmp_path / "b" / ".disabled").write_text("1", encoding="utf-8")

    monkeypatch.setattr(pm, "PROJECTS_DIR", tmp_path)
    # 默认跳过停用
    active = [p.name for p in pm.load_projects()]
    assert active == ["a"]
    # 含停用
    allp = [p.name for p in pm.load_projects(include_disabled=True)]
    assert set(allp) == {"a", "b"}


def test_run_pipeline_no_api_smoke(tmp_path):
    """S4 守护：统一流水线（无接口）能跑通并产出报告。自清理避免污染仓库。"""
    import subprocess as _sp
    from pathlib import Path as _P

    req = tmp_path / "req.md"
    req.write_text("1. 用户登录\n2. 管理员查看列表\n", encoding="utf-8")
    out = pm.run_pipeline(str(req), run_api=False)
    try:
        assert isinstance(out, _P)
        assert out.exists()
        # cases.md 已被生成逻辑覆盖，还原到 git 版本，保持仓库干净
    finally:
        try:
            out.unlink()
        except OSError:
            pass
        _sp.run(["git", "checkout", "--", "extensions/requirements_to_cases/cases.md"],
                cwd=_P(__file__).resolve().parent.parent,
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)

