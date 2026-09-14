"""情景记忆回灌闭环：`extensions/memory/focus.py` 的测试。

这里最容易被破坏、也最值得守的是三件事：
1. **native 与 backfilled 必须分开** —— 合并成一个"覆盖率"，补齐就冒充成模型学会了。
2. **算不出就不猜** —— 清单为空时 rate 是 None，不能填 0 也不能填 100。
3. **匹配要保守** —— 沾到一个无关共同词不算覆盖；宁可漏报未覆盖，不能谎报已覆盖。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "extensions" / "memory"))

import knowledge as kn  # noqa: E402
import focus as fc  # noqa: E402

_CASES_MD = """# 测试用例（由需求生成）
- 来源：requirements.md
- 用例数：2

| id | 标题 | 模块 | 类型 | 优先级 | 前置 | 步骤 | 预期 | 可自动化 |
|---|---|---|---|---|---|---|---|---|
| REQ-001-F | 管理员登录（功能） | 认证 | 功能 | P1 | 已部署 | 1. 输入 admin 与密码 2. 点击登录 | 返回 200，data.token 非空 | 是 |
| REQ-001-B | 密码长度边界（边界） | 认证 | 边界 | P2 | 无 | 1. 输入长度为 6 的密码 | 提示「密码至少 8 位」 | 是 |
"""


def _rows():
    return fc._parse_rows(_CASES_MD)


# --------------------------------------------------------------------------- #
# 覆盖判定
# --------------------------------------------------------------------------- #
def test_covered_when_case_title_matches():
    res = fc.check(_rows(), [{"name": "管理员登录", "count": 3, "detail": "token 为空"}])
    assert res["total"] == 1
    assert [c["name"] for c in res["covered"]] == ["管理员登录"]
    assert res["missing"] == []


def test_missing_when_unrelated():
    res = fc.check(_rows(), [{"name": "导出报表", "count": 2, "detail": ""}])
    assert res["covered"] == []
    assert [m["name"] for m in res["missing"]] == ["导出报表"]
    assert res["rate"] == 0.0


def test_partial_overlap_is_not_counted_as_covered():
    """只沾到一个共同词不算覆盖 —— 否则"用户"这种词会让什么都算覆盖。"""
    # "用户列表分页" 与 "管理员登录" 只有极少量重合（"理" 类 2-gram）
    res = fc.check(_rows(), [{"name": "用户列表分页", "count": 5, "detail": ""}])
    assert res["covered"] == []


def test_empty_items_returns_none_rate_not_zero():
    """清单为空 = 无法判定，rate 必须是 None（填 0 会显示成"一个都没覆盖"）。"""
    res = fc.check(_rows(), [])
    assert res["total"] == 0 and res["rate"] is None


def test_similar_items_can_share_one_covering_case_documents_limitation():
    """已知偏乐观：高度相似的清单项会被同一条用例同时判为已覆盖。

    这条不是"期望行为正确"的断言，而是把**已知边界固化下来**：
    2-gram 分不开 `错误密码A` / `错误密码B`，覆盖率因此只能当粗粒度信号用。
    哪天换成正经语义匹配、这两项能被区分了，这条测试会变红提醒更新文档。
    """
    rows = [{"标题": "登录失败提示", "模块": "认证", "步骤": "1. 输入错误密码",
             "预期": "提示用户名或密码错误"}]
    res = fc.check(rows, [{"name": "错误密码A", "count": 4, "detail": ""},
                          {"name": "错误密码B", "count": 4, "detail": ""}])
    # 只有一条用例、且没有专门针对 A / B 的场景，但两项都被判为已覆盖 —— 这就是偏乐观的地方
    assert [c["name"] for c in res["covered"]] == ["错误密码A", "错误密码B"]


def test_untokenizable_item_treated_as_uncovered():
    """切不出词的项（纯标点）不该被判成已覆盖 —— 算不出就不猜。"""
    assert fc.is_covered("！！！", _rows()) is False


def test_reuses_knowledge_tokenizer():
    """与 knowledge 共用一套切词口径，避免出现第二套 matcher。"""
    assert fc._terms is kn.terms


# --------------------------------------------------------------------------- #
# 补齐与缺口显形
# --------------------------------------------------------------------------- #
def test_apply_appends_supplement_and_gap_section():
    items = [{"name": "导出报表", "count": 4, "detail": "超时"}]
    res = fc.apply(_CASES_MD, items, rows=_rows())
    assert res["native"] == 0 and res["backfilled"] == 1
    assert "【回灌】" in res["md"]
    assert fc.GAP_HEADING in res["md"]
    # 补出来的行要能被解析出来，否则下一轮覆盖率还是 0
    assert len(fc._parse_rows(res["md"])) == len(_rows()) + 1


def test_apply_no_missing_leaves_md_untouched():
    items = [{"name": "管理员登录", "count": 3, "detail": ""}]
    res = fc.apply(_CASES_MD, items, rows=_rows())
    assert res["md"] == _CASES_MD and res["backfilled"] == 0


def test_supplement_does_not_invent_business_detail():
    """补齐的是骨架，不是编造的业务断言 —— 步骤/预期必须写明需人工细化。"""
    rows = fc.supplement_rows([{"name": "导出报表", "count": 2}])
    assert "需人工细化" in rows[0]
    assert "FOCUS-001" in rows[0]


def test_gap_section_survives_backfill():
    """补齐之后缺口章节依然在：它记录的是"生成时没覆盖到"，不能因为补了就抹掉。"""
    res = fc.apply(_CASES_MD, [{"name": "导出报表", "count": 2, "detail": "内存溢出"}],
                   rows=_rows())
    assert "本轮生成**未自发覆盖**" in res["md"] or "未自发覆盖" in res["md"]
    assert "内存溢出" in res["md"]


def test_native_and_backfilled_are_reported_separately():
    items = [{"name": "管理员登录", "count": 3, "detail": ""},
             {"name": "导出报表", "count": 2, "detail": ""}]
    res = fc.apply(_CASES_MD, items, rows=_rows())
    summary = fc.to_summary(res)
    assert "1/2 为生成即覆盖" in summary and "1 条本轮新增补齐" in summary


def test_summary_when_no_items():
    assert "未计分" in fc.to_summary({"total": 0, "covered": [], "missing": []})


# --------------------------------------------------------------------------- #
# 趋势
# --------------------------------------------------------------------------- #
def test_record_and_read_history(tmp_path):
    fc.record_focus(tmp_path, {"total": 2, "native": 1, "backfilled": 1}, mode="规则版")
    fc.record_focus(tmp_path, {"total": 2, "native": 2, "backfilled": 0}, mode="规则版")
    h = fc.read_history(tmp_path)
    assert len(h) == 2
    assert h[0]["native_rate"] == 50.0 and h[1]["native_rate"] == 100.0


def test_delta_distinguishes_first_run_from_flat(tmp_path):
    """首次（None）与持平（0.0）必须可区分 —— 用 `if not d` 会把持平显示成"没跑过"。"""
    fc.record_focus(tmp_path, {"total": 2, "native": 1, "backfilled": 1})
    assert fc.delta(fc.read_history(tmp_path)) is None
    fc.record_focus(tmp_path, {"total": 2, "native": 1, "backfilled": 1})
    assert fc.delta(fc.read_history(tmp_path)) == 0.0


def test_step_focus_writes_cases_and_history(tmp_path):
    """流水线里的 `_step_focus`：补齐要真的落盘，历史要真的记一笔。"""
    import project_manager as pm  # noqa: E402

    pdir = tmp_path / "p"
    (pdir / "artifacts").mkdir(parents=True)
    cf = pdir / "artifacts" / "cases.md"
    cf.write_text(_CASES_MD, encoding="utf-8")
    res = pm._step_focus(pdir, cf, [{"name": "导出报表", "count": 3, "detail": "超时"}])
    assert res["backfilled"] == 1 and res["native"] == 0
    assert "【回灌】" in cf.read_text(encoding="utf-8")
    assert len(fc.read_history(pdir)) == 1


def test_supplement_persists_and_is_reused_next_round(tmp_path):
    """核心：补齐行要跨轮累积 —— cases.md 每轮重新生成，不存就永远原地打转。"""
    items = [{"name": "导出报表", "count": 3, "detail": "超时"}]
    r1 = fc.apply_persistent(tmp_path, _CASES_MD, items)
    assert r1["native"] == 0 and r1["backfilled"] == 1
    assert "【回灌】" in r1["md"]

    # 第二轮：cases.md 被重新生成（回到原始内容），但补齐行应当被并回来
    r2 = fc.apply_persistent(tmp_path, _CASES_MD, items)
    assert r2["persisted"] == 1 and r2["backfilled"] == 0
    assert "【回灌】" in r2["md"]
    assert len(fc._parse_rows(r2["md"])) == len(_rows()) + 1
    # 摘要里要能看出这轮是"沿用上轮"而不是"新补的"
    assert "沿用上轮" in fc.to_summary(r2)


def test_native_never_credits_persisted_rows(tmp_path):
    """生成即覆盖必须只算本轮生成的 —— 把上轮补的算成"模型学会了"就是谎报。"""
    items = [{"name": "导出报表", "count": 3, "detail": "超时"}]
    fc.apply_persistent(tmp_path, _CASES_MD, items)
    r2 = fc.apply_persistent(tmp_path, _CASES_MD, items)
    assert r2["native"] == 0 and r2["persisted"] == 1


def test_stale_supplement_dropped_when_item_leaves_lessons(tmp_path):
    """易错点不再出现（连续成功）后，补齐行要退场，不能一直挂着。"""
    fc.apply_persistent(tmp_path, _CASES_MD,
                        [{"name": "导出报表", "count": 3, "detail": ""}])
    assert fc.read_supplement(tmp_path)
    r = fc.apply_persistent(tmp_path, _CASES_MD,
                            [{"name": "管理员登录", "count": 1, "detail": ""}])
    assert r["persisted"] == 0
    assert fc.read_supplement(tmp_path) == []     # 文件也被删掉，不留空壳


def test_history_skips_corrupt_lines(tmp_path):
    fp = tmp_path / "artifacts" / fc.FOCUS_FILE
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text('{"total":2,"native":1,"backfilled":1,"native_rate":50.0}\n不是 JSON\n',
                  encoding="utf-8")
    assert len(fc.read_history(tmp_path)) == 1
