"""控制台前端「真实浏览器」冒烟：逐个点开所有页面，抓运行期 JS 报错。

为什么必须有这一层：
- `tests/test_web_smoke.py` 用 Flask 测试客户端，**只发 HTTP 请求、不执行 JavaScript**。
  它验证不了「页面点开是白的」「按钮上是代码文本」这类问题。
- 本轮开发中就靠浏览器实测抓到过两个真前端 bug：
  1. 详情弹窗里写了 `${svg('shield')}` —— 那段是静态 HTML 不是 JS 模板字符串，插值不生效，
     按钮上原样显示成代码文本；Flask 测试客户端永远发现不了。
  2. Flask `debug=False` 时 Jinja 缓存模板，改了 `index.html` 刷新无变化（易误判成浏览器缓存）。
所以这里用真实 Chromium 跑一遍，把"页面能不能正常打开"变成可回归的断言。

没有可用的 Chromium 时整体 skip（不判失败）—— 浏览器是环境问题，不是产品缺陷。
"""
from __future__ import annotations

import threading
from typing import Iterator, List

import pytest
from werkzeug.serving import make_server

import app as web_app


def _browser_ready() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            b.close()
        return True
    except Exception:
        return False


needs_browser = pytest.mark.skipif(
    not _browser_ready(),
    reason="本机没有可用的 Playwright Chromium（playwright install chromium）")


# 页面 → 该页必须真实渲染出内容的锚点元素
PAGES = {
    "projects": "#projectGrid",
    "tasks": "#taskList",
    "reports": "#repList",
    "automation": "#autoList",
    "gates": "#gateList",
    "knowledge": "#kbList",
    "skills": "#skillList",
    "models": "#keyOpenai",
}


@pytest.fixture(scope="module")
def console_url() -> Iterator[str]:
    """在后台线程里起一个真实的 HTTP 服务（不是测试客户端）—— 浏览器需要一个真端口。"""
    server = make_server("127.0.0.1", 0, web_app.app, threaded=True)
    port = server.server_port
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        t.join(timeout=5)


@pytest.fixture(scope="module")
def browser_session():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        yield b
        b.close()


def _collect_problems(browser, url: str, pages: List[str]) -> List[str]:
    page = browser.new_page(viewport={"width": 1500, "height": 1000})
    errors: List[str] = []
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: errors.append(f"console.error: {m.text}")
            if m.type == "error" else None)
    page.on("requestfailed",
            lambda r: errors.append(f"requestfailed: {r.url} ({r.failure})"))

    problems: List[str] = []
    try:
        page.goto(url, wait_until="networkidle")
        for name in pages:
            errors.clear()
            nav = page.locator(f'.nav-item[data-page="{name}"]')
            if nav.count() == 0:
                problems.append(f"{name}: 侧边栏没有这个入口")
                continue
            nav.first.click()
            page.wait_for_timeout(1200)

            if page.locator(f"#page-{name}.active").count() == 0:
                problems.append(f"{name}: 点击后页面未激活")
            anchor = page.locator(PAGES[name])
            if anchor.count() == 0 or not anchor.inner_html().strip():
                problems.append(f"{name}: 锚点 {PAGES[name]} 没有渲染出内容（白屏）")
            if errors:
                problems.append(f"{name}: " + " | ".join(errors[:3]))
    finally:
        page.close()
    return problems


@needs_browser
def test_all_console_pages_render_without_js_errors(console_url, browser_session):
    """8 个页面都要能打开、有内容、且运行期没有 JS 报错。"""
    problems = _collect_problems(browser_session, console_url, list(PAGES))
    assert not problems, "控制台页面存在问题：\n" + "\n".join(f" - {p}" for p in problems)


@needs_browser
def test_nav_entries_match_registered_pages(console_url, browser_session):
    """侧边栏入口与 PAGE_META / 页面容器必须一一对应 —— 少一个就是点不开的死链。"""
    page = browser_session.new_page()
    try:
        page.goto(console_url, wait_until="networkidle")
        nav_pages = page.eval_on_selector_all(
            ".nav-item[data-page]", "els => els.map(e => e.dataset.page)")
        assert sorted(nav_pages) == sorted(PAGES), (
            f"侧边栏入口与预期页面不一致：{nav_pages}")
        for name in nav_pages:
            assert page.locator(f"#page-{name}").count() == 1, f"缺少页面容器 #page-{name}"
    finally:
        page.close()


@needs_browser
def test_quality_tab_renders_without_js_errors(console_url, browser_session):
    """详情弹窗的「用例质量」页必须真的渲染出内容，且没有 JS 报错。

    只断言"HTML 里有 renderQualityPanel 这个函数名"是不够的 ——
    函数体里一个 undefined 变量照样会让面板空白，而字符串检查照样通过。
    """
    page = browser_session.new_page(viewport={"width": 1500, "height": 1000})
    errors = []
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: errors.append(f"console.error: {m.text}")
            if m.type == "error" else None)
    try:
        page.goto(console_url, wait_until="networkidle")
        page.evaluate("switchTab('quality')")
        page.wait_for_timeout(300)
        # 有数据：分数、维度条、免责说明都要出现
        page.evaluate("""() => renderQualityPanel({
            has: true, total: 95, delta: -3,
            dims: {coverage: 100, types: 100, executable: 100, specificity: 47, dedup: 100},
            dims_order: ['coverage','types','executable','specificity','dedup'],
            labels: {coverage:'需求覆盖', types:'三类齐备', executable:'可执行性',
                     specificity:'具体性', dedup:'去重'},
            hints: {coverage:'被覆盖到的需求占比'},
            counts: {cases: 15, requirements: 5, covered: 5, dup: 0},
            history: [{total: 98}, {total: 95}], notes: ['提示一条'],
            scored_at: '2026-09-11 18:00', text: '总分：95'}, null)""")
        box = page.locator("#d_quality")
        html = box.inner_html()
        assert "95" in html, "总分没渲染出来"
        assert "q-bar-fill" in html, "维度条没渲染出来"
        assert "不代表用例质量好坏" in html, "缺少「结构分≠质量」的免责说明"
        assert "↓3" in html, "环比下跌没显示"
        # 无数据：要给引导文案，而不是空白
        page.evaluate("() => renderQualityPanel(null, null)")
        assert "尚未计算" in page.locator("#d_quality").inner_html()
        assert not errors, "质量页面有 JS 报错：" + " | ".join(errors[:3])
    finally:
        page.close()


@needs_browser
def test_diff_tab_renders_without_js_errors(console_url, browser_session):
    """详情弹窗的「新旧对比」页必须真的渲染出内容，且没有 JS 报错。

    只断言"HTML 里有 renderDiffPanel 这个字符串"是不够的 —— 函数体里一个
    undefined 变量照样会让面板空白，而字符串检查照样通过。
    """
    page = browser_session.new_page(viewport={"width": 1500, "height": 1000})
    errors = []
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: errors.append(f"console.error: {m.text}")
            if m.type == "error" else None)
    try:
        page.goto(console_url, wait_until="networkidle")
        page.evaluate("switchTab('diff')")
        page.wait_for_timeout(300)
        page.evaluate("""() => renderDiffPanel({
            has: true, focus_new: 1, headline: '本次新增失败 1 项',
            baseline: {available: true, ts_text: '09-11 22:00'},
            counts: {regressed: 1, new: 0, persistent: 1, flaky: 0, recovered: 0},
            labels: {regressed: '回归（此前通过，这次失败）',
                     persistent: '持续失败（历史无通过记录）'},
            items: [
              {name: '登录成功', status: 'regressed', streak: 1,
               window_runs: 3, window_fails: 1, last_pass_text: '09-11 21:00',
               detail: '断言失败'},
              {name: '老毛病', status: 'persistent', streak: 5,
               window_runs: 4, window_fails: 4, last_pass_text: '—', detail: ''}
            ],
            notes: ['有 1 项是历史一直失败']})""")
        html = page.locator("#d_diff").inner_html()
        assert "登录成功" in html, "场景没渲染出来"
        assert "回归" in html, "判定标签没渲染出来"
        assert "连续失败" in html, "连续次数没显示"
        assert "不参与门禁判定" in html, "缺少「不参与门禁」的免责说明"
        # 无数据：要给引导文案，而不是空白
        page.evaluate("() => renderDiffPanel({has: false, baseline: "
                      "{available: false, reason: '尚未生成'}, items: []})")
        empty = page.locator("#d_diff").inner_html()
        assert "尚未生成" in empty, "无数据时没有引导文案"
        assert not errors, "新旧对比页面有 JS 报错：" + " | ".join(errors[:3])
    finally:
        page.close()
