"""失败项 → 缺陷草稿（extensions/reporting/defects.py）单测。

重点验证的是**它克制的地方**：
- 环境问题（SKIP / 基线失败）不进缺陷清单；
- 配置问题不进缺陷清单；
- 严重程度只是建议（不假装能自动定级）；
- 不自动提单（只产出草稿）。
"""
import defects as df


def _reg(results):
    return {"results": results, "total": len(results),
            "passed": sum(1 for r in results if r.get("result") == "PASS"),
            "failed": sum(1 for r in results if r.get("result") == "FAIL"),
            "skipped": sum(1 for r in results if r.get("result") == "SKIP"),
            "all_pass": all(r.get("result") != "FAIL" for r in results)}


# ---------------------------------------------------------------- 分类

def test_regression_fail_becomes_defect():
    reg = _reg([{"name": "登录", "method": "POST", "url": "/admin/login",
                 "status_code": 200, "expect": "200 + code=200",
                 "result": "FAIL", "detail": "code=404（期望 200）"}])
    p = df.build_defects(reg=reg, base_url="http://x")
    assert p["counts"]["total"] == 1
    d = p["items"][0]
    assert d["severity"] == "S2" and d["source"] == "核心回归"
    assert "/admin/login" in " ".join(d["steps"])
    assert d["actual"] == "HTTP 200"


def test_regression_skip_is_env_issue_not_defect():
    """环境问题不是缺陷 —— 把"服务没起"报成缺陷最伤信任。"""
    reg = _reg([{"name": "登录", "result": "SKIP", "detail": "ConnectionError"}])
    p = df.build_defects(reg=reg)
    assert p["counts"]["total"] == 0
    assert p["counts"]["env"] == 1
    assert "环境问题" not in " ".join(p["items"]) if not p["items"] else True


def test_security_fail_is_s1():
    ps = {"security": {"checks": [{"name": "未授权访问", "category": "auth",
                                   "status": "FAIL", "detail": "可以匿名访问"}]}}
    p = df.build_defects(ps=ps)
    assert p["counts"]["total"] == 1
    assert p["items"][0]["severity"] == "S1"
    assert "安全" in p["items"][0]["title"]


def test_security_warn_is_not_defect():
    """WARN 只是提示，不进缺陷清单（否则满屏都是待确认缺陷，等于没有）。"""
    ps = {"security": {"checks": [{"name": "响应头", "status": "WARN", "detail": "缺 CSP"}]}}
    assert df.build_defects(ps=ps)["counts"]["total"] == 0


def test_perf_threshold_fail_is_s3():
    ps = {"perf": {"targets": [{"name": "列表", "method": "GET", "path": "/admin/list",
                                "threshold_fails": ["P95 > 500ms"], "p95_ms": 900,
                                "p99_ms": 1200, "error_rate": 0.0, "rps": 10,
                                "concurrency": 8, "requests": 40}]}}
    p = df.build_defects(ps=ps)
    assert p["counts"]["total"] == 1 and p["items"][0]["severity"] == "S3"


def test_web_fail_is_s2_and_flaky_is_s3():
    web = {"scenarios": [
        {"name": "结账", "result": "FAIL", "reason": "按钮不可见",
         "failed_step": {"index": 2, "action": "click", "target": "#pay"}},
        {"name": "搜索", "result": "FAIL", "flaky": True, "reason": "偶发超时"},
        {"name": "登录", "result": "PASS"},
    ]}
    p = df.build_defects(web=web)
    sev = {d["title"]: d["severity"] for d in p["items"]}
    assert sev["[Web] 结账"] == "S2"
    assert sev["[Web] 搜索"] == "S3"          # 抖动降级
    assert p["counts"]["total"] == 2


def test_web_config_issues_are_not_defects():
    web = {"scenarios": [], "config_issues": ["场景 x 用了 XPath 定位器"]}
    p = df.build_defects(web=web)
    assert p["counts"]["total"] == 0
    assert p["config_issues"] == ["场景 x 用了 XPath 定位器"]


def test_baseline_failure_is_reported_as_env_issue():
    """基线不成立时安全结论不可信 → 归环境问题，不能当成"发现漏洞"。"""
    ps = {"baseline": {"ok": False, "reason": "登录失败"}}
    p = df.build_defects(ps=ps)
    assert p["counts"]["total"] == 0
    assert any("基线未通过" in x for x in p["env_issues"])


def test_defects_sorted_by_severity_and_numbered():
    ps = {"security": {"checks": [{"name": "注入", "status": "FAIL"}]},
          "perf": {"targets": [{"name": "列表", "threshold_fails": ["P95 超"]}]}}
    reg = _reg([{"name": "登录", "result": "FAIL"}])
    p = df.build_defects(reg=reg, ps=ps)
    assert [d["severity"] for d in p["items"]] == ["S1", "S2", "S3"]
    assert [d["id"] for d in p["items"]] == ["DEF-001", "DEF-002", "DEF-003"]


def test_no_failures_yields_empty_list():
    p = df.build_defects(reg=_reg([{"name": "登录", "result": "PASS"}]))
    assert p["items"] == [] and p["counts"]["total"] == 0
    assert "没有发现需要提交的产品缺陷" in df.render_markdown(p)


# ---------------------------------------------------------------- 渲染与落盘

def test_markdown_escapes_table_cells():
    """标题里带竖线/换行不能把 Markdown 表格撑坏。"""
    import re

    reg = _reg([{"name": "登录|超时", "result": "FAIL", "detail": "第一行\n第二行"}])
    md = df.render_markdown(df.build_defects(reg=reg))
    row = [ln for ln in md.splitlines() if ln.startswith("| DEF-001")][0]
    # 按**未转义**的竖线切分，应正好得到 7 个单元格
    # （编号 / 建议级别 / 新旧 / 来源 / 标题 / 实际 / 期望）
    cells = [c for c in re.split(r"(?<!\\)\|", row) if c.strip()]
    assert len(cells) == 7
    assert "\\|" in row        # 原竖线已被转义
    assert "\n" not in row     # 换行已被压平


def test_markdown_mentions_no_auto_filing():
    reg = _reg([{"name": "登录", "result": "FAIL"}])
    md = df.render_markdown(df.build_defects(reg=reg))
    assert "不会自动提单" in md and "建议值" in md


def test_write_and_read_defects(tmp_path):
    reg = _reg([{"name": "登录", "result": "FAIL"}])
    payload = df.build_defects(reg=reg, pid="demo", base_url="http://x")
    f = df.write_defects(tmp_path, payload)
    assert f.is_file() and (tmp_path / "artifacts" / "defects.json").is_file()
    got = df.read_defects(tmp_path)
    assert got["counts"]["total"] == 1


def test_read_defects_missing(tmp_path):
    assert df.read_defects(tmp_path) is None
