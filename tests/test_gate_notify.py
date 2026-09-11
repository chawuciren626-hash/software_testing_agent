"""门禁结果摘要与通知（extensions/reporting/gate_notify.py）测试。

重点验证**判定口径**而不是排版：
- 未执行 ≠ 通过（假绿防线）
- 未配置 ≠ 未通过（假红防线）
- 结论优先级：未通过 > 未执行 > 通过
- 无凭据可跑（--dry-run 不发通知，否则这段代码只能靠"发一次看看"来测）
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import gate_notify as gn


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _mk_project(root: Path, pid: str, *, name: str = "演示项目",
                regression: bool = True, web: bool = False,
                ps_in_yaml: bool = False) -> Path:
    """建一个最小可用的项目目录。"""
    pdir = root / pid
    (pdir / "artifacts").mkdir(parents=True)
    lines = [f"project_id: {pid}", f"name: {name}",
             "env:", "  base_url: http://127.0.0.1:1"]
    if ps_in_yaml:
        lines += ["perf_security:", "  users: 2"]
    (pdir / "project.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if regression:
        (pdir / "regression.yaml").write_text("project_id: %s\nscenarios: []\n" % pid,
                                              encoding="utf-8")
    if web:
        (pdir / "web.yaml").write_text("web:\n  scenarios: []\n", encoding="utf-8")
    return pdir


def _write_artifact(pdir: Path, fname: str, *, all_pass: bool,
                    summary: str = "") -> None:
    (pdir / "artifacts" / fname).write_text(
        json.dumps({"all_pass": all_pass, "summary": summary}, ensure_ascii=False),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# 基础读写
# --------------------------------------------------------------------------- #
def test_read_json_missing_and_broken(tmp_path):
    assert gn._read_json(tmp_path / "nope.json") is None
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert gn._read_json(tmp_path / "bad.json") is None


def test_read_json_rejects_non_dict(tmp_path):
    (tmp_path / "arr.json").write_text("[1,2,3]", encoding="utf-8")
    assert gn._read_json(tmp_path / "arr.json") is None


def test_load_project_yaml_falls_back_without_pyyaml(tmp_path, monkeypatch):
    """没有 pyyaml 时也不能崩：至少能把 name 取出来（CI 通知作业可能不装依赖）。"""
    p = _mk_project(tmp_path, "p1", name="降级解析项目")
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "yaml":
            raise ImportError("no yaml")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    meta = gn._load_project_yaml(p / "project.yaml")
    assert meta["name"] == "降级解析项目"


# --------------------------------------------------------------------------- #
# 「是否已配置」判定 —— 未配置不参与门禁，避免无谓红
# --------------------------------------------------------------------------- #
def test_is_configured_by_decl_file(tmp_path):
    p = _mk_project(tmp_path, "p", regression=True, web=False)
    assert gn._is_configured(p, "regression.yaml", "regression") is True
    assert gn._is_configured(p, "web.yaml", "web") is False


def test_is_configured_perf_security_derives_from_regression(tmp_path):
    p = _mk_project(tmp_path, "p", regression=True)
    assert gn._is_configured(p, None, "perf_security") is True


def test_is_configured_perf_security_via_project_yaml(tmp_path):
    p = _mk_project(tmp_path, "p", regression=False, ps_in_yaml=True)
    assert gn._is_configured(p, None, "perf_security") is True


def test_is_configured_false_when_nothing_declared(tmp_path):
    p = _mk_project(tmp_path, "p", regression=False)
    assert gn._is_configured(p, None, "perf_security") is False


# --------------------------------------------------------------------------- #
# 结论优先级
# --------------------------------------------------------------------------- #
def test_verdict_fail_beats_not_run():
    gates = {"a": {"status": "pass"}, "b": {"status": "not_run"},
             "c": {"status": "fail"}}
    assert gn._verdict(gates) == ("fail", "存在未通过的门禁")


def test_verdict_not_run_is_not_pass():
    """未执行不能算通过 —— 这是本模块最重要的一条防线。"""
    gates = {"a": {"status": "pass"}, "b": {"status": "not_run"}}
    assert gn._verdict(gates) == ("not_run", "有门禁尚未执行（未执行 ≠ 通过）")


def test_verdict_all_pass():
    gates = {"a": {"status": "pass"}, "b": {"status": "pass"}}
    code, reason = gn._verdict(gates)
    assert code == "pass" and reason == ""


def test_verdict_only_not_configured_counts_as_not_run():
    """一个门禁都没配置 → 整体'未执行'，但不判红（没有门禁 ≠ 门禁失败）。"""
    gates = {"a": {"status": "not_configured"}, "b": {"status": "not_configured"}}
    assert gn._verdict(gates) == ("not_run", "没有已配置的门禁")


def test_verdict_ignores_not_configured_when_others_pass():
    gates = {"a": {"status": "pass"}, "b": {"status": "not_configured"}}
    assert gn._verdict(gates)[0] == "pass"


# --------------------------------------------------------------------------- #
# 采集
# --------------------------------------------------------------------------- #
def test_collect_gates_missing_dir(tmp_path):
    assert gn.collect_gates(tmp_path / "nope") == []


def test_collect_gates_skips_dir_without_project_yaml(tmp_path):
    (tmp_path / "not-a-project").mkdir()
    assert gn.collect_gates(tmp_path) == []


def test_collect_gates_marks_not_configured_web(tmp_path):
    p = _mk_project(tmp_path, "p1", regression=True, web=False)
    _write_artifact(p, "regression.json", all_pass=True)
    rows = gn.collect_gates(tmp_path)
    assert len(rows) == 1
    assert rows[0]["gates"]["web"]["status"] == "not_configured"
    # 未配置的 Web 不该拖累结论
    assert rows[0]["verdict"] == "not_run"  # perf_security 未执行


def test_collect_gates_three_states(tmp_path):
    p = _mk_project(tmp_path, "p1", regression=True, web=True)
    _write_artifact(p, "regression.json", all_pass=True)
    _write_artifact(p, "perf_security.json", all_pass=False, summary="2 项失败")
    # web.yaml 有，但没跑过 → 未执行
    rows = gn.collect_gates(tmp_path)
    g = rows[0]["gates"]
    assert g["regression"]["status"] == "pass"
    assert g["perf_security"]["status"] == "fail"
    assert g["perf_security"]["summary"] == "2 项失败"
    assert g["web"]["status"] == "not_run"
    assert rows[0]["verdict"] == "fail"


# --------------------------------------------------------------------------- #
# 汇总与渲染
# --------------------------------------------------------------------------- #
def test_summarize_no_rows_is_not_pass():
    """一个项目都没有 → 不能算通过（多半是流水线跑错目录了）。"""
    s = gn.summarize([])
    assert s["all_pass"] is False and s["projects"] == 0


def test_summarize_counts(tmp_path):
    p = _mk_project(tmp_path, "p1", regression=True, web=True)
    _write_artifact(p, "regression.json", all_pass=True)
    _write_artifact(p, "perf_security.json", all_pass=True)
    _write_artifact(p, "web.json", all_pass=True)
    s = gn.summarize(gn.collect_gates(tmp_path))
    assert s["pass"] == 1 and s["fail"] == 0 and s["not_run"] == 0
    assert s["all_pass"] is True


def test_render_text_no_projects_warns():
    txt = gn.render_text([])
    assert "未发现任何已接入项目" in txt
    assert "未执行 ≠ 通过" in txt


def test_render_text_shows_fail_summary(tmp_path):
    p = _mk_project(tmp_path, "p1", regression=True)
    _write_artifact(p, "regression.json", all_pass=False, summary="1 通过 / 2 失败")
    txt = gn.render_text(gn.collect_gates(tmp_path), title="T")
    assert txt.startswith("T")
    assert "1 通过 / 2 失败" in txt
    assert "门禁未达成" in txt


def test_render_text_all_pass(tmp_path):
    p = _mk_project(tmp_path, "p1", regression=True)
    _write_artifact(p, "regression.json", all_pass=True)
    _write_artifact(p, "perf_security.json", all_pass=True)
    txt = gn.render_text(gn.collect_gates(tmp_path))
    assert "全部门禁通过" in txt


# --------------------------------------------------------------------------- #
# 发送与 CLI
# --------------------------------------------------------------------------- #
def test_send_dry_run_does_not_touch_notifiers(monkeypatch):
    """dry-run 绝不能真的发出去 —— 否则本地预览/单测会骚扰收件人。"""
    calls = []
    monkeypatch.setattr(gn.notify_dingtalk, "send", lambda *a, **k: calls.append("ding"))
    monkeypatch.setattr(gn.notify_email, "send", lambda *a, **k: calls.append("mail"))
    gn._send("hi", dry_run=True)
    assert calls == []


def test_send_calls_both_notifiers(monkeypatch):
    calls = []
    monkeypatch.setattr(gn.notify_dingtalk, "send", lambda *a, **k: calls.append("ding"))
    monkeypatch.setattr(gn.notify_email, "send", lambda *a, **k: calls.append("mail"))
    gn._send("hi", dry_run=False)
    assert calls == ["ding", "mail"]


def test_collect_gates_project_filter(tmp_path):
    """CI 只跑了一个项目时必须能只看它 —— 否则其余未跑过的项目全记「未执行」→ 永远红。"""
    p1 = _mk_project(tmp_path, "gate-proj", regression=True)
    _write_artifact(p1, "regression.json", all_pass=True)
    _write_artifact(p1, "perf_security.json", all_pass=True)
    _mk_project(tmp_path, "never-ran", regression=True)  # 没产物

    assert len(gn.collect_gates(tmp_path)) == 2
    rows = gn.collect_gates(tmp_path, only=["gate-proj"])
    assert len(rows) == 1 and rows[0]["pid"] == "gate-proj"
    assert gn.summarize(rows)["all_pass"] is True
    # 反例：不加过滤时，未跑过的项目会把整体结论拖成"未执行"
    assert gn.summarize(gn.collect_gates(tmp_path))["all_pass"] is False


def test_project_filter_matches_project_id_not_only_dirname(tmp_path):
    """project_id 与目录名不一致时（手工搬过目录就会这样），两种写法都要能命中。"""
    p = _mk_project(tmp_path, "dir-name", regression=True)
    text = (p / "project.yaml").read_text(encoding="utf-8").replace(
        "project_id: dir-name", "project_id: custom-id")
    (p / "project.yaml").write_text(text, encoding="utf-8")
    _write_artifact(p, "regression.json", all_pass=True)
    _write_artifact(p, "perf_security.json", all_pass=True)

    assert len(gn.collect_gates(tmp_path, only=["custom-id"])) == 1
    assert len(gn.collect_gates(tmp_path, only=["dir-name"])) == 1
    assert gn.collect_gates(tmp_path, only=["custom-id"])[0]["pid"] == "custom-id"


def test_main_dry_run_exit_code(tmp_path):
    p = _mk_project(tmp_path, "p1", regression=True)
    _write_artifact(p, "regression.json", all_pass=True)
    _write_artifact(p, "perf_security.json", all_pass=True)
    assert gn.main(["--projects-dir", str(tmp_path), "--dry-run"]) == 0
    assert gn.main(["--projects-dir", str(tmp_path), "--dry-run", "--fail-on-gate"]) == 0


def test_main_fail_on_gate_returns_one(tmp_path):
    p = _mk_project(tmp_path, "p1", regression=True)
    _write_artifact(p, "regression.json", all_pass=False)
    assert gn.main(["--projects-dir", str(tmp_path), "--dry-run", "--fail-on-gate"]) == 1


def test_main_out_writes_summary(tmp_path):
    p = _mk_project(tmp_path, "p1", regression=True)
    _write_artifact(p, "regression.json", all_pass=True)
    _write_artifact(p, "perf_security.json", all_pass=True)
    out = tmp_path / "summary.txt"
    assert gn.main(["--projects-dir", str(tmp_path / "nope"), "--dry-run",
                    "--out", str(out), "--fail-on-gate"]) == 1  # 空目录：门禁未达成
    assert out.is_file() and "未发现任何已接入项目" in out.read_text(encoding="utf-8")


def test_main_text_file_sends_content(tmp_path, monkeypatch):
    """CI 通知作业复用上游摘要：不能在新 checkout 上重算出「无项目」。"""
    f = tmp_path / "upstream.txt"
    f.write_text("上游算好的结论", encoding="utf-8")
    sent = []
    monkeypatch.setattr(gn, "_send", lambda text, dry_run, subject="": sent.append(text))
    assert gn.main(["--text-file", str(f)]) == 0
    assert sent == ["上游算好的结论"]


def test_main_text_file_missing_returns_one(tmp_path):
    assert gn.main(["--text-file", str(tmp_path / "nope.txt")]) == 1


# --------------------------------------------------------------------------- #
# 停用项目（.disabled）—— 必须与看板/控制台口径一致，否则是"幽灵红"
# --------------------------------------------------------------------------- #
def _disable(pdir: Path) -> None:
    (pdir / ".disabled").write_text("", encoding="utf-8")


def test_disabled_project_excluded_like_the_dashboard(tmp_path):
    """停用项目带着历史失败产物时，不能继续把门禁拖红 —— 而项目页又看不到它。"""
    keep = _mk_project(tmp_path, "active", regression=True)
    _write_artifact(keep, "regression.json", all_pass=True)
    _write_artifact(keep, "perf_security.json", all_pass=True)

    retired = _mk_project(tmp_path, "retired", regression=True)
    _write_artifact(retired, "regression.json", all_pass=False, summary="早就没维护了")
    _disable(retired)

    rows = gn.collect_gates(tmp_path)
    assert [r["pid"] for r in rows] == ["active"]
    assert gn.summarize(rows, disabled=1)["all_pass"] is True
    # 不加过滤时停用项目会把它拖红 —— 这正是修之前的行为
    assert gn.summarize(gn.collect_gates(tmp_path, include_disabled=True))["all_pass"] is False


def test_disabled_projects_are_listed_not_silently_hidden(tmp_path):
    """跳过可以，静默不行 —— 摘要里要交代跳过了谁，否则会被误读成"全都算过了"。"""
    p = _mk_project(tmp_path, "retired", regression=True)
    _disable(p)
    assert gn.disabled_projects(tmp_path) == ["retired"]
    txt = gn.render_text([], disabled=["retired"])
    assert "已停用" in txt and "retired" in txt


def test_disabled_projects_empty_when_dir_missing(tmp_path):
    assert gn.disabled_projects(tmp_path / "nope") == []


def test_summarize_reports_disabled_count(tmp_path):
    p = _mk_project(tmp_path, "active", regression=True)
    _write_artifact(p, "regression.json", all_pass=True)
    _write_artifact(p, "perf_security.json", all_pass=True)
    s = gn.summarize(gn.collect_gates(tmp_path), disabled=2)
    assert s["disabled"] == 2 and s["all_pass"] is True


def test_main_skips_disabled_by_default_and_can_include(tmp_path):
    p = _mk_project(tmp_path, "retired", regression=True)
    _write_artifact(p, "regression.json", all_pass=False)
    _disable(p)
    # 默认跳过 → 扫不到任何项目 → 未执行 → 门禁未达成
    assert gn.main(["--projects-dir", str(tmp_path), "--dry-run", "--fail-on-gate"]) == 1
    # 显式包含 → 看到它未通过
    assert gn.main(["--projects-dir", str(tmp_path), "--dry-run",
                    "--include-disabled", "--fail-on-gate"]) == 1


# --------------------------------------------------------------------------- #
# 停用项目（.disabled）—— 必须与看板/控制台口径一致，否则是"幽灵红"
# --------------------------------------------------------------------------- #
def _disable(pdir: Path) -> None:
    (pdir / ".disabled").write_text("", encoding="utf-8")


def test_disabled_project_excluded_like_the_dashboard(tmp_path):
    """停用项目带着历史失败产物时，不能继续把门禁拖红 —— 而项目页又看不到它。"""
    keep = _mk_project(tmp_path, "active", regression=True)
    _write_artifact(keep, "regression.json", all_pass=True)
    _write_artifact(keep, "perf_security.json", all_pass=True)

    retired = _mk_project(tmp_path, "retired", regression=True)
    _write_artifact(retired, "regression.json", all_pass=False, summary="早就没维护了")
    _disable(retired)

    rows = gn.collect_gates(tmp_path)
    assert [r["pid"] for r in rows] == ["active"]
    assert gn.summarize(rows, disabled=1)["all_pass"] is True
    # 不加过滤时停用项目会把它拖红 —— 这正是修之前的行为
    assert gn.summarize(gn.collect_gates(tmp_path, include_disabled=True))["all_pass"] is False


def test_disabled_projects_are_listed_not_silently_hidden(tmp_path):
    """跳过可以，静默不行 —— 摘要里要交代跳过了谁，否则会被误读成"全都算过了"。"""
    p = _mk_project(tmp_path, "retired", regression=True)
    _disable(p)
    assert gn.disabled_projects(tmp_path) == ["retired"]
    txt = gn.render_text([], disabled=["retired"])
    assert "已停用" in txt and "retired" in txt


def test_disabled_projects_empty_when_dir_missing(tmp_path):
    assert gn.disabled_projects(tmp_path / "nope") == []


def test_summarize_reports_disabled_count(tmp_path):
    p = _mk_project(tmp_path, "active", regression=True)
    _write_artifact(p, "regression.json", all_pass=True)
    _write_artifact(p, "perf_security.json", all_pass=True)
    s = gn.summarize(gn.collect_gates(tmp_path), disabled=2)
    assert s["disabled"] == 2 and s["all_pass"] is True


def test_main_skips_disabled_by_default_and_can_include(tmp_path):
    p = _mk_project(tmp_path, "retired", regression=True)
    _write_artifact(p, "regression.json", all_pass=False)
    _disable(p)
    # 默认跳过 → 扫不到任何项目 → 未执行 → 门禁未达成
    assert gn.main(["--projects-dir", str(tmp_path), "--dry-run", "--fail-on-gate"]) == 1
    # 显式包含 → 看到它未通过
    assert gn.main(["--projects-dir", str(tmp_path), "--dry-run",
                    "--include-disabled", "--fail-on-gate"]) == 1
