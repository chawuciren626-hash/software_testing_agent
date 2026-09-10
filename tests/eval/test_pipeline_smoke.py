"""L2 评测：规则版流水线端到端可跑通（解析→生成→渲染，无异常、无网络）。"""
import generate_cases as gc
import project_manager as pm


def test_rule_pipeline_end_to_end():
    req = "1. 用户登录\n2. 查看列表\n"
    items = gc.parse_requirements(req)
    cases = gc.gen_cases(items)
    md = gc.to_markdown(cases, "inline")
    assert len(cases) == 6
    html, n = pm._cases_html(md)
    assert n == 6
    assert "req-card" in html
