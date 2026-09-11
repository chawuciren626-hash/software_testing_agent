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


# ---------------------------------------------------------------------------
# ⑤ Web UI 冒烟（project_manager 层）
# ---------------------------------------------------------------------------
def _write_web(pdir, all_pass=True, **over):
    import json as _json
    payload = {
        "project_id": "proj", "base_url": "http://localhost:8090",
        "browser": "chromium", "headless": True,
        "generated_at": "2026-09-11 12:00:00",
        "baseline": {"ok": True, "reason": "可达（HTTP 200，title='自检页'）"},
        "config_issues": [], "flaky": 0, "assertions": 2,
        "all_pass": all_pass, "total": 2, "passed": 2, "failed": 0, "skipped": 0,
        "summary": ("场景 2 通过 / 0 失败 / 0 跳过" if all_pass
                    else "环境不可达（或浏览器无法启动），全部场景跳过，按未通过处理（防 CI 假绿）"),
        "scenarios": [
            {"name": "Web 首页可访问", "tags": ["smoke"], "result": "PASS", "reason": "",
             "assertions": 1, "attempts": 1, "flaky": False, "duration_ms": 500,
             "failed_step": None, "screenshots": [], "repro": ""},
            {"name": "登录后进入工作台", "tags": ["smoke"], "result": "PASS", "reason": "",
             "assertions": 1, "attempts": 1, "flaky": False, "duration_ms": 600,
             "failed_step": None, "screenshots": [], "repro": ""},
        ],
    }
    payload.update(over)
    (pdir / "artifacts" / "web.json").write_text(
        _json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


def test_step_report_renders_web_card(tmp_path):
    pdir = tmp_path / "proj"
    (pdir / "artifacts").mkdir(parents=True)
    _write_web(pdir)
    reg = {"passed": 1, "total": 1, "failed": 0, "skipped": 0, "all_pass": True,
           "results": [{"name": "登录", "result": "PASS"}]}
    txt = pm._step_report("proj", pdir, None, reg).read_text(encoding="utf-8")
    assert "Web UI 冒烟" in txt
    assert "Web 首页可访问" in txt
    assert "登录后进入工作台" in txt
    assert "http://localhost:8090" in txt
    assert "Web UI" in txt          # 摘要条里的独立门禁项


def test_step_report_without_web_has_no_card(tmp_path):
    pdir = tmp_path / "proj"
    (pdir / "artifacts").mkdir(parents=True)
    reg = {"passed": 1, "total": 1, "failed": 0, "skipped": 0, "all_pass": True,
           "results": [{"name": "登录", "result": "PASS"}]}
    txt = pm._step_report("proj", pdir, None, reg).read_text(encoding="utf-8")
    assert "Web UI 冒烟" not in txt


def test_web_card_links_are_relative_to_artifacts_dir(tmp_path):
    """证据链接必须相对 report.html（同在 artifacts/）——拼成 artifacts/artifacts/... 会 404。"""
    pdir = tmp_path / "proj"
    (pdir / "artifacts").mkdir(parents=True)
    _write_web(pdir, all_pass=False, failed=1, passed=1,
               scenarios=[
                   {"name": "下单", "tags": ["order"], "result": "FAIL",
                    "reason": "文案不符", "assertions": 1, "attempts": 1, "flaky": False,
                    "duration_ms": 900,
                    "failed_step": {"index": 4, "action": "expect_text", "target": "#toast"},
                    "screenshots": [r"artifacts\web_shots\下单-FAIL.png"],
                    "repro": r"artifacts\web_repro_下单.spec.ts"},
               ])
    reg = {"passed": 1, "total": 1, "failed": 0, "skipped": 0, "all_pass": True,
           "results": [{"name": "登录", "result": "PASS"}]}
    txt = pm._step_report("proj", pdir, None, reg).read_text(encoding="utf-8")
    assert "href='web_shots/下单-FAIL.png'" in txt
    assert "href='web_repro_下单.spec.ts'" in txt
    assert "artifacts/web_shots" not in txt
    assert "artifacts\\web_shots" not in txt
    # 失败步骤要写出来，便于直接定位
    assert "第4步 expect_text" in txt


def test_web_card_reports_config_issues_not_silently(tmp_path):
    """配置问题必须显式列出：静默跳过会让门禁悄悄变松。"""
    pdir = tmp_path / "proj"
    (pdir / "artifacts").mkdir(parents=True)
    _write_web(pdir, all_pass=False, failed=0, passed=0, skipped=1,
               config_issues=["[登录] [config] 第 2 步（click）：脆弱定位器 `div:nth-child(3)`"])
    reg = {"passed": 1, "total": 1, "failed": 0, "skipped": 0, "all_pass": True,
           "results": [{"name": "登录", "result": "PASS"}]}
    txt = pm._step_report("proj", pdir, None, reg).read_text(encoding="utf-8")
    assert "配置/门禁问题 1 项" in txt
    assert "脆弱定位器" in txt


def test_web_card_marks_skipped_scenarios(tmp_path):
    pdir = tmp_path / "proj"
    (pdir / "artifacts").mkdir(parents=True)
    _write_web(pdir, all_pass=False, passed=0, failed=0, skipped=2,
               baseline={"ok": False, "reason": "环境不可达：ERR_CONNECTION_REFUSED"},
               scenarios=[
                   {"name": "Web 首页可访问", "tags": ["smoke"], "result": "SKIP",
                    "reason": "未执行：环境不可达", "assertions": 0, "attempts": 0,
                    "flaky": False, "duration_ms": 0, "failed_step": None,
                    "screenshots": [], "repro": ""},
               ])
    reg = {"passed": 1, "total": 1, "failed": 0, "skipped": 0, "all_pass": True,
           "results": [{"name": "登录", "result": "PASS"}]}
    txt = pm._step_report("proj", pdir, None, reg).read_text(encoding="utf-8")
    assert "基线未通过" in txt
    assert "不计为产品缺陷" in txt
    assert "跳过" in txt


def test_read_web_handles_missing_and_broken(tmp_path):
    pdir = tmp_path / "proj"
    (pdir / "artifacts").mkdir(parents=True)
    assert pm._read_web(pdir) is None
    (pdir / "artifacts" / "web.json").write_text("{bad json", encoding="utf-8")
    assert pm._read_web(pdir) is None


def test_create_generates_parseable_web_yaml(tmp_path, monkeypatch):
    """create 生成的 web.yaml 必须能真解析，且 {{username}} 占位符不能被 format 吃掉。"""
    import yaml
    monkeypatch.setattr(pm, "PROJECTS_DIR", tmp_path)
    args = pm.build_parser().parse_args([
        "create", "--id", "wp", "--name", "W", "--base-url", "http://h:9",
        "--owner", "o", "--description", "d", "--auth-type", "form",
        "--login-url", "/login", "--user-env", "U", "--pass-env", "P",
    ])
    pm.cmd_create(args)
    wy = tmp_path / "wp" / "web.yaml"
    assert wy.is_file()
    text = wy.read_text(encoding="utf-8")
    assert "{{username}}" in text and "{{password}}" in text   # 未被转义成 {username}
    data = yaml.safe_load(text)
    assert data["project_id"] == "wp"
    assert data["web"]["scenarios"][0]["name"]
    assert data["web"]["viewport"] == {"width": 1440, "height": 900}
    # project.yaml 里也留了 Web 地址入口（注释形式，不影响解析）
    pj = yaml.safe_load((tmp_path / "wp" / "project.yaml").read_text(encoding="utf-8"))
    assert pj["web_file"] == "web.yaml"


def test_web_yaml_template_placeholders_are_replaced():
    text = pm.WEB_YAML_TMPL.replace("{pid}", "pid-x")
    assert "{pid}" not in text
    assert "pid-x" in text


def test_dashboard_shows_web_column(tmp_path, monkeypatch):
    """看板要带 Web UI 列，否则多项目下 Web 门禁结果无处可看。"""
    pdirs = tmp_path / "projects"
    for pid, with_web in (("a", True), ("b", False)):
        d = pdirs / pid
        d.mkdir(parents=True)
        (d / "project.yaml").write_text(f"project_id: {pid}\nname: {pid}\n", encoding="utf-8")
        (d / "artifacts").mkdir()
        if with_web:
            _write_web(d, all_pass=True)
    monkeypatch.setattr(pm, "PROJECTS_DIR", pdirs)
    monkeypatch.setattr(pm, "ROOT", tmp_path)
    pm.cmd_dashboard(pm.build_parser().parse_args(["dashboard"]))
    html = (tmp_path / "projects_dashboard.html").read_text(encoding="utf-8")
    assert "Web UI" in html
    assert "2通过/0失败" in html     # a 项目
    assert "未执行" in html          # b 项目


def test_cli_has_web_subcommand():
    ap = pm.build_parser()
    args = ap.parse_args(["web", "p1", "--only", "smoke", "--headed", "--browser", "firefox"])
    assert args.func is pm.cmd_web
    assert args.only == "smoke" and args.headed is True and args.browser == "firefox"


def test_run_parser_has_web_flag():
    args = pm.build_parser().parse_args(["run", "p1", "--web"])
    assert args.web is True


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


# ---------- 用例结构质量分（L3 评测常态化） ----------

_CASES_MD = """| id | 标题 | 模块 | 类型 | 优先级 | 前置 | 步骤 | 预期 | 可自动化 |
|---|---|---|---|---|---|---|---|---|
| REQ-001-F | 登录（功能） | 认证 | 功能 | P1 | 已部署 | 1. 输入 admin 与密码 macro123 2. 点击登录 | 返回 200，data.token 非空 | 是 |
| REQ-001-B | 登录（边界） | 认证 | 边界 | P2 | 无 | 1. 输入长度为 6 个字符的密码 2. 提交 | 提示「密码至少 8 位」 | 是 |
| REQ-001-N | 登录（异常） | 认证 | 异常 | P1 | 无 | 1. 输入错误密码 2. 点击登录 | 返回 500 且提示「用户名或密码错误」 | 是 |
"""


def test_step_quality_writes_files_and_history(tmp_path):
    pdir = tmp_path / "p"
    (pdir := pdir).mkdir()
    pm._step_quality(pdir, _CASES_MD, requirement_count=1, mode="规则版")
    assert (pdir / "artifacts" / "quality.json").is_file()
    assert (pdir / "artifacts" / "quality_history.jsonl").is_file()
    q = pm._read_quality(pdir)
    assert q["total"] is not None and q["counts"]["cases"] == 3


def test_step_quality_second_run_has_delta(tmp_path):
    """第二次跑要能算环比 —— 用例被砍掉一半时分数必须下跌（趋势信号可用）。"""
    pdir = tmp_path / "p"
    pdir.mkdir()
    pm._step_quality(pdir, _CASES_MD, requirement_count=1)
    first = pm._read_quality(pdir)["total"]
    assert first == 100
    # 表头(1) + 分隔行(2) + 只留第一条用例(3)：覆盖率不变，但三类齐备从 100 掉到 33
    one_case = "\n".join(_CASES_MD.splitlines()[:4])
    pm._step_quality(pdir, one_case, requirement_count=1)
    q = pm._read_quality(pdir)
    assert q["total"] < first and q["delta"] == q["total"] - first
    assert len(q["history"]) == 2


def test_step_quality_no_cases_returns_none(tmp_path):
    pdir = tmp_path / "p"
    pdir.mkdir()
    assert pm._step_quality(pdir, "", requirement_count=1) is None
    assert pm._step_quality(pdir, None, requirement_count=1) is None


def test_quality_card_discloses_it_is_not_quality(tmp_path):
    """卡片必须写明"结构分 ≠ 用例质量"，否则满分会被误读成质量结论。"""
    pdir = tmp_path / "p"
    pdir.mkdir()
    pm._step_quality(pdir, _CASES_MD, requirement_count=1)
    card = pm._quality_card_html(pm._read_quality(pdir))
    assert "形式完整性" in card and "不代表用例质量好坏" in card
    assert "q-bar-fill" in card          # 维度条渲染出来了
    assert "环比" in card


def test_report_includes_quality_card(tmp_path, monkeypatch):
    """报告里要出现质量卡片与摘要项（跑过才有）。"""
    pdir = tmp_path / "p"
    pdir.mkdir()
    (pdir / "project.yaml").write_text("project_id: demo\n", encoding="utf-8")
    pm._step_quality(pdir, _CASES_MD, requirement_count=1)
    out = pm._step_report("demo", pdir, None, {"results": [], "total": 0,
                                              "passed": 0, "failed": 0, "skipped": 0})
    html = out.read_text(encoding="utf-8")
    assert "用例结构质量分" in html and "用例结构分" in html
