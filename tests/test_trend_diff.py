"""失败项新旧对比（extensions/reporting/trend_diff.py）的测试。

两条被真实踩出来的坑，由这里的用例钉死：
1. `flaky` 分支曾是死代码（上一层 `or "PASS" in window_results` 抢先把抖动判成回归）。
2. `_step_diff` 必须在写本次快照**之前**调用，否则自己跟自己比。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "extensions" / "reporting"))

import trend_diff as td  # noqa: E402
import defects as df  # noqa: E402
import project_manager as pm  # noqa: E402


# --------------------------------------------------------------------------- #
# 构造工具
# --------------------------------------------------------------------------- #
def _R(*pairs):
    """构造 regression results：`_R(("场景", "PASS"), ...)`。"""
    return [{"name": n, "result": r, "type": "api_smoke", "method": "GET",
             "url": "http://x/", "expect": "200", "status_code": 500,
             "detail": f"{n} 断言失败"}
            for n, r in pairs]


def _snap(results, ts, passed=None, failed=None):
    return {
        "ts": ts,
        "results": results,
        "passed": passed if passed is not None
        else sum(1 for r in results if r["result"] == "PASS"),
        "failed": failed if failed is not None
        else sum(1 for r in results if r["result"] == "FAIL"),
    }


_NOW = int(time.time())


def _history(*rounds):
    """rounds: 由早到晚的 results 列表。"""
    out = []
    for i, res in enumerate(rounds):
        out.append(_snap(res, _NOW - (len(rounds) - i) * 100))
    return out


# --------------------------------------------------------------------------- #
# 分类
# --------------------------------------------------------------------------- #
def test_prev_pass_now_fail_is_regressed():
    hist = _history(_R(("登录", "PASS")))
    prev = hist[-1]
    p = td.compare(_R(("登录", "FAIL")), prev=prev, history=hist)
    assert p["items"][0]["status"] == "regressed"


def test_scene_that_never_passed_is_new():
    p = td.compare(_R(("新场景", "FAIL")), prev=_snap(_R(("无关", "PASS")), _NOW),
                   history=[])
    assert p["items"][0]["status"] == "new"


def test_fail_since_forever_is_persistent():
    hist = _history(_R(("老毛病", "FAIL")), _R(("老毛病", "FAIL")))
    p = td.compare(_R(("老毛病", "FAIL")), prev=hist[-1], history=hist)
    it = p["items"][0]
    assert it["status"] == "persistent"
    assert it["streak"] == 3            # 历史 2 次 + 本次
    assert it["last_pass_text"] == "—"


def test_flaky_not_misreported_as_regressed():
    """回归：上一次 FAIL、但窗口里红绿交替 → 必须判 flaky。

    曾经因为判定分支顺序写反（上层 `or PASS in window` 抢先），
    把抖动误报成"刚引入的回归"——这是最伤信任的一类误判。
    """
    hist = _history(_R(("抖动", "PASS")), _R(("抖动", "FAIL")), _R(("抖动", "PASS")),
                    _R(("抖动", "FAIL")))
    p = td.compare(_R(("抖动", "FAIL")), prev=hist[-1], history=hist)
    assert p["items"][0]["status"] == "flaky", "抖动被误判成了回归"
    # 且不能被算进"本次新增失败"
    assert p["focus_new"] == 0


def test_fail_to_pass_is_recovered():
    hist = _history(_R(("修好了", "FAIL")))
    p = td.compare(_R(("修好了", "PASS")), prev=hist[-1], history=hist)
    assert p["items"][0]["status"] == "recovered"


def test_pass_all_along_is_not_listed():
    hist = _history(_R(("一直好", "PASS")))
    p = td.compare(_R(("一直好", "PASS")), prev=hist[-1], history=hist)
    assert p["items"] == []


def test_skipped_does_not_participate():
    """SKIP 是环境问题的产物，算进胜负序列会污染 streak。"""
    hist = _history(_R(("A", "SKIP")))
    p = td.compare(_R(("A", "SKIP")), prev=hist[-1], history=hist)
    assert p["items"] == []


def test_focus_new_counts_only_regressed_and_new():
    hist = _history(_R(("回归项", "PASS"), ("老毛病", "FAIL")))
    cur = _R(("回归项", "FAIL"), ("老毛病", "FAIL"), ("新场景", "FAIL"))
    p = td.compare(cur, prev=hist[-1], history=hist)
    assert p["focus_new"] == 2
    assert p["counts"]["regressed"] == 1 and p["counts"]["new"] == 1
    assert p["counts"]["persistent"] == 1


def test_items_sorted_regressed_first():
    hist = _history(_R(("新增项", "PASS"), ("长期项", "FAIL")))
    cur = _R(("长期项", "FAIL"), ("新增项", "FAIL"))
    p = td.compare(cur, prev=hist[-1], history=hist)
    assert p["items"][0]["status"] == "regressed"
    assert p["items"][-1]["status"] == "persistent"


# --------------------------------------------------------------------------- #
# 基线有效性
# --------------------------------------------------------------------------- #
def test_no_previous_run_is_unknown():
    p = td.compare(_R(("A", "FAIL")))
    assert p["baseline"]["available"] is False
    assert "第一次" in p["baseline"]["reason"]
    assert p["items"][0]["status"] == "unknown"
    assert p["focus_new"] == 0


def test_previous_all_skipped_is_not_a_baseline():
    """上次全 SKIP（环境不可达）却当成基线，会把全部失败都算成"新增"。

    这是与「环境不可达不判绿」同源的规矩：**没有有效基线就不做新旧判断**。
    """
    bad_prev = _snap(_R(("A", "SKIP")), _NOW, passed=0, failed=0)
    p = td.compare(_R(("A", "FAIL"), ("B", "FAIL")), prev=bad_prev, history=[])
    assert p["baseline"]["available"] is False
    assert every_status_is(p, "unknown")
    assert p["focus_new"] == 0, "没有基线的情况下不得报出新增失败"


def every_status_is(p, status):
    return all(i["status"] == status for i in p["items"]) and bool(p["items"])


# --------------------------------------------------------------------------- #
# 渲染与落盘
# --------------------------------------------------------------------------- #
def test_render_text_states_it_is_not_a_gate():
    hist = _history(_R(("A", "PASS")))
    p = td.compare(_R(("A", "FAIL")), prev=hist[-1], history=hist)
    txt = td.render_text(p, "标题")
    assert "回归" in txt and "A" in txt
    assert "对照基线" in txt


def test_write_and_read_roundtrip(tmp_path):
    pdir = tmp_path / "p"
    hist = _history(_R(("A", "PASS")))
    payload = td.compare(_R(("A", "FAIL")), prev=hist[-1], history=hist)
    f = td.write_diff(pdir, payload)
    assert f.is_file()
    assert td.read_diff(pdir)["headline"] == payload["headline"]


def test_read_diff_missing_or_corrupt(tmp_path):
    pdir = tmp_path / "p"
    assert td.read_diff(pdir) is None
    pdir.mkdir()
    (pdir / "artifacts").mkdir()
    (pdir / "artifacts" / "diff.json").write_text("{坏 JSON", encoding="utf-8")
    assert td.read_diff(pdir) is None


# --------------------------------------------------------------------------- #
# 与缺陷草稿的集成
# --------------------------------------------------------------------------- #
def test_defects_carry_change_labels_and_reorder():
    hist = _history(_R(("老毛病", "FAIL"), ("回归项", "PASS")))
    cur = _R(("老毛病", "FAIL"), ("回归项", "FAIL"), ("新场景", "FAIL"))
    diff = td.compare(cur, prev=hist[-1], history=hist)
    payload = df.build_defects({"results": cur}, pid="demo", diff=diff)

    by_scene = {d["scene"]: d for d in payload["items"]}
    assert by_scene["回归项"]["change"] == "regressed"
    assert by_scene["新场景"]["change"] == "new"
    assert by_scene["老毛病"]["change"] == "persistent"

    # 同为 S2 时，regressed > new > persistent
    order = [d["change"] for d in payload["items"]]
    assert order == ["regressed", "new", "persistent"]
    assert payload["counts"]["by_change"]["regressed"] == 1


def test_defects_without_diff_still_works():
    """不传 diff 时保持纯严重度排序 —— CLI 单独使用不该因此崩掉。"""
    payload = df.build_defects({"results": _R(("A", "FAIL"))}, pid="demo")
    assert payload["items"][0]["id"] == "DEF-001"
    assert payload["items"][0].get("change", None) is None


def test_defects_markdown_has_change_column():
    hist = _history(_R(("A", "PASS")))
    cur = _R(("A", "FAIL"))
    diff = td.compare(cur, prev=hist[-1], history=hist)
    md = df.render_markdown(df.build_defects({"results": cur}, pid="demo", diff=diff))
    assert "| 新旧 |" in md
    assert "回归" in md


def test_every_defect_item_has_scene_field():
    """scene 是与新旧对比对齐的锚点。用 title 反解会在前缀变化时静默失效。"""
    cur = _R(("A", "FAIL"))
    payload = df.build_defects({"results": cur}, pid="demo")
    assert all("scene" in d for d in payload["items"])


# --------------------------------------------------------------------------- #
# project_manager 集成
# --------------------------------------------------------------------------- #
class _FakeStore:
    """最小 run_store 替身：只提供 list_snapshots。"""

    def __init__(self, snaps):
        self._snaps = snaps
        self.calls = 0

    def list_snapshots(self, pid, limit=60, since=None):
        self.calls += 1
        return list(self._snaps)


def test_step_diff_writes_file_and_prints(tmp_path, capsys):
    pdir = tmp_path / "p"
    pdir.mkdir()
    hist = _history(_R(("A", "PASS")))
    pm._step_diff("demo", pdir, {"results": _R(("A", "FAIL"))},
                  run_store=_FakeStore(hist))
    assert (pdir / "artifacts" / "diff.json").is_file()
    assert "新旧对比" in capsys.readouterr().out


def test_step_diff_without_store_treats_as_first_run(tmp_path, capsys):
    """没有 DB（轻量入口）时按"无基线"处理，仍落盘一份写明原因的结论。"""
    pdir = tmp_path / "p"
    pdir.mkdir()
    p = pm._step_diff("demo", pdir, {"results": _R(("A", "FAIL"))}, run_store=None)
    assert p["baseline"]["available"] is False
    assert p == td.read_diff(pdir)


def test_step_diff_reads_history_before_snapshot_written(tmp_path):
    """守护调用顺序：history 必须来自现场读取，不能包含本次还没写入的结果。"""
    pdir = tmp_path / "p"
    pdir.mkdir()
    hist = _history(_R(("A", "PASS")))
    store = _FakeStore(hist)
    payload = pm._step_diff("demo", pdir, {"results": _R(("A", "FAIL"))},
                            run_store=store)
    assert store.calls == 1
    # 历史里只有 1 轮，因此 A 的判定是"回归"而不是"持续失败"
    assert payload["items"][0]["status"] == "regressed"


def test_diff_against_self_would_hide_everything(tmp_path):
    """反例：**如果**调用方写反顺序（先 insert_snapshot 再对比），
    历史里最新一条就是本次自己 → 本次所有失败都变成 persistent / 无新增。

    这条把那条看不见的坑变成可执行的文档：结论没变化就说明顺序是对的。
    """
    cur = _R(("A", "FAIL"))
    own = _snap(cur, _NOW)                       # 模拟"本次已被写进快照"
    p = td.compare(cur, prev=own, history=[own])
    assert p["focus_new"] == 0
    assert p["items"][0]["status"] == "persistent"


def test_report_renders_diff_card(tmp_path):
    pdir = tmp_path / "p"
    pdir.mkdir()
    (pdir / "project.yaml").write_text("project_id: demo\n", encoding="utf-8")
    hist = _history(_R(("A", "PASS")))
    pm._step_diff("demo", pdir, {"results": _R(("A", "FAIL"))},
                  run_store=_FakeStore(hist))
    out = pm._step_report("demo", pdir, None,
                          {"results": _R(("A", "FAIL")), "total": 1,
                           "passed": 0, "failed": 1, "skipped": 0})
    html = out.read_text(encoding="utf-8")
    assert "失败项新旧对比" in html
    assert "不参与门禁判定" in html


def test_report_without_diff_shows_nothing(tmp_path):
    pdir = tmp_path / "p"
    pdir.mkdir()
    (pdir / "project.yaml").write_text("project_id: demo\n", encoding="utf-8")
    (pdir / "artifacts").mkdir()          # 报告往这写；正常流程由前面的步骤创建
    out = pm._step_report("demo", pdir, None,
                          {"results": [], "total": 0, "passed": 0,
                           "failed": 0, "skipped": 0})
    assert "失败项新旧对比" not in out.read_text(encoding="utf-8")


def test_step_defects_picks_up_diff_from_disk(tmp_path):
    """调用方不传 diff 时，从产物读 —— CLI `defects <id>` 也能带上新旧标签。"""
    pdir = tmp_path / "p"
    pdir.mkdir()
    hist = _history(_R(("回归项", "PASS")))
    pm._step_diff("demo", pdir, {"results": _R(("回归项", "FAIL"))},
                  run_store=_FakeStore(hist))
    payload = pm._step_defects("demo", pdir, {"results": _R(("回归项", "FAIL"))})
    assert payload["items"][0].get("change") == "regressed"
