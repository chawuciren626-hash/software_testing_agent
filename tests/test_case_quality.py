"""用例结构质量分（extensions/requirements_to_cases/case_quality.py）单测。

重点验证的是**它诚实的地方**：
- 算不出的维度记 None + 说明，而不是猜一个数把总分凑满；
- 步骤编号里的数字不算"具体数据"（否则规则版模板直接满分，趋势失去意义）；
- 重复用例、缺步骤/预期会被如实扣分；
- 环比区分"首次/持平/涨/跌"，不用 `if not delta` 把持平显示成"—"。
"""
import json

import case_quality as cq
import pytest


def _row(rid="REQ-001-F", title="登录（功能）", ctype="功能", steps="1. 输入 admin / macro123 2. 点击登录",
         expected="返回 200 且 data.token 非空"):
    return {"id": rid, "标题": title, "模块": "认证", "类型": ctype, "优先级": "P1",
            "前置": "已部署", "步骤": steps, "预期": expected, "可自动化": "是"}


def _good_cases(n_req=1):
    """每个需求三类齐备、写了具体数据的用例集。"""
    rows = []
    for i in range(1, n_req + 1):
        rid = f"REQ-{i:03d}"
        rows += [
            _row(f"{rid}-F", f"需求{i}（功能）", "功能",
                 "1. 输入账号 admin 与密码 macro123 2. 点击登录", "返回 200，data.token 非空"),
            _row(f"{rid}-B", f"需求{i}（边界）", "边界",
                 "1. 输入长度为 6 个字符的密码 2. 提交", "提示「密码长度至少 8 位」"),
            _row(f"{rid}-N", f"需求{i}（异常）", "异常",
                 "1. 输入错误密码 2. 点击登录", "返回 500 且提示「用户名或密码错误」"),
        ]
    return rows


# ---------------------------------------------------------------- 打分逻辑

def test_perfect_cases_score_high():
    r = cq.score_rows(_good_cases(2), requirement_count=2)
    assert r["total"] == 100
    assert r["dims"]["coverage"] == 100 and r["dims"]["types"] == 100
    assert r["counts"]["cases"] == 6 and r["counts"]["covered"] == 2


def test_empty_cases_not_scored():
    """没有用例 → 明确说算不了，而不是给 0 分（0 分会被当成"质量极差"）。"""
    r = cq.score_rows([], requirement_count=3)
    assert r["total"] is None
    assert any("没有解析到任何用例" in n for n in r["notes"])


def test_missing_requirement_count_marks_coverage_unscored():
    """需求条数未知时覆盖率算不出来：记 None + 说明，总分按剩余维度归一化。"""
    r = cq.score_rows(_good_cases(1), requirement_count=None)
    assert r["dims"]["coverage"] is None
    assert any("未提供需求条数" in n for n in r["notes"])
    # 其余维度仍是满分 → 归一化后总分仍应是 100
    assert r["total"] == 100
    assert any("未计分维度" in n for n in r["notes"])


def test_unparsable_ids_disable_coverage_and_types():
    """id 不符合 REQ-NNN-X → 无法定位所属需求，两个维度都不猜。"""
    rows = [_row(rid="TC-01", ctype="功能"), _row(rid="TC-02", ctype="边界")]
    r = cq.score_rows(rows, requirement_count=1)
    assert r["dims"]["coverage"] is None and r["dims"]["types"] is None
    assert any("REQ-NNN-X" in n for n in r["notes"])
    # 剩下的维度仍参与计分
    assert r["total"] is not None


def test_partial_coverage_lowers_score():
    rows = _good_cases(1)                    # 只覆盖 REQ-001，但共有 3 条需求
    r = cq.score_rows(rows, requirement_count=3)
    assert r["dims"]["coverage"] == 33
    assert r["dims"]["types"] == 100         # 已覆盖的那条需求三类齐全
    assert r["total"] < 100


def test_missing_types_lowers_types_score():
    rows = [r for r in _good_cases(2) if r["类型"] != "异常"]
    r = cq.score_rows(rows, requirement_count=2)
    assert r["dims"]["types"] == 67          # 每个需求只剩 2/3 类


def test_duplicate_cases_lower_dedup_and_note_it():
    # 必须复制得**完全一致**：去重键是「标题+步骤」，只改标题不算重复
    rows = _good_cases(1) + [_good_cases(1)[0]]
    r = cq.score_rows(rows, requirement_count=1)
    assert r["counts"]["dup"] == 1
    assert r["dims"]["dedup"] < 100
    assert any("重复用例" in n for n in r["notes"])


def test_executable_requires_steps_and_expected():
    """缺步骤或预期 → 这条不算可执行（"有动作但没预期"无法判定成败）。"""
    good = _row()
    no_steps = _row(steps="")
    no_expected = _row(expected="")
    r = cq.score_rows([good, no_steps, no_expected], requirement_count=1)
    assert r["dims"]["executable"] == 33


def test_step_number_is_not_concrete_data():
    """回归坑：步骤编号（"1. 打开页面"）本身带数字，不能当成具体数据，
    否则规则版模板会满分 → 分数饱和、趋势失去意义。"""
    vague = _row(steps="1. 按需求执行：用户登录", expected="功能按预期正常完成，无报错")
    assert cq.has_specific(vague) is False
    concrete = _row(steps="1. 输入长度为 6 个字符的密码", expected="提示「密码过短」")
    assert cq.has_specific(concrete) is True


def test_saturation_is_disclosed():
    """满分必须显式说明"只代表形式完整"，否则会被误读成质量结论。"""
    r = cq.score_rows(_good_cases(1), requirement_count=1)
    assert any("饱和" in n for n in r["notes"])


def test_weights_renormalized_when_dim_missing():
    """少一个维度时，剩下的权重按比例放大，不是简单除以维度数。"""
    rows = _good_cases(1)
    full = cq.score_rows(rows, requirement_count=1)
    partial = cq.score_rows(rows, requirement_count=None)
    # 两处都是满分 → 归一化与否都应得 100（防止"缺维度就掉分"的假信号）
    assert full["total"] == partial["total"] == 100


# ---------------------------------------------------------------- 解析

def test_parse_rows_reads_markdown_table():
    md = ("| id | 标题 | 类型 | 步骤 | 预期 |\n|---|---|---|---|---|\n"
          "| REQ-001-F | 登录 | 功能 | 1. 登录 | 成功 |\n")
    rows = cq.parse_rows(md)
    assert len(rows) == 1 and rows[0]["id"] == "REQ-001-F"


def test_parse_rows_empty_on_no_table():
    assert cq.parse_rows("# 没有表格\n随便一段文字\n") == []


def test_score_cases_md_entry():
    md = ("| id | 标题 | 模块 | 类型 | 优先级 | 前置 | 步骤 | 预期 | 可自动化 |\n"
          "|---|---|---|---|---|---|---|---|---|\n"
          "| REQ-001-F | 登录（功能） | 认证 | 功能 | P1 | 无 | 1. 输入 8 位密码 | 返回 200 | 是 |\n")
    r = cq.score_cases_md(md, requirement_count=1)
    assert r["counts"]["cases"] == 1 and r["total"] is not None


# ---------------------------------------------------------------- 展示

def test_render_text_contains_total_and_dims():
    r = cq.score_rows(_good_cases(1), requirement_count=1)
    txt = cq.render_text(r)
    assert "总分" in txt and "需求覆盖" in txt and "三类齐备" in txt


def test_sparkline_needs_two_points():
    """只有一个点画不出趋势 → 返回空串，不画一条自欺欺人的平线。"""
    assert cq.sparkline([90]) == ""
    assert cq.sparkline([]) == ""
    s = cq.sparkline([80, 90])
    assert "polyline" in s


def test_sparkline_colors_by_direction():
    assert "#34d399" in cq.sparkline([70, 90])    # 涨 → 绿
    assert "#f87171" in cq.sparkline([90, 70])    # 跌 → 红
    assert "#94a3b8" in cq.sparkline([80, 80])    # 平 → 灰


# ---------------------------------------------------------------- 历史与环比

def test_history_roundtrip_and_delta(tmp_path):
    pdir = tmp_path / "p"
    pdir.mkdir()
    assert cq.read_history(pdir) == []

    cq.append_history(pdir, {"total": 90, "dims": {}, "counts": {"cases": 9},
                            "scored_at": "2026-01-01 10:00"}, mode="规则版")
    assert cq.delta(cq.read_history(pdir)) is None          # 只有一个点 → 首次

    cq.append_history(pdir, {"total": 80, "dims": {}, "counts": {"cases": 8},
                            "scored_at": "2026-01-02 10:00"}, mode="LLM 增强")
    hist = cq.read_history(pdir)
    assert len(hist) == 2 and cq.delta(hist) == -10
    assert hist[-1]["mode"] == "LLM 增强"
    assert hist[-1]["cases"] == 8


def test_delta_zero_is_not_treated_as_first():
    """`delta == 0`（持平）与 `delta is None`（首次）必须区分开。"""
    hist = [{"total": 90}, {"total": 90}]
    assert cq.delta(hist) == 0


def test_history_skips_corrupt_lines(tmp_path):
    """一行坏数据不该让整个趋势挂掉。"""
    pdir = tmp_path / "p"
    (pdir := pdir).mkdir()
    hp = cq.history_path(pdir)
    hp.parent.mkdir(parents=True, exist_ok=True)
    hp.write_text('{"total": 90}\n这不是 JSON\n{"total": 70}\n', encoding="utf-8")
    hist = cq.read_history(pdir)
    assert len(hist) == 2 and cq.delta(hist) == -20


def test_history_truncated_to_keep(tmp_path):
    pdir = tmp_path / "p"
    pdir.mkdir()
    for i in range(10):
        cq.append_history(pdir, {"total": i, "dims": {}, "counts": {"cases": 1},
                                 "scored_at": f"t{i}"}, keep=5)
    assert len(cq.read_history(pdir)) == 5


def test_record_quality_appends_and_computes_delta(tmp_path):
    """推荐入口 record_quality：一次完成「追加历史 + 写文件 + 算环比」。"""
    pdir = tmp_path / "p"
    pdir.mkdir()
    cq.append_history(pdir, {"total": 90, "dims": {}, "counts": {"cases": 9}, "scored_at": "t1"})
    res = cq.score_rows(_good_cases(1), requirement_count=1)
    cq.record_quality(pdir, res, mode="规则版")
    got = cq.read_quality(pdir)
    assert got["total"] == res["total"]
    assert got["delta"] == res["total"] - 90
    assert len(got["history"]) == 2 and got["history"][-1]["mode"] == "规则版"


def test_write_quality_alone_does_not_invent_delta(tmp_path):
    """历史不足两个点时，write_quality 必须给出 delta=None（首次），不编造「持平」。"""
    pdir = tmp_path / "p"
    pdir.mkdir()
    res = cq.score_rows(_good_cases(1), requirement_count=1)
    cq.write_quality(pdir, res, history=[])
    assert cq.read_quality(pdir)["delta"] is None


def test_read_quality_missing_returns_none(tmp_path):
    assert cq.read_quality(tmp_path / "nope") is None
