"""L1 评测：规则版用例生成质量断言。"""
import generate_cases as gc
import project_manager as pm

SAMPLE = """1. 用户可以使用账号密码登录系统
2. 管理员能够查看用户列表
3. 普通用户不能访问管理后台
"""


def test_parse_requirements():
    items = gc.parse_requirements(SAMPLE)
    assert len(items) == 3


def test_gen_cases_three_types():
    items = gc.parse_requirements(SAMPLE)
    cases = gc.gen_cases(items)
    assert len(cases) == 9  # 3 需求 * 3 类
    types = {c["type"] for c in cases}
    assert types == {"功能", "边界", "异常"}


def test_roundtrip_markdown_parse():
    items = gc.parse_requirements(SAMPLE)
    md = gc.to_markdown(gc.gen_cases(items), "sample")
    meta, rows = pm._parse_cases(md)
    assert meta["count"] == 9
    assert len(rows) == 9
    assert all(r.get("可自动化") for r in rows)
