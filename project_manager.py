"""多项目对接：公司各类项目接入 + 全流程测试 + 核心业务回归编排。

这是"软件测试智能体"面向多项目的统一入口。新建一个公司项目 = 填写项目信息（测试环境地址、
需求等），即可对该项目执行：
  - 全流程测试：需求 -> 用例 -> 接口自动化(指向本项目环境) -> 核心回归 -> 报告
  - 核心业务回归：只跑该项目 regression.yaml 声明的核心场景（适合常态化回归门禁）

子命令：
  create   交互式（或带参）填写项目信息，生成 project.yaml + requirements.md + regression.yaml
  list     列出已接入的项目
  info     查看某项目详情
  run      全流程测试：需求->用例 -> 接口自动化 -> 核心回归 -> 报告
  regression  仅核心业务回归
  dashboard   跨项目总览看板（各项目回归门禁状态）

核心回归支持三类回归项（详见 extensions/regression/run_regression.py）：
  api_smoke      声明式 HTTP 检查（method / path / expect_status）
  pytest_marker  复用已有 pytest 用例，按 marker 筛选（target + marker，如 -m smoke）
  pytest_node    复用已有 pytest 用例，指定 node id
推荐后两者：让"核心回归"与已写好的自动化用例同源，不必重复声明。

说明：
  - 密钥分离：project.yaml 仅存环境变量名，真实口令/token 在 .env（已被 .gitignore 忽略）。
  - 全流程中的"接口自动化"复用 extensions/api_testing（pytest + requests），通过 BASE_URL 指向本项目环境。
  - "核心回归"复用 extensions/regression/run_regression.py。

示例：
  python project_manager.py create --id mall-admin --name "商城后台管理系统" \\
      --base-url http://localhost:8080 --owner 张三 --auth-type form \\
      --login-url /admin/login --user-env MALL_ADMIN_USER --pass-env MALL_ADMIN_PASS
  python project_manager.py run mall-admin
  python project_manager.py regression mall-admin
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 打包成 exe 后 __file__ 指向临时目录，项目数据应落在 exe 所在目录，
# 因此支持用 STA_ROOT 环境变量显式指定仓库根（由 web_console/desktop.py 设置）。
ROOT = Path(os.environ.get("STA_ROOT") or Path(__file__).resolve().parent)
PROJECTS_DIR = ROOT / "projects"


def python_exe() -> str:
    """用于起子进程的 Python 解释器。

    正常情况就是 sys.executable；但打包成 exe 后 sys.executable 指向 exe 本身，
    无法用来跑 `python -m pytest`，此时按 STA_PYTHON -> PATH 里的 python 兜底。
    """
    import shutil
    if os.environ.get("STA_PYTHON"):
        return os.environ["STA_PYTHON"]
    if getattr(sys, "frozen", False):
        found = shutil.which("python") or shutil.which("python3")
        if found:
            return found
    return sys.executable
TODAY = datetime.date.today().isoformat()


def _load_dotenv(env_file: Path | None = None) -> None:
    """极简 .env 加载（零依赖）。

    密钥分离约定：project.yaml 只存环境变量【名】，真实口令/token 写在本项目的
    .env（已被 .gitignore 忽略，不入库）。本函数把 .env 读入 os.environ，
    供回归执行器按变量名取值。
    """
    p = env_file or (ROOT / ".env")
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ----------------------------------------------------------------------------
# 模板
# ----------------------------------------------------------------------------
PROJECT_YAML_TMPL = """\
project_id: {pid}
name: {name}
description: {description}
owner: {owner}
env:
  base_url: {base_url}
  auth:
    type: {auth_type}
    login_url: {login_url}
    username_env: {user_env}
    password_env: {pass_env}
    token_field: token
requirements_file: requirements.md
regression_file: regression.yaml
created_at: {today}
"""

REGRESSION_TMPL = """\
project_id: {pid}
# 核心业务回归：把核心场景逐项列出，执行器会真实请求并校验。
# type 支持三种：
#   api_smoke      声明式 HTTP 检查（method / path / expect_status）
#   pytest_marker  复用已有 pytest 用例，按 marker 筛选（target + marker）
#   pytest_node    复用已有 pytest 用例，指定 node id
# api_smoke 的 auth: required 表示需携带登录 token；
# body 支持 {{username}}/{{password}} 占位（运行期替换为凭据）。
# ⚠ 强烈建议配 expect_json：很多后端 HTTP 状态码恒为 200、成败写在 body 的业务码，
#   只断言状态码会产生"永远通过"的假绿。expect_json 支持点路径（如 data.token），
#   取值 __not_null__ 表示该字段必须非空。
core_business:
  - name: 健康检查
    type: api_smoke
    method: GET
    path: /
    expect_status: 200
  - name: 管理员登录冒烟
    type: api_smoke
    method: POST
    path: {login_url}
    body:
      username: "{{username}}"
      password: "{{password}}"
    expect_status: 200
    expect_json:
      code: 200              # 业务成功码（注意：不是 HTTP 状态码）
  # - name: 核心查询(需登录)
  #   type: api_smoke
  #   method: GET
  #   path: /admin/list
  #   auth: required
  #   expect_status: 200
  # 推荐：直接复用已写好的 pytest 用例做核心回归（与用例同源，不必重复声明）
  # - name: 登录核心用例(复用 pytest)
  #   type: pytest_marker
  #   target: extensions/api_testing
  #   marker: smoke
"""

REQUIREMENTS_TMPL = """\
# {name} · 项目需求

> 由 `python project_manager.py create` 生成，请补充本项目测试所需需求条目。
> 每条需求用编号或项目符号列出，后续 `project run <id>` 会自动转为测试用例。

1. 管理员可使用账号密码登录系统，登录失败有清晰提示
2. 管理员可创建新账号并校验唯一性
3. 管理员可查询账号列表并支持分页
"""


# ----------------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------------
def _ask(label: str, default: str, provided: Optional[str]) -> str:
    """优先用显式参数；否则若 stdin 是终端则交互询问；否则用默认。"""
    if provided is not None:
        return provided
    try:
        if sys.stdin.isatty():
            val = input(f"{label} [{default}]: ").strip()
            return val or default
    except Exception:
        pass
    return default


DISABLED_MARK = ".disabled"


def is_disabled(pdir: Path) -> bool:
    """项目是否停用。用标记文件而非改 project.yaml：避免重写时丢掉注释与格式。"""
    return (pdir / DISABLED_MARK).is_file()


def set_disabled(pdir: Path, disabled: bool) -> None:
    f = pdir / DISABLED_MARK
    if disabled:
        f.write_text("已停用：不参与回归与看板；删除本文件即可恢复。\n", encoding="utf-8")
    elif f.is_file():
        f.unlink()


def load_projects(include_disabled: bool = False) -> List[Path]:
    """列出项目目录。默认**跳过已停用**项目（看板/回归都基于它，停用即不参与门禁）。"""
    if not PROJECTS_DIR.is_dir():
        return []
    dirs = [p for p in PROJECTS_DIR.iterdir() if (p / "project.yaml").is_file()]
    if not include_disabled:
        dirs = [p for p in dirs if not is_disabled(p)]
    return sorted(dirs)


def load_project(pid: str) -> Dict[str, Any]:
    pdir = PROJECTS_DIR / pid
    py = pdir / "project.yaml"
    if not py.is_file():
        raise SystemExit(f"未找到项目 {pid}（{py} 不存在）。先运行 create。")
    try:
        import yaml
    except ImportError:
        raise SystemExit("需要 PyYAML：pip install pyyaml")
    return yaml.safe_load(py.read_text(encoding="utf-8")) or {}


# ----------------------------------------------------------------------------
# 子命令实现
# ----------------------------------------------------------------------------
def cmd_create(args: argparse.Namespace) -> None:
    pid = args.id or input("project_id（英文标识，如 mall-admin）: ").strip()
    if not pid:
        raise SystemExit("project_id 不能为空")
    pdir = PROJECTS_DIR / pid
    if (pdir / "project.yaml").is_file() and not args.force:
        raise SystemExit(f"项目 {pid} 已存在。用 --force 覆盖。")

    name = _ask("项目名称", pid, args.name)
    base_url = _ask("测试环境地址", "http://localhost:8080", args.base_url)
    owner = _ask("负责人", "未指定", args.owner)
    description = _ask("项目描述", name, args.description)
    auth_type = _ask("认证类型(none/form/bearer)", "none", args.auth_type)
    login_url = _ask("登录接口路径", "/admin/login", args.login_url)
    user_env = _ask("用户名环境变量名", f"{pid.upper().replace('-', '_')}_USER", args.user_env)
    pass_env = _ask("口令环境变量名", f"{pid.upper().replace('-', '_')}_PASS", args.pass_env)

    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "project.yaml").write_text(
        PROJECT_YAML_TMPL.format(
            pid=pid, name=name, description=description, owner=owner,
            base_url=base_url, auth_type=auth_type, login_url=login_url,
            user_env=user_env, pass_env=pass_env, today=TODAY,
        ),
        encoding="utf-8",
    )

    req_src = args.requirements_file
    if req_src and Path(req_src).is_file():
        (pdir / "requirements.md").write_text(
            Path(req_src).read_text(encoding="utf-8"), encoding="utf-8"
        )
        print(f"  已复制需求文件 -> {pdir / 'requirements.md'}")
    else:
        (pdir / "requirements.md").write_text(
            REQUIREMENTS_TMPL.format(name=name), encoding="utf-8"
        )
        print(f"  已生成需求模板 -> {pdir / 'requirements.md'}（请补充需求）")

    # 用 replace 而非 format：模板里的 {{username}} 是给回归执行器的占位符，
    # format 会把它转义成 {username}，导致凭据替换静默失效。
    (pdir / "regression.yaml").write_text(
        REGRESSION_TMPL.replace("{pid}", pid).replace("{login_url}", login_url),
        encoding="utf-8",
    )
    (pdir / "artifacts").mkdir(exist_ok=True)
    print(f"\n✅ 项目 {pid} 已创建：{pdir}")
    print("   下一步：")
    print(f"   1) 在 .env 中设置 {user_env} / {pass_env}（密钥不入库）")
    print(f"   2) 完善 {pdir / 'requirements.md'} 与 {pdir / 'regression.yaml'}")
    print(f"   3) python project_manager.py run {pid}    # 全流程")
    print(f"      python project_manager.py regression {pid}   # 仅核心回归")


def cmd_list(args: argparse.Namespace) -> None:
    projects = load_projects()
    if not projects:
        print("暂无已接入项目。运行 `python project_manager.py create` 新建。")
        return
    print(f"已接入项目（{len(projects)} 个）：")
    for p in projects:
        meta = load_project(p.name)
        print(f"  - {p.name:20s} {meta.get('name',''):20s} env={meta.get('env',{}).get('base_url','')}")


def cmd_info(args: argparse.Namespace) -> None:
    meta = load_project(args.id)
    print(f"项目：{args.id}")
    for k, v in meta.items():
        print(f"  {k}: {v}")


def _step_requirements(pid: str, pdir: Path) -> Optional[Path]:
    req_file = pdir / "requirements.md"
    out = pdir / "artifacts" / "cases.md"
    if not req_file.is_file():
        print("  [需求->用例] 跳过：无 requirements.md")
        return None
    sys.path.insert(0, str(ROOT / "extensions" / "requirements_to_cases"))
    import generate_cases as gc  # noqa: E402

    items = gc.parse_requirements(req_file.read_text(encoding="utf-8"))
    cases = gc.gen_cases(items)
    out.write_text(gc.to_markdown(cases, str(req_file)), encoding="utf-8")
    print(f"  [需求->用例] {len(items)} 条需求 -> {len(cases)} 条用例 -> {out}")
    return out


def _step_api(pid: str, pdir: Path, base_url: str) -> None:
    env = os.environ.copy()
    env["BASE_URL"] = base_url
    allure_dir = pdir / "artifacts" / "allure-results"
    allure_dir.mkdir(parents=True, exist_ok=True)
    print(f"  [接口自动化] 对 {base_url} 运行 pytest（不可达会自动 skip）...")
    r = subprocess.run(
        [python_exe(), "-m", "pytest", "extensions/api_testing", "-v",
         f"--alluredir={allure_dir}"],
        cwd=ROOT, env=env,
    )
    print(f"  [接口自动化] pytest 退出码 {r.returncode}")


def _step_regression(pid: str, pdir: Path) -> Dict[str, Any]:
    sys.path.insert(0, str(ROOT / "extensions" / "regression"))
    import run_regression as rr  # noqa: E402

    out_json = pdir / "artifacts" / "regression.json"
    print("  [核心回归] 执行核心业务场景...")
    summary = rr.run_regression(pdir / "project.yaml", pdir / "regression.yaml",
                                out_json, repo_root=ROOT)
    skipped = summary.get("skipped", 0)
    print(f"  [核心回归] 通过 {summary['passed']}/{summary['total']}"
          + (f"，跳过 {skipped}" if skipped else "")
          + (" ✅" if summary["all_pass"] else " ❌"))
    return summary


def _step_rerun(pid: str, pdir: Path, scene: str) -> Dict[str, Any]:
    """重跑单个核心场景：结果合并回 regression.json，并同步刷新 report.html。"""
    sys.path.insert(0, str(ROOT / "extensions" / "regression"))
    import run_regression as rr  # noqa: E402

    out_json = pdir / "artifacts" / "regression.json"
    summary = rr.rerun_one(pdir / "project.yaml", pdir / "regression.yaml",
                           out_json, scene, repo_root=ROOT)
    if summary.get("error"):
        print(f"  [单场景重跑] 失败：{summary['error']}", file=sys.stderr)
        return summary
    # 报告同步刷新：保留既有的需求->用例预览（cases.md 存在才带上）
    cases = pdir / "artifacts" / "cases.md"
    _step_report(pid, pdir, cases if cases.is_file() else None, summary)
    return summary


def _parse_cases(md_text: str) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """解析 cases.md：提取元信息 + Markdown 表格行。"""
    lines = md_text.strip().splitlines()
    meta: Dict[str, Any] = {"title": "测试用例（由需求生成）", "source": "", "generated": "", "count": 0}
    for line in lines[:8]:
        if line.startswith("# "):
            meta["title"] = line[2:].strip()
        elif line.startswith("- 来源："):
            meta["source"] = line[len("- 来源："):].strip()
        elif line.startswith("- 生成时间："):
            meta["generated"] = line[len("- 生成时间："):].strip()
        elif line.startswith("- 用例数："):
            try:
                meta["count"] = int(line[len("- 用例数："):].strip())
            except ValueError:
                pass

    table_start = -1
    for i, line in enumerate(lines):
        if line.startswith("|") and "---" in line:
            table_start = i - 1
            break
    if table_start < 0:
        return meta, []

    headers = [h.strip() for h in lines[table_start].split("|")[1:-1] if h.strip()]
    rows: List[Dict[str, str]] = []
    for line in lines[table_start + 2:]:
        if not line.strip() or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) < max(2, len(headers)):
            continue
        rows.append(dict(zip(headers, cells)))
    return meta, rows


def _cases_html(md_text: str) -> Tuple[str, int]:
    """把 cases.md 渲染为分组卡片式 HTML；返回 (html, count)。"""
    import html as _html
    meta, rows = _parse_cases(md_text)
    if not rows:
        return f"<pre>{_html.escape(md_text)}</pre>", 0

    from collections import defaultdict
    groups: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in rows:
        rid = row.get("id", "")
        parts = rid.split("-")
        key = "-".join(parts[:2]) if len(parts) >= 2 else rid
        groups[key].append(row)

    def _type_badge(t: str) -> str:
        t = t.strip()
        cls = "case-f" if t == "功能" else ("case-b" if t == "边界" else ("case-n" if t == "异常" else "case-x"))
        return f"<span class='badge {cls}'>{_html.escape(t)}</span>"

    def _prio_badge(p: str) -> str:
        p = p.strip().upper()
        cls = "p1" if p == "P1" else ("p2" if p == "P2" else "px")
        return f"<span class='badge prio {cls}'>{_html.escape(p)}</span>"

    def _auto_badge(a: str) -> str:
        a = (a or "").strip()
        al = a.lower()
        fw = ("pytest", "playwright", "selenium", "appium", "jmeter", "curl", "requests", "postman")
        if any(k in al for k in fw):
            cls, label, title = "auto-yes", "可自动化", f"可自动化 · {a}"
        elif a in ("可", "可自动化", "是", "✓", "Y", "yes"):
            cls, label, title = "auto-yes", "可自动化", "可自动化"
        elif a in ("部分", "部分自动化", "半"):
            cls, label, title = "auto-part", "部分自动化", "部分自动化"
        elif a in ("否", "不可", "不可自动化", "✗", "N", "no"):
            cls, label, title = "auto-no", "暂不可自动化", "暂不可自动化"
        else:
            cls, label, title = "auto-unknown", (a or "未标注"), (a or "未标注")
        icon = {
            "auto-yes": "<svg class='ai' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'><path d='M20 6L9 17l-5-5'/></svg>",
            "auto-part": "<svg class='ai' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'><path d='M5 12h14'/></svg>",
            "auto-no": "<svg class='ai' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'><path d='M18 6L6 18M6 6l12 12'/></svg>",
            "auto-unknown": "<svg class='ai' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.4' stroke-linecap='round' stroke-linejoin='round'><circle cx='12' cy='12' r='9'/><path d='M12 8v4M12 16h.01'/></svg>",
        }.get(cls, "")
        return f"<span class='case-auto {cls}' title='{_html.escape(title)}'>{icon}<span>{_html.escape(label)}</span></span>"

    cards = []
    for req_key, cases in sorted(groups.items()):
        # 取功能用例的标题作为需求标题，并去掉尾标
        title = cases[0].get("标题", "")
        for suffix in ("（功能）", "（边界）", "（异常）"):
            if title.endswith(suffix):
                title = title[: -len(suffix)].strip()
                break
        title_html = _html.escape(title) or _html.escape(req_key)

        case_items = []
        for c in cases:
            cid = _html.escape(c.get("id", ""))
            ctype = _type_badge(c.get("类型", ""))
            prio = _prio_badge(c.get("优先级", ""))
            pre = _html.escape(c.get("前置", ""))
            step = _html.escape(c.get("步骤", ""))
            expect = _html.escape(c.get("预期", ""))
            auto = _auto_badge(c.get("可自动化", ""))
            case_items.append(
                f"<div class='case'>"
                f"<div class='case-line'>"
                f"<span class='case-id'>{cid}</span>{ctype}{prio}"
                f"{auto}"
                f"</div>"
                f"<div class='case-body'>"
                f"<div class='case-row'><span class='k'>前置</span><span class='v'>{pre}</span></div>"
                f"<div class='case-row'><span class='k'>步骤</span><span class='v'>{step}</span></div>"
                f"<div class='case-row'><span class='k'>预期</span><span class='v'>{expect}</span></div>"
                f"</div>"
                f"</div>"
            )

        cards.append(
            f"<div class='req-card'>"
            f"<div class='req-head'>"
            f"<span class='req-key'>{_html.escape(req_key)}</span>"
            f"<span class='req-title'>{title_html}</span>"
            f"<span class='req-count'>{len(cases)} 条用例</span>"
            f"</div>"
            f"<div class='case-list'>{''.join(case_items)}</div>"
            f"</div>"
        )

    return (
        f"<div class='cases-meta'>"
        f"<div><span class='meta-k'>来源</span><span class='meta-v'>{_html.escape(meta['source'])}</span></div>"
        f"<div><span class='meta-k'>生成时间</span><span class='meta-v'>{_html.escape(meta['generated'])}</span></div>"
        f"<div><span class='meta-k'>用例数</span><span class='meta-v'>{meta['count']}</span></div>"
        f"</div>"
        f"<div class='req-grid'>{''.join(cards)}</div>"
    ), meta.get("count", len(rows))


def _step_report(pid: str, pdir: Path, cases_md: Optional[Path], reg: Dict[str, Any]) -> Path:
    out = pdir / "artifacts" / "report.html"
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    cases_html = ""
    cases_count = 0
    if cases_md:
        cases_html, cases_count = _cases_html(cases_md.read_text(encoding="utf-8"))

    def _row_cls(result: Any) -> str:
        v = str(result)
        return "ok" if v == "PASS" else ("skip" if v == "SKIP" else "bad")

    def _result_badge(result: Any) -> str:
        v = str(result)
        if v == "PASS":
            return "<span class='tag ok'>通过</span>"
        if v == "SKIP":
            return "<span class='tag skip'>跳过</span>"
        return "<span class='tag bad'>失败</span>"

    results = reg.get("results", [])
    rows = "".join(
        f"<tr class='{_row_cls(r.get('result'))}'>"
        f"<td class='name'>{_h(r.get('name'))}</td>"
        f"<td class='method'>{_h(r.get('method'))}</td>"
        f"<td class='url'>{_h(r.get('url'))}</td>"
        f"<td class='actual'>{_h(r.get('status_code'))}</td>"
        f"<td class='expect'>{_h(r.get('expect'))}</td>"
        f"<td class='result'>{_result_badge(r.get('result'))}</td>"
        f"<td class='detail'>{_h(r.get('detail') or '')}</td></tr>"
        for r in results
    )
    if results:
        table_html = (
            "<div class='table-wrap'><table class='reg-table'>"
            "<thead><tr><th>场景</th><th>方法</th><th>URL</th><th>实际</th><th>期望</th><th>结果</th><th>说明</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>"
        )
    else:
        table_html = "<div class='reg-empty'>本次没有可执行的核心回归场景（环境不可达或尚未配置回归项）。</div>"

    skip_txt = f"，跳过 {reg.get('skipped', 0)}" if reg.get("skipped") else ""
    all_pass = bool(reg.get("all_pass"))
    gate_badge = "<span class='gate ok'>全部通过</span>" if all_pass else "<span class='gate bad'>存在失败</span>"
    gate_desc = "最近一次核心回归符合门禁" if all_pass else "最近一次核心回归未通过门禁"

    html_doc = f"""<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>测试报告 - {pid}</title>
<style>
:root{{
  --ink:#0f172a; --text:#1f2937; --muted:#64748b; --muted-2:#94a3b8;
  --bg:#f6f8fb; --panel:#ffffff; --surface:#f8fafc; --border:#e6eaf0; --border-2:#eef2f7;
  --primary:#6366f1; --primary-600:#4f46e5; --primary-soft:#eef2ff;
  --ok:#059669; --ok-soft:#d1fae5; --bad:#dc2626; --bad-soft:#fee2e2;
  --warn:#d97706; --warn-soft:#fef3c7; --info:#2563eb; --info-soft:#dbeafe;
  --purple:#7c3aed; --purple-soft:#f3e8ff;
  --shadow-sm:0 1px 2px rgba(15,23,42,.04),0 1px 3px rgba(15,23,42,.05);
  --shadow-md:0 1px 2px rgba(15,23,42,.04),0 12px 32px rgba(15,23,42,.08);
  --radius:14px; --radius-sm:10px;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei","PingFang SC",Roboto,Helvetica,Arial,sans-serif;
  --mono:ui-monospace,"Cascadia Code","JetBrains Mono",Consolas,"Courier New",monospace;
}}
*{{box-sizing:border-box;}}
body{{font-family:var(--sans);background:var(--bg);color:var(--text);margin:0;padding:0;line-height:1.6;-webkit-font-smoothing:antialiased;}}
.container{{max-width:1120px;margin:0 auto;padding:32px 24px 48px;}}
header{{margin-bottom:28px;}}
header h1{{font-size:26px;font-weight:800;color:var(--ink);margin:0 0 8px;letter-spacing:-.3px;}}
header .subtitle{{font-size:14px;color:var(--muted);}}
.summary-card{{display:flex;align-items:center;gap:20px;flex-wrap:wrap;background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);padding:20px 24px;margin-bottom:24px;box-shadow:var(--shadow-sm);}}
.summary-item{{display:flex;flex-direction:column;gap:2px;}}
.summary-item .k{{font-size:12px;color:var(--muted);font-weight:600;}}
.summary-item .v{{font-size:20px;font-weight:800;color:var(--ink);}}
.gate{{display:inline-flex;align-items:center;gap:6px;font-size:13px;font-weight:700;padding:6px 12px;border-radius:999px;}}
.gate.ok{{background:var(--ok-soft);color:var(--ok);}}
.gate.bad{{background:var(--bad-soft);color:var(--bad);}}
.card{{background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);padding:22px 24px;margin-bottom:20px;box-shadow:var(--shadow-sm);}}
.card-title{{font-size:16px;font-weight:700;color:var(--ink);margin:0 0 16px;display:flex;align-items:center;gap:8px;}}
.card-title .count{{font-size:12px;color:var(--muted);font-weight:600;background:var(--surface);padding:2px 8px;border-radius:999px;border:1px solid var(--border-2);}}
.table-wrap{{overflow-x:auto;border:1px solid var(--border);border-radius:var(--radius-sm);}}
.reg-table{{width:100%;border-collapse:collapse;font-size:13px;}}
.reg-table th{{text-align:left;background:var(--surface);color:var(--muted);font-weight:700;font-size:11px;text-transform:uppercase;letter-spacing:.5px;padding:12px 14px;border-bottom:1px solid var(--border);white-space:nowrap;}}
.reg-table td{{padding:12px 14px;border-bottom:1px solid var(--border-2);vertical-align:top;}}
.reg-table tr:last-child td{{border-bottom:0;}}
.reg-table tr:hover td{{background:rgba(99,102,241,.03);}}
.reg-table .name{{font-weight:700;color:var(--ink);min-width:140px;}}
.reg-table .method{{font-family:var(--mono);font-size:11.5px;color:var(--muted);white-space:nowrap;}}
.reg-table .url{{font-family:var(--mono);font-size:11.5px;color:var(--muted-2);word-break:break-all;max-width:260px;}}
.reg-table .actual{{font-family:var(--mono);font-size:12px;color:var(--ink);white-space:nowrap;}}
.reg-table .expect{{font-family:var(--mono);font-size:11.5px;color:var(--muted);max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
.reg-table .expect:hover{{white-space:normal;overflow:visible;background:var(--surface);}}
.reg-table .detail{{font-family:var(--mono);font-size:11.5px;color:var(--bad);max-width:260px;white-space:pre-wrap;}}
.reg-empty{{text-align:center;color:var(--muted);padding:48px 0;font-size:14px;}}
.tag{{display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:700;padding:4px 10px;border-radius:999px;}}
.tag.ok{{background:var(--ok-soft);color:var(--ok);}}
.tag.bad{{background:var(--bad-soft);color:var(--bad);}}
.tag.skip{{background:var(--warn-soft);color:var(--warn);}}
.cases-meta{{display:flex;gap:24px;flex-wrap:wrap;margin-bottom:18px;}}
.cases-meta div{{display:flex;align-items:center;gap:8px;}}
.meta-k{{font-size:12px;color:var(--muted);}}
.meta-v{{font-size:13px;color:var(--ink);font-weight:600;}}
.req-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px;}}
.req-card{{background:var(--panel);border:1px solid var(--border);border-radius:var(--radius-sm);padding:16px 18px;transition:box-shadow .2s,border-color .2s;}}
.req-card:hover{{box-shadow:var(--shadow-md);border-color:#d6deea;}}
.req-head{{display:flex;align-items:flex-start;gap:10px;margin-bottom:12px;}}
.req-key{{font-family:var(--mono);font-size:11px;font-weight:700;color:var(--primary);background:var(--primary-soft);padding:3px 8px;border-radius:6px;flex-shrink:0;margin-top:2px;}}
.req-title{{font-size:14px;font-weight:700;color:var(--ink);flex:1;min-width:0;line-height:1.45;}}
.req-count{{font-size:11px;color:var(--muted);white-space:nowrap;flex-shrink:0;margin-top:2px;}}
.case-list{{display:flex;flex-direction:column;gap:10px;}}
.case{{background:var(--surface);border:1px solid var(--border-2);border-radius:10px;padding:12px 14px;}}
.case-line{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px;}}
.case-id{{font-family:var(--mono);font-size:11px;color:var(--muted-2);}}
.badge{{display:inline-flex;align-items:center;font-size:10.5px;font-weight:700;padding:2px 8px;border-radius:999px;flex-shrink:0;}}
.badge.case-f{{background:#dbeafe;color:#1d4ed8;}}
.badge.case-b{{background:#fef3c7;color:#b45309;}}
.badge.case-n{{background:#fee2e2;color:#b91c1c;}}
.badge.case-x{{background:var(--border-2);color:var(--muted);}}
.badge.prio.p1{{background:#dcfce7;color:#15803d;}}
.badge.prio.p2{{background:#e0e7ff;color:#4338ca;}}
.badge.prio.px{{background:var(--border-2);color:var(--muted);}}
.case-auto{{margin-left:auto;display:inline-flex;align-items:center;gap:4px;font-size:10.5px;font-weight:700;padding:3px 9px;border-radius:999px;white-space:nowrap;}}
.case-auto .ai{{width:11px;height:11px;display:block;flex-shrink:0;}}
.case-auto.auto-yes{{background:var(--ok-soft);color:var(--ok);}}
.case-auto.auto-part{{background:var(--warn-soft);color:var(--warn);}}
.case-auto.auto-no{{background:var(--bad-soft);color:var(--bad);}}
.case-auto.auto-unknown{{background:var(--border-2);color:var(--muted);}}
.case-body{{display:flex;flex-direction:column;gap:8px;}}
.case-row{{display:grid;grid-template-columns:44px 1fr;gap:10px;align-items:flex-start;}}
.case-row .k{{font-size:11px;color:var(--muted);font-weight:700;padding-top:1px;}}
.case-row .v{{font-size:12.5px;color:var(--text);line-height:1.6;}}
.empty-case{{color:var(--muted);font-size:14px;padding:20px 0;text-align:center;}}
footer{{margin-top:24px;padding-top:18px;border-top:1px solid var(--border);font-size:12px;color:var(--muted-2);display:flex;justify-content:space-between;align-items:center;}}
footer code{{font-family:var(--mono);background:var(--surface);padding:2px 6px;border-radius:4px;}}
@media (max-width:720px){{
  .container{{padding:20px 16px;}}
  .req-grid{{grid-template-columns:1fr;}}
  .summary-card{{flex-direction:column;align-items:flex-start;}}
}}
</style>
</head><body>
<div class='container'>
<header>
  <h1>项目测试报告 · {pid}</h1>
  <div class='subtitle'>生成时间：{now} · 核心回归通过 {reg.get('passed')}/{reg.get('total')}{skip_txt}</div>
</header>

<div class='summary-card'>
  <div class='summary-item'><span class='k'>门禁状态</span><span class='v'>{gate_badge}</span></div>
  <div class='summary-item'><span class='k'>场景总数</span><span class='v'>{reg.get('total', 0)}</span></div>
  <div class='summary-item'><span class='k'>通过</span><span class='v' style='color:var(--ok)'>{reg.get('passed', 0)}</span></div>
  <div class='summary-item'><span class='k'>失败</span><span class='v' style='color:var(--bad)'>{reg.get('failed', 0)}</span></div>
  <div class='summary-item' style='margin-left:auto;'><span class='k' style='text-align:right;'>{gate_desc}</span></div>
</div>

<div class='card'>
  <div class='card-title'>核心业务回归结果 <span class='count'>{len(results)}</span></div>
  {table_html}
</div>

<div class='card'>
  <div class='card-title'>由需求生成的用例（预览） <span class='count'>{cases_count}</span></div>
  {cases_html if cases_html else "<div class='empty-case'>尚未生成用例，需先执行一次全流程。</div>"}
</div>

<footer>
  <span>Allure 原始结果见 <code>artifacts/allure-results/</code>（运行 <code>allure serve</code> 查看）</span>
  <span>测试智能体 · {pid}</span>
</footer>
</div>
</body></html>"""
    out.write_text(html_doc, encoding="utf-8")
    print(f"  [报告] 已生成 {out}")
    return out


def _h(s: Any) -> str:
    import html as _html
    return _html.escape(str(s))


def cmd_run(args: argparse.Namespace) -> None:
    meta = load_project(args.id)
    pdir = PROJECTS_DIR / args.id
    base_url = (meta.get("env", {}) or {}).get("base_url", "http://localhost:8080")
    print(f"== 全流程测试：{args.id}（环境 {base_url}）==")
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "artifacts").mkdir(exist_ok=True)
    cases = _step_requirements(args.id, pdir)
    _step_api(args.id, pdir, base_url)
    reg = _step_regression(args.id, pdir)
    _step_report(args.id, pdir, cases, reg)
    print(f"\n✅ 全流程完成。报告：{pdir / 'artifacts' / 'report.html'}")


def cmd_regression(args: argparse.Namespace) -> None:
    meta = load_project(args.id)
    pdir = PROJECTS_DIR / args.id
    base_url = (meta.get("env", {}) or {}).get("base_url", "http://localhost:8080")
    print(f"== 核心业务回归：{args.id}（环境 {base_url}）==")
    reg = _step_regression(args.id, pdir)
    sys.exit(0 if reg["all_pass"] else 1)


def cmd_rerun(args: argparse.Namespace) -> None:
    pdir = PROJECTS_DIR / args.id
    print(f"== 单场景重跑：{args.id} / {args.scene} ==")
    reg = _step_rerun(args.id, pdir, args.scene)
    # 退出码语义：本次重跑是否执行成功，而非整项目门禁。
    # 重跑只影响一个场景，其它场景的历史失败不该算到本次头上（否则任务永远显示"失败"）。
    # 整体门禁请仍以 `regression` / `dashboard` 的退出码为准。
    sys.exit(0 if not reg.get("error") else 1)


def cmd_dashboard(args: argparse.Namespace) -> None:
    """跨项目总览看板：汇总各项目最近一次核心回归结果，作为交付质量门禁。"""
    pdirs = load_projects()
    if not pdirs:
        print("尚未接入任何项目。先执行：python project_manager.py create")
        return

    rows: List[Dict[str, Any]] = []
    for pdir in pdirs:
        meta = load_project(pdir.name)
        reg_file = pdir / "artifacts" / "regression.json"
        reg: Optional[Dict[str, Any]] = None
        if reg_file.is_file():
            try:
                reg = json.loads(reg_file.read_text(encoding="utf-8"))
            except Exception:
                reg = None
        rows.append({
            "pid": meta.get("project_id", pdir.name),
            "name": meta.get("name", ""),
            "owner": meta.get("owner", ""),
            "base_url": (meta.get("env", {}) or {}).get("base_url", ""),
            "reg": reg,
        })

    print(f"== 跨项目总览（{len(rows)} 个项目）==")
    for r in rows:
        reg = r["reg"]
        if not reg:
            print(f"  - {r['pid']:<16}{r['name']:<22}未执行回归")
        else:
            gate = "✅ 通过" if reg.get("all_pass") else "❌ 未通过"
            print(f"  - {r['pid']:<16}{r['name']:<22}"
                  f"通过 {reg.get('passed', 0)}/{reg.get('total', 0)}"
                  f"（跳过 {reg.get('skipped', 0)}）  {gate}")

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    trs = []
    for r in rows:
        reg = r["reg"]
        if not reg:
            cls, badge = "skip", "未执行"
            detail = "<span class='muted'>尚未运行 run / regression</span>"
        else:
            cls = "ok" if reg.get("all_pass") else "bad"
            badge = "通过 ✅" if reg.get("all_pass") else "未通过 ❌"
            detail = (f"通过 {reg.get('passed', 0)}/{reg.get('total', 0)}"
                      f"（跳过 {reg.get('skipped', 0)}）")
        trs.append(
            f"<tr class='{cls}'><td><b>{_h(r['pid'])}</b></td>"
            f"<td>{_h(r['name'])}</td><td>{_h(r['owner'])}</td>"
            f"<td><code>{_h(r['base_url'])}</code></td>"
            f"<td>{detail}</td><td>{badge}</td>"
            f"<td><a href='projects/{_h(r['pid'])}/artifacts/report.html'>项目报告</a></td></tr>"
        )

    html_doc = f"""<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>
<title>跨项目测试总览</title>
<style>body{{font-family:-apple-system,'Microsoft YaHei',sans-serif;margin:32px;color:#1f2937;}}
h1{{color:#4f46e5;}} .meta{{color:#6b7280;font-size:13px;}}
table{{border-collapse:collapse;width:100%;margin-top:16px;}}
th,td{{border:1px solid #e5e7eb;padding:8px 10px;font-size:13px;text-align:left;}}
th{{background:#f3f4f6;}}
tr.ok td:nth-child(6){{color:#047857;font-weight:600;}}
tr.bad td:nth-child(6){{color:#b91c1c;font-weight:600;}}
tr.skip td:nth-child(6){{color:#b45309;font-weight:600;}}
.muted{{color:#9ca3af;}}</style>
</head><body>
<h1>跨项目测试总览</h1>
<div class='meta'>生成时间：{now} · 项目数：{len(rows)} · 数据来源：各项目最近一次核心回归</div>
<table><tr><th>项目ID</th><th>名称</th><th>负责人</th><th>测试环境</th><th>核心回归</th><th>门禁</th><th>明细</th></tr>
{''.join(trs)}
</table>
<div class='meta'>重新生成：python project_manager.py dashboard</div>
</body></html>"""
    out = ROOT / "projects_dashboard.html"
    out.write_text(html_doc, encoding="utf-8")
    print(f"\n看板已生成：{out}")


def run_pipeline(req_file: str, run_api: bool = False, api_base: Optional[str] = None) -> Path:
    """统一流水线：需求 -> 用例 -> [接口自动化] -> 报告总览。

    供 CLI ``run`` 与轻量入口（software_testing_agent.py）共用，消除双入口漂移。
    不依赖 LLM；有 LLM key 时需求→用例可经基座 ``make_llm`` 增强（见 generate_cases._llm_generate）。
    """
    # 1) 需求 -> 用例
    sys.path.insert(0, str(ROOT / "extensions" / "requirements_to_cases"))
    import generate_cases as gc  # noqa: F401  (延迟导入，避免基座依赖常驻)
    text = Path(req_file).read_text(encoding="utf-8")
    items = gc.parse_requirements(text)
    cases = gc.gen_cases(items)
    cases_md = ROOT / "extensions" / "requirements_to_cases" / "cases.md"
    cases_md.write_text(gc.to_markdown(cases, req_file), encoding="utf-8")
    print(f"[1/3] 需求→用例：解析 {len(items)} 条需求，生成 {len(cases)} 条用例 → {cases_md}")

    # 2) 接口自动化（可选）
    if run_api:
        env = os.environ.copy()
        if api_base:
            env["BASE_URL"] = api_base
        print("[2/3] 接口自动化：运行 pytest（被测服务不可达会自动 skip）...")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "extensions/api_testing", "-v",
             "--alluredir=allure-results"],
            cwd=ROOT, env=env,
        )
        print(f"[2/3] 接口自动化：pytest 退出码 {result.returncode}")
    else:
        print("[2/3] 接口自动化：跳过（未加 --run-api）")

    # 3) 报告总览
    sys.path.insert(0, str(ROOT / "extensions" / "reporting"))
    import generate_report as gr  # noqa: F401
    html = gr.render()
    out = ROOT / "test_report_index.html"
    out.write_text(html, encoding="utf-8")
    print(f"[3/3] 报告聚合：已生成 {out}")
    return out


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="软件测试智能体 · 多项目对接入口")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="新建项目（填写项目信息）")
    c.add_argument("--id", help="项目英文标识")
    c.add_argument("--name")
    c.add_argument("--base-url")
    c.add_argument("--owner")
    c.add_argument("--description")
    c.add_argument("--auth-type", dest="auth_type")
    c.add_argument("--login-url", dest="login_url")
    c.add_argument("--user-env", dest="user_env")
    c.add_argument("--pass-env", dest="pass_env")
    c.add_argument("--requirements-file", dest="requirements_file", help="直接导入需求文件(.md)")
    c.add_argument("--force", action="store_true", help="覆盖已存在项目")
    c.set_defaults(func=cmd_create)

    sub.add_parser("list", help="列出已接入项目").set_defaults(func=cmd_list)

    sub.add_parser("dashboard", help="跨项目总览看板（各项目回归门禁状态）").set_defaults(func=cmd_dashboard)

    i = sub.add_parser("info", help="查看项目详情")
    i.add_argument("id")
    i.set_defaults(func=cmd_info)

    r = sub.add_parser("run", help="全流程测试（需求->用例->接口->回归->报告）")
    r.add_argument("id")
    r.set_defaults(func=cmd_run)

    g = sub.add_parser("regression", help="仅核心业务回归")
    g.add_argument("id")
    g.set_defaults(func=cmd_regression)

    rs = sub.add_parser("rerun", help="重跑单个核心场景（结果合并回回归报告，不覆盖）")
    rs.add_argument("id")
    rs.add_argument("--scene", required=True, help="场景名称，需与 regression.yaml 中 name 一致")
    rs.set_defaults(func=cmd_rerun)
    return ap


def main() -> None:
    _load_dotenv()   # 密钥分离：先载入 .env，后续按变量名取真实凭据
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
