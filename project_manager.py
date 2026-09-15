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
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 打包成 exe 后 __file__ 指向临时目录，项目数据应落在 exe 所在目录，
# 因此支持用 STA_ROOT 环境变量显式指定仓库根（由 web_console/desktop.py 设置）。
ROOT = Path(os.environ.get("STA_ROOT") or Path(__file__).resolve().parent)
PROJECTS_DIR = ROOT / "projects"

# 统一日志出口（可观测性）：extensions/common/obs.py。
# 显式把 extensions/ 挂进 sys.path 再 import —— 与下面 `_common()` 惰性引入同一目的，
# 让根目录脚本也能取到共享实现层。obs 只依赖标准库，导入成本可忽略。
# 见 docs/HARNESS_ARCHITECTURE_REVIEW.md §4.2 / §7 序 3。
_EXTENSIONS_DIR = str(ROOT / "extensions")
if _EXTENSIONS_DIR not in sys.path:
    sys.path.insert(0, _EXTENSIONS_DIR)
from common.obs import get_logger, adopt_env_run_id, RUN_ID_ENV          # noqa: E402
# 阶段注册表 + 依赖拓扑执行：把"阶段顺序"里的隐式约束（diff 必须在 snapshot 之前）
# 变成显式 requires。见 docs/HARNESS_ARCHITECTURE_REVIEW.md §4.4 / §7 序 7。
from common.pipeline import Stage, resolve_order, run_stages            # noqa: E402
# 效率口径的**唯一定义处**（§10 #2）：进程内墙钟按阶段分解 + 合计。
# 两条硬口径：未执行的阶段记 None 而非 0（"没跑"不等于"跑得飞快"）；
# "人力节省"属**不可测**项，显式声明，不给任何近似值。
from common import timing as timing_mod                                 # noqa: E402

log = get_logger("project_manager")


def _common(mod: str):
    """取用 `extensions/common` 下的共享实现模块（跨扩展的**唯一定义处**）。

    读 YAML / 认证解析 / .env 加载 / cases.md 行解析曾在多个模块里各自复刻，
    现统一收敛到 extensions/common（见 docs/HARNESS_ARCHITECTURE_REVIEW.md §4.1）。

    惰性引入：把 extensions/ 加入 sys.path 后再 import，避免在模块顶层引入
    可选依赖；这也是本文件既有的风格（用到时才把目录塞进 sys.path）。
    """
    import importlib
    ext = str(ROOT / "extensions")
    if ext not in sys.path:
        sys.path.insert(0, ext)
    return importlib.import_module(f"common.{mod}")


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
    """载入 .env（密钥分离）——实现收敛到 `extensions/common/auth.load_dotenv`。

    project.yaml 只存环境变量【名】，真实口令/token 写在本项目 .env
    （已被 .gitignore 忽略，不入库）。本函数把 .env 读入 os.environ，
    供回归执行器按变量名取值。
    """
    _common("auth").load_dotenv(env_file)


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
  # ⑤ Web UI 冒烟的地址（前端地址，通常与接口地址不同端口/域名）。
  #   不写则 Web 冒烟回退用 base_url。示例：http://localhost:8090
  # web_base_url: {base_url}
  auth:
    type: {auth_type}
    login_url: {login_url}
    username_env: {user_env}
    password_env: {pass_env}
    token_field: token
requirements_file: requirements.md
regression_file: regression.yaml
web_file: web.yaml        # ⑤ Web UI 冒烟场景（Playwright 声明式 YAML）
# ④ 性能与安全冒烟（可选）。整段不写也能跑：压测目标会自动从 regression.yaml
#   的只读接口派生，安全检查用默认清单。需要更精细控制时再取消注释。
# perf_security:
#   perf:
#     users: 10            # 并发虚拟用户
#     iterations: 5        # 每用户请求次数（总量 = users × iterations）
#     warmup: 2            # 预热次数，不计入统计
#     thresholds:          # 留空 / 0 表示不做阈值判定
#       p95_ms: 800
#       p99_ms: 1500
#       max_error_rate: 0.01   # 比例，0.01 = 1%
#       min_rps: 5
#     targets:             # 不写则从 regression.yaml 派生（写操作默认跳过）
#       - name: 管理员列表
#         method: GET
#         path: /admin/list
#         auth: required
#         expect_code: 200     # 业务码；不写则只看 HTTP 状态码
#   security:
#     protected:           # 未授权访问检查的靶子；不写则从 regression.yaml 派生
#       - name: 当前管理员信息
#         method: GET
#         path: /admin/info
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

WEB_YAML_TMPL = """\
project_id: {pid}
# ↑ 本模板用 str.replace 渲染（不是 format）：模板里大量 { } 是 YAML 语法，
#   若用 format 会被当成占位符吃掉/报错——与 REGRESSION_TMPL 同一个坑。
# Web UI 冒烟：浏览器里的关键用户路径，声明式书写、可做 CI 门禁。
# 执行：python project_manager.py web {pid} [--only 场景名] [--headed] [--browser firefox]
#
# 三条硬约定（写场景前请先读，否则门禁会失真）：
# 1) 每个场景至少要有一个 expect_* 断言。只操作不断言的场景结果记 SKIP（不算绿）——
#    "点完就走"的场景只会永远通过，是门禁里最危险的东西。
# 2) 定位器优先级：data-test-subj > aria-label > 可见文本 > 语义 role。
#    禁止 XPath 与位置选择器（:nth-child / :nth-of-type / 裸 div/span 链）——
#    布局一改就碎，产出的是假红噪音。用了会被判为"配置问题"并拒绝执行。
# 3) 环境不可达 → 全部 SKIP、门禁不通过（防 CI 假绿）；
#    但 HTTP 4xx/5xx 算产品缺陷 → FAIL。两者不要混。
#
# 可用步骤：
#   goto / click / fill / press / check / uncheck / hover / select_option
#   scroll_into_view / wait_for / wait_for_hidden / wait_for_url / wait_for_load_state
#   expect_visible / expect_hidden / expect_text / expect_value / expect_count
#   expect_url / expect_title / screenshot
# 取值支持 {{username}} / {{password}} 占位（运行期从 .env 替换，不入库）。
web:
  # base_url 不写则回退 project.yaml 的 env.web_base_url / env.base_url
  # base_url: http://localhost:8090
  browser: chromium          # chromium | firefox | webkit
  headless: true
  timeout_ms: 15000
  retries: 0                 # 默认不重试：重试会吃掉偶发缺陷的证据；需要抗抖动再调 1~2
  viewport: {width: 1440, height: 900}
  launch_args: []            # 崩溃逃生口，如 ["--no-sandbox","--disable-gpu","--disable-dev-shm-usage"]
  screenshot_on_failure: true

  scenarios:
    # 示例：请按真实前端改选择器与断言，跑通后再纳入门禁。
    - name: Web 首页可访问
      tags: [smoke]
      steps:
        - goto: /
        - expect_visible: "body"
        - screenshot: home
    # - name: 管理员登录进入后台
    #   tags: [smoke]
    #   steps:
    #     - goto: /login
    #     - fill: {selector: "[data-test-subj='username']", value: "{{username}}"}
    #     - fill: {selector: "[data-test-subj='password']", value: "{{password}}"}
    #     - click: "[data-test-subj='submit']"
    #     - wait_for_url: "**/home"
    #     - expect_visible: "[data-test-subj='sidebar']"
    #     - expect_text: {selector: "h1", contains: "工作台"}
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
        pass  # 可忽略：非终端环境（管道 / CI）本就不该交互，按默认值继续是预期行为
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
        return _common("yamlio").load_yaml(py)      # 唯一定义处，见 extensions/common
    except RuntimeError as e:                        # 未安装 PyYAML
        raise SystemExit(str(e))


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
    # Web 冒烟场景模板（同样是 replace 渲染：YAML 里全是 { }，format 会踩坑）
    (pdir / "web.yaml").write_text(
        WEB_YAML_TMPL.replace("{pid}", pid), encoding="utf-8",
    )
    # 长期记忆：项目知识库模板（人工维护的业务取值约定）。
    # 走 ensure_template 而非直接 write —— 已存在时绝不覆盖用户写的内容。
    try:
        sys.path.insert(0, str(ROOT / "extensions" / "memory"))
        import knowledge as kn  # noqa: E402
        if kn.ensure_template(pdir):
            print(f"  已生成知识库模板 -> {pdir / 'knowledge.md'}（可选，写了才生效）")
    except Exception as e:
        # 必须出声：项目已建成、但知识库模板没生成 —— 用户不会发现少了一个文件，
        # "长期记忆"这条能力就此静默失效。
        log.warning("  知识库模板生成失败（项目已创建，模板缺失）：%s", e)
    (pdir / "artifacts").mkdir(exist_ok=True)
    print(f"\n✅ 项目 {pid} 已创建：{pdir}")
    print("   下一步：")
    print(f"   1) 在 .env 中设置 {user_env} / {pass_env}（密钥不入库）")
    print(f"   2) 完善 {pdir / 'requirements.md'} 与 {pdir / 'regression.yaml'}")
    print(f"   3) python project_manager.py run {pid}    # 全流程")
    print(f"      python project_manager.py regression {pid}   # 仅核心回归")
    print(f"   可选：按前端真实选择器改 {pdir / 'web.yaml'}，再跑 "
          f"`python project_manager.py web {pid}`")


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


def _step_requirements(pid: str, pdir: Path, use_llm: bool = False,
                       agentic: bool = False,
                       extra_context: Optional[str] = None,
                       injected: Optional[Dict] = None) -> Optional[Path]:
    req_file = pdir / "requirements.md"
    out = pdir / "artifacts" / "cases.md"
    if not req_file.is_file():
        print("  [需求->用例] 跳过：无 requirements.md")
        return None
    sys.path.insert(0, str(ROOT / "extensions" / "requirements_to_cases"))
    import generate_cases as gc  # noqa: E402
    import provenance as pv  # noqa: E402

    text = req_file.read_text(encoding="utf-8")
    # 用 generate_with_meta：产物里要盖上「怎么生成的」标记，
    # 尤其是 **LLM 失败降级** 必须在文件里看得见 —— 否则它长得跟"没配 key 走规则版"一模一样。
    md, meta = gc.generate_with_meta(text, use_llm=use_llm, agentic=agentic,
                                     source=str(req_file), extra_context=extra_context,
                                     injected=injected)
    out.write_text(md, encoding="utf-8")
    _, rows = _parse_cases(md)
    mode = "智能体多步编排" if (use_llm and agentic) else ("LLM 增强" if use_llm else "规则版")
    print(f"  [需求->用例] {mode}：{len(rows)} 条用例 -> {out}")
    if meta.get("degraded"):
        # 降级不是错误（流水线照常产出），但**必须出声**：
        # 静默降级 = 让人以为拿到了 LLM 质量的用例，实际拿到的是模板占位。
        print(f"  [需求->用例] ⚠️ 本次为降级产出（{meta.get('degrade_reason') or '原因未记录'}），"
              f"步骤/预期为模板占位，需人工细化")
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
        log.error("  [单场景重跑] 失败：%s", summary["error"])
        return summary
    # 报告同步刷新：保留既有的需求->用例预览（cases.md 存在才带上）
    cases = pdir / "artifacts" / "cases.md"
    _step_report(pid, pdir, cases if cases.is_file() else None, summary)
    return summary


RUN_META_FILE = "run_meta.json"
PERF_SEC_FILE = "perf_security.json"
WEB_FILE = "web.json"
AGENTIC_FILE = "agentic.json"
QUALITY_FILE = "quality.json"
QUALITY_HISTORY_FILE = "quality_history.jsonl"

# 新旧对比回看多少次历史执行（用于判断 flaky / persistent）
_DIFF_WINDOW = 6


def _step_perf_security(pdir: Path, users: Optional[int] = None,
                        iterations: Optional[int] = None,
                        only: Optional[str] = None) -> Dict[str, Any]:
    """性能 + 安全冒烟：结果落 artifacts/perf_security.json，供报告与 Web 复用。

    与核心回归同源的防假绿约定：环境不可达（基线登录失败）时全部 SKIP，
    all_pass=False，退出码非零，避免 CI 拿到"性能与安全通过"的虚假信号。
    """
    sys.path.insert(0, str(ROOT / "extensions" / "perf_security"))
    import run_perf_security as ps  # noqa: E402

    out_json = pdir / "artifacts" / PERF_SEC_FILE
    print("  [性能与安全] 执行冒烟（性能：并发/延迟/错误率；安全：鉴权/注入/泄露/配置）...")
    return ps.run_all(pdir / "project.yaml", pdir / "regression.yaml", out_json,
                      only=only, users=users, iterations=iterations)


def _read_perf_security(pdir: Path) -> Optional[Dict[str, Any]]:
    f = Path(pdir) / "artifacts" / PERF_SEC_FILE
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _step_web(pdir: Path, only: Optional[str] = None, headed: bool = False,
              browser: Optional[str] = None) -> Dict[str, Any]:
    """Web UI 冒烟：结果落 artifacts/web.json，截图落 artifacts/web_shots/。

    与核心回归 / 性能安全同源的三道防假绿闸门（见 extensions/web_testing/run_web.py）：
    无断言不算绿、环境不可达不判绿、配置问题单独报出。
    """
    import argparse as _ap

    sys.path.insert(0, str(ROOT / "extensions" / "web_testing"))
    import run_web as rw  # noqa: E402

    out_json = pdir / "artifacts" / WEB_FILE
    web_yaml = pdir / "web.yaml"
    if not web_yaml.is_file():
        raise SystemExit(
            f"未找到 {web_yaml}。请先在项目目录下创建 web.yaml 声明 Web 场景"
            "（可用 `python project_manager.py create` 生成模板，或参考 "
            "extensions/web_testing/run_web.py 的文件头示例）。"
        )
    print("  [Web 冒烟] 执行浏览器关键路径回归（Playwright + 声明式 YAML）...")
    ns = _ap.Namespace(browser=browser, headed=headed)
    return rw.run_web(pdir / "project.yaml", web_yaml, out_json, only=only, args=ns)


def _read_web(pdir: Path) -> Optional[Dict[str, Any]]:
    f = Path(pdir) / "artifacts" / WEB_FILE
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _agent_python() -> str:
    """跑「AI 探索」入口的解释器。

    `STA_AGENT_PYTHON` 优先：基座依赖（langgraph/langchain/langmem/playwright）可以装在
    一个**独立环境**里，主环境（与 CI 硬门禁）不必安装 —— 这正是决策 D2 的目的。
    """
    return os.environ.get("STA_AGENT_PYTHON") or python_exe()


def _agentic_contract():
    """取用探索契约模块（零依赖，唯一定义处：extensions/agentic/agentic_contract.py）。"""
    ext_dir = str(ROOT / "extensions" / "agentic")
    if ext_dir not in sys.path:
        sys.path.insert(0, ext_dir)
    import importlib
    return importlib.import_module("agentic_contract")


def _step_agentic(pdir: Path, mission: Optional[Path] = None, max_steps: int = 30,
                  timeout: Optional[float] = None, headed: bool = False) -> Dict[str, Any]:
    """AI 探索测试（S1）：**子进程 + 明确契约**调用独立入口（§3.3 / 决策 D2）。

    契约：`extensions/agentic/run_agentic.py` 输入 mission、输出 `artifacts/agentic.json`。
    本函数只做三件事——起进程、读契约、兜底。

    **降级语义**：探索是"可降级阶段"，**绝不中断流水线**；但也**绝不静默**。
    三层兜底（外层兜里层）：
      ① 入口内部：无 key / 超时 / 跑不出结果 → 入口自己写 degraded 契约；
      ② 这里：子进程超时 / 非零退出 / 契约缺失或坏 JSON → **本函数合成** degraded 契约；
      ③ 无论哪一层，降级都**落盘 + 出声**，并进 run_meta 与报告卡片
         （否则报告看不出少了这一环 = 悄悄放松门禁）。

    为什么跑之前先删旧契约：否则入口崩溃时会**读到上一轮的旧结论**，
    把"这次没跑"伪装成"这次跑成功了"——这正是本项目反复强调的"运行前清上次证据"。
    """
    ac = _agentic_contract()
    pdir = Path(pdir)
    out_json = pdir / "artifacts" / AGENTIC_FILE
    out_json.parent.mkdir(parents=True, exist_ok=True)
    mission_p = Path(mission) if mission else (pdir / "mission.yaml")

    # 阶段墙钟上限：默认有限（序 5 的结论——没有上限的探索等于把成本交给模型）。
    stage_timeout = ac.default_timeout() if timeout is None else float(timeout)

    print("  [AI 探索] 启动基座自主探索（独立进程；无 key / 超时 / 异常将降级并出声）...")
    try:
        out_json.unlink(missing_ok=True)
    except Exception as e:      # 删不掉也不能就此放弃（顶多是"可能读到旧值"的风险）
        log.warning("  [AI 探索] 清理旧契约失败：%s", e)

    entry = ROOT / "extensions" / "agentic" / "run_agentic.py"
    cmd = [_agent_python(), str(entry), "--project-dir", str(pdir),
           "--mission", str(mission_p), "--max-steps", str(int(max_steps))]
    if stage_timeout > 0:
        cmd += ["--timeout", str(stage_timeout)]
    if headed:
        cmd.append("--headed")

    # 进程级上限比入口内部上限**多留 60s 缓冲**：让入口自己的超时先触发，
    # 从而写出带日志与原因的降级契约；这里的上限只用于"入口本身挂死"。
    proc_timeout = (stage_timeout + 60.0) if stage_timeout > 0 else None
    try:
        r = subprocess.run(cmd, cwd=ROOT, timeout=proc_timeout, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        if r.stdout:
            print(r.stdout.rstrip())
        if r.returncode != 0:
            log.warning("  [AI 探索] 入口退出码 %s（非 0；仍以契约文件为准）", r.returncode)
    except subprocess.TimeoutExpired:
        log.warning("  [AI 探索] ⚠ 入口进程超时（%.0fs），已终止。", proc_timeout or 0)
    except Exception as e:      # 解释器不存在 / 权限 / OOM…
        log.warning("  [AI 探索] ⚠ 无法启动入口进程：%s", e)

    payload = _read_agentic(pdir)
    if not payload:
        # ② 兜底：连契约都没有 —— 合成一个降级结果，**绝不**默默返回空
        payload = ac.build_degraded(
            ac.DegradeReason.ERROR,
            f"未能从独立入口取得契约（{out_json} 不存在或不可解析）。"
            f"常见原因：解释器缺少基座依赖（可用 STA_AGENT_PYTHON 指向独立环境）、"
            f"或入口进程被信号终止。",
        )
        try:
            ac.write_result(out_json, payload)
        except Exception as e:
            log.warning("  [AI 探索] 兜底契约落盘失败：%s", e)

    if ac.is_ok(payload):
        print("  " + ac.render_summary_line(payload))
    else:
        # 降级必须出声（验收判据）：走诊断通道
        log.warning("  " + ac.render_degrade_warning(payload))
        print("  " + ac.render_summary_line(payload))
    return payload


def _read_agentic(pdir: Path) -> Optional[Dict[str, Any]]:
    f = Path(pdir) / "artifacts" / AGENTIC_FILE
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _step_quality(pdir: Path, cases_md: Optional[str],
                  requirement_count: Optional[int] = None,
                  mode: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """L3 评测常态化：每次 run 都算一次**用例结构质量分**（确定性、零依赖）。

    落盘两处：`artifacts/quality.json`（本次结果+历史）与
    `artifacts/quality_history.jsonl`（趋势数据源，只存结论数字）。

    为什么值得每次都算：LLM judge 需要 key、有成本、单次判分还有方差，注定只能抽样；
    而"这次的用例比上次明显差"这种信号，只有在每次都算的情况下才拿得到。
    ⚠️ 这是**结构分**（形式完整性），不是语义质量判定，默认不做硬门禁。
    """
    if not cases_md or not cases_md.strip():
        return None
    sys.path.insert(0, str(ROOT / "extensions" / "requirements_to_cases"))
    import case_quality as cq  # noqa: E402

    result = cq.score_cases_md(cases_md, requirement_count=requirement_count)
    cq.record_quality(pdir, result, mode=mode)   # 一步到位：追加历史 + 写 quality.json

    total = result.get("total")

    # 把结构质量分回写到 cases.md 的溯源标记里：用例文件常被单独发出去评审，
    # 分数只躺在 quality.json 里，拿到文件的人就看不到"这批用例形式完整度如何"。
    try:
        import provenance as pv  # noqa: E402
        _cf = Path(pdir) / "artifacts" / "cases.md"
        if _cf.is_file():
            _cf.write_text(pv.attach_quality(_cf.read_text(encoding="utf-8"), total),
                           encoding="utf-8")
    except Exception as e:      # 回写失败不能让打分失败
        # 必须出声：分数没写进 cases.md，单独把用例文件发出去评审的人会以为
        # "这批用例没有质量分"，与"分很低"是两回事。
        log.warning("  [质量分] 回写溯源标记失败（不影响打分）：%s", e)
    d = cq.delta(cq.read_history(pdir))
    if total is None:
        print("  [质量分] 无法计分（用例为空或缺少可判定维度），已跳过")
        return result
    # 注意区分 `d is None`（数据不足，首次）与 `d == 0`（持平）——
    # 用 `if not d` 会把"持平"显示成"—"，让人以为还没跑过。
    arrow = "首次" if d is None else ("持平" if d == 0 else (f"↑{d}" if d > 0 else f"↓{abs(d)}"))
    print(f"  [质量分] 结构质量 {total}/100（环比 {arrow}） -> artifacts/{QUALITY_FILE}")
    return result


def _step_focus(pdir: Path, cases_path: Path,
                items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """情景记忆回灌闭环：校验本轮用例覆盖了多少"重点覆盖清单"，缺口补骨架并如实标注。

    与结构质量分**两码事，不能互相替代**：
      质量分看形式完整度（可注水：多写点套话就能刷高）
      这里看历史易错点有没有被覆盖到（不可注水：覆盖就是覆盖，没覆盖就写出来）

    产物：
      - cases.md：缺失项补为【回灌】骨架用例 + 末尾「本轮未覆盖的历史易错点」章节
      - artifacts/focus_history.jsonl：覆盖率趋势（验收口径：重复 run 不应下降）
    """
    if not items:
        return None
    sys.path.insert(0, str(ROOT / "extensions" / "memory"))
    import focus as fc  # noqa: E402
    cf = Path(cases_path)
    md = cf.read_text(encoding="utf-8")
    # apply_persistent：并入上轮补齐行 → 校验 → 补新缺口 → 存回去。
    # 用带持久化的版本，覆盖才能跨轮累积（cases.md 每轮重生成，不存就白补）。
    res = fc.apply_persistent(pdir, md, items)
    cf.write_text(res.get("md") or md, encoding="utf-8")
    fc.record_focus(pdir, res)
    _d = fc.delta(fc.read_history(pdir))
    _tail = "（首次）" if _d is None else ("（持平）" if _d == 0 else
                                       (f"（环比 ↑{_d}）" if _d > 0 else f"（环比 ↓{abs(_d)}）"))
    print(f"  [重点覆盖] {fc.to_summary(res)}{_tail} -> artifacts/{fc.FOCUS_FILE}")
    return res


def _read_quality(pdir: Path) -> Optional[Dict[str, Any]]:
    f = Path(pdir) / "artifacts" / QUALITY_FILE
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _read_regression(pdir: Path) -> Dict[str, Any]:
    f = Path(pdir) / "artifacts" / "regression.json"

    if f.is_file():
        try:
            return json.loads(f.read_text(encoding="utf-8")) or {}
        except Exception as e:
            # 必须出声：文件在、但读不出来（半写 / 截断 / 编码坏）会被上层当成
            # "没有回归结果"，报告与看板显示成"尚未执行"，把真实故障掩盖掉。
            log.warning("  [回归结果] 读取 %s 失败，按无结果处理：%s", f, e)
    return {}


def _write_run_meta(pdir: Path, **fields: Any) -> Dict[str, Any]:
    """记录本次 run 的「怎么生成的」——供报告与 Web 展示，便于追溯。

    fields 典型含：mode（生成模式）/ use_llm / agentic / lessons_injected /
    requirements（需求条数）/ cases（用例条数）。写入 artifacts/run_meta.json。
    """
    meta: Dict[str, Any] = {"ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
    meta.update(fields)
    art = Path(pdir) / "artifacts"
    art.mkdir(parents=True, exist_ok=True)
    (art / RUN_META_FILE).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def _merge_run_meta(pdir: Path, **fields: Any) -> Dict[str, Any]:
    """在既有 run_meta 上**增量**更新，保留其它阶段写入的字段（perf_security / web / quality）。

    为什么需要它：`_write_run_meta` 是整体覆盖。若「只生成用例、没跑回归」的场景
    直接整体覆盖，会把上次全流程的性能安全 / Web 结论一起抹掉 ——
    报告会变成"这些门禁从没跑过"，属于自造的信息丢失。
    """
    cur = _read_run_meta(pdir) or {}
    cur.pop("ts", None)
    cur.update(fields)
    return _write_run_meta(pdir, **cur)


def _read_run_meta(pdir: Path) -> Optional[Dict[str, Any]]:
    f = Path(pdir) / "artifacts" / RUN_META_FILE
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


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
                pass  # 可忽略：仅供展示的元信息，解析不出就保留默认 0，不参与任何判定

    # 表格行解析收敛到 extensions/common/cases.parse_rows（唯一定义处）。
    return meta, _common("cases").parse_rows(md_text)


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


def _ps_result_badge(result: Any) -> str:
    v = str(result)
    if v == "PASS":
        return "<span class='tag ok'>通过</span>"
    if v == "SKIP":
        return "<span class='tag skip'>跳过</span>"
    if v == "WARN":
        return "<span class='tag warn'>提示</span>"
    return "<span class='tag bad'>失败</span>"


def _perf_security_card_html(pf: Dict[str, Any]) -> str:
    """渲染「性能与安全冒烟」卡片（性能指标表 + 安全检查表）。"""
    ok_all = bool(pf.get("all_pass"))
    gate = ("<span class='gate ok'>通过</span>" if ok_all
            else "<span class='gate bad'>未通过</span>")
    bloom = _h(pf.get("summary") or "")

    perf = pf.get("perf") or {}
    sec = pf.get("security") or {}

    # ---- 性能表 ----
    targets = perf.get("targets") or []
    if not perf.get("enabled"):
        perf_html = "<div class='reg-empty'>本次未执行性能冒烟。</div>"
    elif perf.get("skipped"):
        perf_html = (f"<div class='ps-note warn'>未执行：{_h(perf.get('reason', ''))}</div>")
    elif not targets:
        perf_html = "<div class='reg-empty'>没有可压测的目标。</div>"
    else:
        ov = perf.get("overall") or {}
        rows = "".join(
            f"<tr class='{('ok' if t.get('result') == 'PASS' else ('skip' if t.get('result') == 'SKIP' else 'bad'))}'>"
            f"<td class='name'>{_h(t.get('name'))}</td>"
            f"<td class='method'>{_h(t.get('method'))} {_h(t.get('path'))}</td>"
            f"<td class='actual'>{_h(t.get('requests'))}</td>"
            f"<td class='actual'>{_h(t.get('ok'))}</td>"
            f"<td class='actual'>{float(t.get('error_rate') or 0) * 100:.2f}%</td>"
            f"<td class='actual'>{_h(t.get('p50_ms'))}</td>"
            f"<td class='actual'>{_h(t.get('p95_ms'))}</td>"
            f"<td class='actual'>{_h(t.get('p99_ms'))}</td>"
            f"<td class='actual'>{_h(t.get('rps'))}</td>"
            f"<td class='result'>{_ps_result_badge(t.get('result'))}</td>"
            f"<td class='detail'>{_h('；'.join(t.get('threshold_fails') or []))}</td></tr>"
            for t in targets
        )
        cfgp = perf.get("config") or {}
        thr = cfgp.get("thresholds") or {}
        thr_txt = "、".join(
            f"{k}={v}" for k, v in
            [("P95(ms)", thr.get("p95_ms")), ("P99(ms)", thr.get("p99_ms")),
             ("最大错误率", thr.get("max_error_rate")), ("最小吞吐(rps)", thr.get("min_rps"))]
            if v
        ) or "未设置阈值"
        perf_html = (
            f"<div class='ps-sub'>并发 {_h(cfgp.get('users'))} · 每用户 {_h(cfgp.get('iterations'))} 次"
            f" · 阈值：{_h(thr_txt)} · 目标来源：{_h(perf.get('target_source'))}</div>"
            "<div class='table-wrap'><table class='reg-table'>"
            "<thead><tr><th>目标</th><th>接口</th><th>请求</th><th>成功</th><th>错误率</th>"
            "<th>P50</th><th>P95</th><th>P99</th><th>RPS</th><th>结果</th><th>未达阈值</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>"
            f"<div class='ps-sub'>整体：样本 {_h(ov.get('samples'))} · 错误率 "
            f"{float(ov.get('error_rate') or 0) * 100:.2f}% · P50 {_h(ov.get('p50_ms'))}ms"
            f" · P95 {_h(ov.get('p95_ms'))}ms · P99 {_h(ov.get('p99_ms'))}ms"
            f" · 吞吐 {_h(ov.get('rps'))} rps</div>"
        )

    # ---- 安全表 ----
    checks = sec.get("checks") or []
    if not sec.get("enabled"):
        sec_html = "<div class='reg-empty'>本次未执行安全冒烟。</div>"
    elif not checks:
        sec_html = f"<div class='ps-note warn'>未执行：{_h(sec.get('reason', ''))}</div>"
    else:
        rows = "".join(
            f"<tr class='{('ok' if c.get('status') == 'PASS' else ('skip' if c.get('status') == 'SKIP' else 'bad'))}'>"
            f"<td class='name'>{_h(c.get('name'))}</td>"
            f"<td class='method'>{_h(c.get('category'))}</td>"
            f"<td class='result'>{_ps_result_badge(c.get('status'))}</td>"
            f"<td class='expect' title='{_h(c.get('evidence'))}'>{_h(c.get('detail'))}</td></tr>"
            for c in checks
        )
        sec_html = (
            f"<div class='ps-sub'>通过 {_h(sec.get('passed_count'))} · 失败 {_h(sec.get('failed'))}"
            f" · 提示 {_h(sec.get('warned'))} · 跳过 {_h(sec.get('skipped'))}</div>"
            "<div class='table-wrap'><table class='reg-table'>"
            "<thead><tr><th>检查项</th><th>类别</th><th>状态</th><th>说明</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>"
        )

    baseline = pf.get("baseline") or {}
    base_html = ""
    if baseline and not baseline.get("ok"):
        base_html = (f"<div class='ps-note bad'>基线未通过：{_h(baseline.get('reason', ''))}"
                     "（此状态下结果不计为安全缺陷，但门禁按未通过处理）</div>")
    _stale = pf.get("stale_sections") or []
    if _stale:
        _names = "、".join({"perf": "性能", "security": "安全"}.get(k, k) for k in _stale)
        _when = pf.get("perf_from_previous") or pf.get("security_from_previous") or ""
        base_html += (f"<div class='ps-note warn'>说明：{_h(_names)}为上次结果（{_h(_when)}），"
                      "本次只重跑了另一侧；门禁按两侧合并判定。</div>")

    return (
        "<div class='card'>"
        f"<div class='card-title'>性能与安全冒烟 {gate}"
        f"<span class='count'>{_h(bloom)}</span></div>"
        f"{base_html}"
        f"<div class='ps-sub'>时间：{_h(pf.get('generated_at'))} · 环境：<code>{_h(pf.get('base_url'))}</code></div>"
        f"<div class='ps-h'>性能指标</div>{perf_html}"
        f"<div class='ps-h'>安全检查</div>{sec_html}"
        "</div>"
    )


def _base_name(p: Any) -> str:
    """取路径末段文件名，对 Windows 反斜杠与 POSIX 正斜杠两种分隔符都成立。

    为什么不能直接用 `Path(p).name`：执行器（extensions/web_testing/run_web.py）在 Windows 上
    产出的是反斜杠分隔的路径（artifacts + 反斜杠 + web_shots + 反斜杠 + 文件名），
    而 POSIX 下反斜杠只是普通字符 —— 此时 `Path(...).name` 会把整串路径当成一个文件名，
    证据链接随之被渲染成「web_shots/artifacts...」，在 Linux/CI 上必然 404。
    报告产物可能在另一个平台生成、再被本机读取（本项目支持跨机看报告），
    所以这里显式统一分隔符，而不依赖运行平台的路径语义。
    """
    return str(p).replace("\\", "/").rstrip("/").split("/")[-1]


def _web_card_html(wf: Dict[str, Any]) -> str:
    """渲染「Web UI 冒烟」卡片（场景表 + 配置/门禁问题 + 失败证据）。"""
    ok_all = bool(wf.get("all_pass"))
    gate = ("<span class='gate ok'>通过</span>" if ok_all
            else "<span class='gate bad'>未通过</span>")
    bloom = _h(wf.get("summary") or "")

    scenarios = wf.get("scenarios") or []
    issues = wf.get("config_issues") or []

    if not scenarios:
        scen_html = "<div class='reg-empty'>没有已声明的 Web 场景。</div>"
    else:
        rows = []
        for s in scenarios:
            cls = {"PASS": "ok", "SKIP": "skip"}.get(str(s.get("result")), "bad")
            fs = s.get("failed_step") or {}
            fs_txt = ""
            if fs:
                fs_txt = f"第{fs.get('index')}步 {fs.get('action')} → {fs.get('target')}"
            extra = []
            if s.get("flaky"):
                extra.append("抖动（重试后通过）")
            shots = s.get("screenshots") or []
            if shots:
                # 证据文件都在 artifacts/ 下（web_shots/ 与 web_repro_*.spec.ts），
                # 而 report.html 本身也在 artifacts/ —— 所以只取文件名拼相对路径，
                # 不能用执行器返回的项目级相对路径（那会拼成 artifacts/artifacts/...）。
                links = "、".join(
                    f"<a href='web_shots/{_h(_base_name(p))}'>截图{i + 1}</a>"
                    for i, p in enumerate(shots)
                )
                extra.append(links)
            if s.get("repro"):
                extra.append(f"<a href='{_h(_base_name(s['repro']))}'>可复现脚本</a>")
            rows.append(
                f"<tr class='{cls}'>"
                f"<td class='name'>{_h(s.get('name'))}</td>"
                f"<td class='method'>{_h('、'.join(s.get('tags') or []) or '—')}</td>"
                f"<td class='actual'>{_h(s.get('assertions'))}</td>"
                f"<td class='actual'>{_h(s.get('duration_ms'))}ms</td>"
                f"<td class='result'>{_ps_result_badge(s.get('result'))}</td>"
                f"<td class='detail'>{_h(s.get('reason') or '')}"
                + (f"<br>{_h(fs_txt)}" if fs_txt else "")
                + (f"<br>{' · '.join(extra)}" if extra else "")
                + "</td></tr>"
            )
        base = wf.get("baseline") or {}
        base_note = ""
        if base and not base.get("ok"):
            base_note = (f"<div class='ps-note bad'>基线未通过：{_h(base.get('reason', ''))}"
                         "（此状态下场景未实际执行，结果不计为产品缺陷，但门禁按未通过处理）</div>")
        scen_html = (
            f"<div class='ps-sub'>通过 {_h(wf.get('passed'))} · 失败 {_h(wf.get('failed'))}"
            f" · 跳过 {_h(wf.get('skipped'))} · 断言 {_h(wf.get('assertions'))}"
            + (f" · 抖动 {_h(wf.get('flaky'))}" if wf.get("flaky") else "")
            + "</div>"
            + base_note
            + "<div class='table-wrap'><table class='reg-table'>"
            "<thead><tr><th>场景</th><th>标签</th><th>断言</th><th>耗时</th>"
            "<th>结果</th><th>说明 / 证据</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )

    issues_html = ""
    if issues:
        items = "".join(f"<li>{_h(i)}</li>" for i in issues)
        issues_html = (f"<div class='ps-note bad'>配置/门禁问题 {len(issues)} 项"
                       "（这些问题会让门禁失真，必须先修）："
                       f"<ul class='ps-list'>{items}</ul></div>")

    return (
        "<div class='card'>"
        f"<div class='card-title'>Web UI 冒烟 {gate}"
        f"<span class='count'>{_h(bloom)}</span></div>"
        f"<div class='ps-sub'>时间：{_h(wf.get('generated_at'))} · 地址："
        f"<code>{_h(wf.get('base_url') or '(未配置)')}</code> · 浏览器：{_h(wf.get('browser'))}"
        f" · {'无头' if wf.get('headless') else '有头'}</div>"
        f"{issues_html}{scen_html}"
        "</div>"
    )


def _agentic_href(pdir: Path, value: Any) -> Optional[str]:
    """把契约里的路径（相对**仓库根**）转成相对**报告所在目录**（artifacts/）的链接。

    为什么要转：report.html 本身在 `artifacts/` 下，而契约里存的是仓库根相对路径。
    直接拼会得到 `artifacts/projects/<id>/artifacts/...` 这种必然 404 的链接
    （本项目在报告证据链上踩过同类坑）。转不出来的（指向 artifacts 之外）不链接。
    """
    if not value:
        return None
    p = Path(str(value))
    if not p.is_absolute():
        p = ROOT / p
    try:
        return str(p.resolve().relative_to((Path(pdir) / "artifacts").resolve()))
    except Exception:
        return None


def _agentic_card_html(pdir: Path, ag: Dict[str, Any]) -> str:
    """渲染「AI 探索测试」卡片（可降级阶段：状态 / 降级原因 / 发现 / 证据链接）。"""
    ac = _agentic_contract()
    ok = ac.is_ok(ag)
    reason = str(ag.get("reason") or "")
    gate = ("<span class='gate ok'>已执行</span>" if ok
            else "<span class='gate' style='background:var(--warn-soft);color:var(--warn)'>已降级</span>")

    sub = (f"LLM：<code>{_h(ag.get('provider') or '—')}</code>"
           f" · 耗时：{_h(ag.get('elapsed_s'))}s")
    if ok:
        sub += (f" · 任务：<code>{_h(ag.get('thread_id') or '—')}</code>"
                f" · 结束原因：<code>{_h(ag.get('end_reason') or '未记录')}</code>"
                f" · 目标判定：<code>{_h(ag.get('goal_verdict') or 'unknown')}</code>"
                f" · 动作 {_h(ag.get('actions'))} 条 / 发现 {_h(ag.get('bug_count'))} 个")

    # 降级说明：必须显眼，且明确"未执行 ≠ 通过"
    degrade_html = ""
    if not ok:
        label = ac.DEGRADE_LABEL.get(reason, reason or "未知原因")
        degrade_html = (
            "<div class='ps-note bad'>⚠ 本阶段**未执行**（降级）："
            f"{_h(label)}"
            + (f"<br>{_h(ag.get('message') or '')}" if ag.get("message") else "")
            + "<br>本轮结论以<b>确定性链路</b>（回归 / 性能安全 / Web）为准 —— "
              "「未执行」不等于「通过」，请勿据此判定探索已覆盖。</div>"
        )

    bugs = ag.get("bugs") or []
    bugs_html = ""
    if ok and bugs:
        items = "".join(f"<li>{_h(b)}</li>" for b in bugs)
        bugs_html = (f"<div class='ps-h'>探索发现 <span class='count'>{len(bugs)}</span></div>"
                     f"<div class='ps-note warn'>以下为基座探索的**原始记录**，"
                     f"未经结构化与定级（不自动作为缺陷提交）："
                     f"<ul class='ps-list'>{items}</ul></div>")

    links = []
    for key, label in (("report_dir", "探索报告目录"), ("action_tape", "动作磁带"),
                       ("log", "完整日志")):
        href = _agentic_href(pdir, ag.get(key))
        if href:
            links.append(f"<a href='{_h(href)}'>{label}</a>")
    links_html = (f"<div class='ps-sub'>证据：{' · '.join(links)}</div>") if links else ""

    return (
        "<div class='card'>"
        f"<div class='card-title'>AI 探索测试（可降级阶段）{gate}</div>"
        f"<div class='ps-sub'>{sub}</div>"
        f"{degrade_html}{bugs_html}{links_html}"
        "</div>"
    )


def _step_diff(pid: str, pdir: Path, reg: Dict[str, Any],
               run_store: Any = None) -> Optional[Dict[str, Any]]:
    """失败项新旧对比：把「这次新红的」从「一直红的」里挑出来。

    ⚠️ **必须在把本次结果写入快照（insert_snapshot）之前调用** ——
    否则历史快照里最新一条就是本次自己，自己跟自己比，结论永远是"没有新增失败"。
    这个顺序由 `tests/test_trend_diff.py::test_step_diff_excludes_current_from_history` 守护。

    run_store 为 None 时（例如轻量入口没有 DB）只按"无历史"处理，仍会落盘一份
    写明"无基线"的结论 —— 保持 run → 报告 → 控制台的口径一致。
    """
    sys.path.insert(0, str(ROOT / "extensions" / "reporting"))
    import trend_diff as td  # noqa: E402

    history: List[Dict[str, Any]] = []
    if run_store is not None:
        try:
            history = run_store.list_snapshots(pid, limit=_DIFF_WINDOW)
        except Exception as e:
            # 必须出声：历史读不到时对比会退化成"无基线"，结论看似正常实则失真 ——
            # 这正是最该被看见的一类静默降级。
            log.warning("  [新旧对比] 读取历史失败（回退为无基线对比）：%s", e)
    prev = history[-1] if history else None
    payload = td.compare(reg.get("results") or [], prev=prev, history=history)
    td.write_diff(pdir, payload)

    print("  [新旧对比] " + (payload.get("headline") or ""))
    if not (payload.get("baseline") or {}).get("available"):
        print("           └ " + str((payload["baseline"] or {}).get("reason") or ""))
    return payload


def _step_defects(pid: str, pdir: Path, reg: Dict[str, Any],
                  base_url: str = "", diff: Optional[Dict[str, Any]] = None
                  ) -> Optional[Dict[str, Any]]:
    """把失败的门禁项整理成**缺陷草稿**（`artifacts/defects.md` / `.json`）。

    性能安全与 Web 结论从既有产物读（这两道是可选步骤，可能没跑），
    因此"只跑回归"也能拿到缺陷草稿。
    """
    sys.path.insert(0, str(ROOT / "extensions" / "reporting"))
    import defects as df  # noqa: E402

    # 新旧对比结论：调用方没传就从产物读（正常流程里 _step_diff 先跑过并已落盘）。
    # 读不到就按"无对照"处理 —— 没有标签可以接受，编错的标签不行。
    if diff is None:
        try:
            import trend_diff as _td  # noqa: E402
            diff = _td.read_diff(pdir)
        except Exception:
            # 可忽略：缺陷草稿按"无对照"处理 —— 没有对照可以接受，编错的对照不行。
            diff = None

    payload = df.build_defects(reg, _read_perf_security(pdir), _read_web(pdir),
                               pid=pid, base_url=base_url, diff=diff)
    df.write_defects(pdir, payload)
    n = payload["counts"]["total"]
    env_n = payload["counts"]["env"]
    if n:
        print(f"  [缺陷草稿] {n} 条待确认（S1 "
              f"{payload['counts']['by_severity']['S1']} / S2 "
              f"{payload['counts']['by_severity']['S2']} / S3 "
              f"{payload['counts']['by_severity']['S3']}） -> artifacts/{df.DEFECTS_MD}")
    else:
        print(f"  [缺陷草稿] 本次无产品缺陷（{df.DEFECTS_MD} 已更新）")
    if env_n:
        # 环境问题单列，不混进缺陷清单 —— 把环境没起报成缺陷最伤信任
        print(f"  [缺陷草稿] 另有 {env_n} 项环境问题（已单列，不按缺陷处理）")
    return payload


def _read_defects(pdir: Path) -> Optional[Dict[str, Any]]:
    try:
        sys.path.insert(0, str(ROOT / "extensions" / "reporting"))
        import defects as df  # noqa: E402
    except Exception:
        return None
    return df.read_defects(pdir)


def _read_diff(pdir: Path) -> Optional[Dict[str, Any]]:
    """失败项新旧对比结论（`artifacts/diff.json`）。没跑过就没有，不编。"""
    try:
        sys.path.insert(0, str(ROOT / "extensions" / "reporting"))
        import trend_diff as td  # noqa: E402
    except Exception:
        return None
    return td.read_diff(pdir)


_DIFF_ICON = {"regressed": "🔺", "new": "🆕", "persistent": "🔁",
              "flaky": "🎲", "recovered": "✅", "unknown": "❔"}
_DIFF_COLOR = {"regressed": "var(--bad)", "new": "var(--warn)",
               "persistent": "var(--muted)", "flaky": "var(--info)",
               "recovered": "var(--ok)", "unknown": "var(--muted-2)"}


def _diff_card_html(dp: Dict[str, Any]) -> str:
    """报告里的「本次新增失败」卡片。

    存在的意义不是再列一遍失败，而是**回答先看哪一个**：
    红同样的 5 个场景，看久了人就麻了；真正的信号是"相比上次新红的那个"。
    """
    items = dp.get("items") or []
    base = dp.get("baseline") or {}
    counts = dp.get("counts") or {}
    labels = dp.get("labels") or {}

    head = (f"<div class='card'><div class='card-title'>失败项新旧对比 "
            f"<span class='count'>{len(items)}</span></div>")
    if not base.get("available"):
        head += (f"<div class='empty-case'>暂无可对照的基线 —— {_h(str(base.get('reason') or ''))}<br>"
                 "因此<b>未作新旧判断</b>：没有有效基线还硬贴「新增失败」标签，"
                 "等于用噪音刷注意力。</div></div>")
        return head

    focus = int(counts.get("regressed", 0)) + int(counts.get("new", 0))
    line = _h(dp.get("headline") or "")
    chips = "".join(
        f"<span class='badge' style='color:{_DIFF_COLOR.get(k, 'var(--muted)')}'>"
        f"{_DIFF_ICON.get(k, '')} {labels.get(k, k)} {counts.get(k, 0)}</span>"
        for k in ("regressed", "new", "persistent", "flaky", "recovered")
        if counts.get(k))
    head += (f"<div style='font-size:13px;color:var(--muted);margin-bottom:8px'>"
             f"对照基线：{_h(str(base.get('ts_text') or '—'))} · {line}</div>"
             f"<div style='display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px'>{chips}</div>")

    if not items:
        head += "<div class='empty-case'>本次没有失败项，也没有由失败转通过的场景。</div></div>"
        return head

    rows: List[str] = []
    for it in items:
        st = str(it.get("status") or "")
        extra: List[str] = []
        if int(it.get("streak") or 0) > 1:
            extra.append(f"连续失败 {it['streak']} 次")
        if it.get("window_runs"):
            extra.append(f"近 {it['window_runs']} 次中失败 {it['window_fails']}")
        if str(it.get("last_pass_text") or "—") != "—":
            extra.append(f"上次通过 {it['last_pass_text']}")
        tail = f"<span class='web-ev'>{'；'.join(extra)}</span>" if extra else ""
        rows.append(
            f"<tr><td style='white-space:nowrap;color:{_DIFF_COLOR.get(st, 'var(--muted)')}'>"
            f"{_DIFF_ICON.get(st, '')} {labels.get(st, st)}</td>"
            f"<td><b>{_h(str(it.get('name') or ''))}</b>{tail}</td>"
            f"<td style='color:var(--muted)'>{_h(str(it.get('detail') or ''))}</td></tr>")

    notes = "".join(f"<div class='web-ev'>· {_h(n)}</div>" for n in (dp.get("notes") or []))
    if focus:
        notes += ("<div class='web-ev' style='margin-top:6px'>· 建议优先处理标记为"
                  "「回归 / 新增」的项；其余属于已知问题，按排期处理即可。</div>")
    return (f"{head}<table class='tbl'><tr><th>判定</th><th>场景</th><th>说明</th></tr>"
            f"{''.join(rows)}</table>{notes}"
            "<div class='web-ev' style='margin-top:8px'>"
            "本卡片只做「先看哪个」的排序提示，<b>不参与门禁判定</b>。</div></div>")


def _defects_card_html(dp: Dict[str, Any]) -> str:
    """报告里的「待提交缺陷」卡片：清单 + 级别建议 + 环境问题单列。"""
    items = dp.get("items") or []
    counts = dp.get("counts") or {}
    by_sev = counts.get("by_severity") or {}

    def _sev_tag(s: str) -> str:
        cls = {"S1": "bad", "S2": "bad", "S3": "warn"}.get(s, "skip")
        return f"<span class='tag {cls}'>{s}</span>"

    if not items:
        body = ("<div class='reg-empty'>本次执行没有发现需要提交的产品缺陷。"
                + (f"<br>另有 {counts.get('env', 0)} 项环境问题（见下方，不按缺陷处理）。"
                   if counts.get("env") else "")
                + "</div>")
    else:
        rows = "".join(
            f"<tr><td class='name'>{_h(d.get('id'))}</td>"
            f"<td>{_sev_tag(str(d.get('severity', 'S4')))}</td>"
            f"<td>{_h(d.get('source'))}</td>"
            f"<td class='name'>{_h(d.get('title'))}</td>"
            f"<td class='actual'>{_h(d.get('actual'))}</td>"
            f"<td class='detail'>{_h(d.get('evidence') or d.get('severity_reason', ''))}</td></tr>"
            for d in items)
        body = ("<div class='table-wrap'><table class='reg-table'>"
                "<thead><tr><th>编号</th><th>建议级别</th><th>来源</th><th>标题</th>"
                "<th>实际</th><th>说明</th></tr></thead>"
                f"<tbody>{rows}</tbody></table></div>")

    env_html = ""
    if dp.get("env_issues"):
        env_html = ("<div class='ps-note warn'><b>环境问题（不是缺陷，别提单）：</b><ul>"
                    + "".join(f"<li>{_h(x)}</li>" for x in dp["env_issues"]) + "</ul></div>")
    cfg_html = ""
    if dp.get("config_issues"):
        cfg_html = ("<div class='ps-note warn'><b>配置问题（不是缺陷，改配置即可）：</b><ul>"
                    + "".join(f"<li>{_h(x)}</li>" for x in dp["config_issues"]) + "</ul></div>")

    return f"""<div class='card'>
  <div class='card-title'>待提交缺陷（草稿） <span class='count'>{_h(dp.get('generated_at', ''))}</span></div>
  {body}
  <div class='ps-sub'>严重程度是<b>规则推断的建议值</b>，提交前请按业务影响人工复核；
    本卡片<b>不会自动提单</b>。完整草稿见 <code>artifacts/defects.md</code>（可直接粘进缺陷系统）。</div>
  {env_html}
  {cfg_html}
</div>"""


def _timing_card_html(payload: Dict[str, Any]) -> str:
    """渲染「效率口径」卡片：各阶段耗时 + 已排除项 + **未测量声明**。

    两条必须显眼：
    ① 未执行的阶段写「未执行」，**不写 0 秒** —— 否则"没跑"看起来像"跑得飞快"；
    ② 「人力节省 / 质量」单列一栏写明**本口径不测** —— 留白会被读成"这个数应该能算"。
    """
    rows: List[str] = []
    for st in payload.get("stages") or []:
        if not isinstance(st, dict):
            continue
        name = _h(st.get("name", "?"))
        if st.get("executed") is False:
            rows.append(
                f"<tr><td class='name'>{name}</td>"
                "<td class='actual' style='color:var(--muted-2)'>未执行</td>"
                "<td class='expect'>本次未开启 / 未发生 —— 不是 0 秒</td></tr>")
            continue
        ok = bool(st.get("ok", True))
        note = _h(str(st.get("error"))) if st.get("error") else \
            ("阶段内异常（耗时仍计入）" if not ok else "")
        rows.append(
            f"<tr><td class='name'>{name}</td>"
            f"<td class='actual' style='color:{'var(--ink)' if ok else 'var(--bad)'}'>"
            f"{_h(timing_mod.format_seconds(st.get('seconds')))}</td>"
            f"<td class='expect'>{note}</td></tr>")

    not_measured = "".join(f"<li>{_h(x)}</li>" for x in (payload.get("not_measured") or []))
    excluded = "".join(f"<li>{_h(x)}</li>" for x in (payload.get("excluded") or []))
    skipped = payload.get("skipped") or []
    failed = payload.get("failed") or []
    extra = (f" · 未执行：{'、'.join(_h(x) for x in skipped)}" if skipped else "") \
        + (f" · 异常阶段：{'、'.join(_h(x) for x in failed)}" if failed else "")

    return (
        "<div class='card'>"
        "<div class='card-title'>效率口径 "
        f"<span class='count'>{_h(payload.get('scope', ''))}</span></div>"
        f"<div class='ps-sub'>合计 <b>{_h(timing_mod.format_seconds(payload.get('total_seconds')))}</b>"
        f" · 已执行 {_h(payload.get('executed_count'))}/{_h(payload.get('stage_count'))} 阶段"
        f" · 计量：{_h(payload.get('measured', ''))}{extra}</div>"
        "<div class='table-wrap'><table class='reg-table'>"
        "<thead><tr><th>阶段</th><th>耗时</th><th>说明</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
        "<div class='ps-note warn'>本口径<b>只测机器侧时间</b>，以下项目"
        "<b>明确不测</b>（不给近似值，也不用 0 冒充）："
        f"<ul class='ps-list'>{not_measured}</ul>"
        + (f"已排除：<ul class='ps-list'>{excluded}</ul>" if excluded else "")
        + "</div></div>"
    )


def _quality_card_html(q: Dict[str, Any]) -> str:
    """用例结构质量分卡片：总分 + 维度条 + 环比 + 趋势 sparkline + 未计分说明。

    卡片里必须显式写清"结构分 ≠ 用例质量"：
    一个满分的结构分只说明形式完整，读的人很容易把它当质量结论用。
    """
    total = q.get("total")
    dims = q.get("dims") or {}
    delta = q.get("delta")
    hist = q.get("history") or []
    counts = q.get("counts") or {}

    def _color(v: int) -> str:
        return "var(--ok)" if v >= 80 else ("var(--warn)" if v >= 60 else "var(--bad)")

    def _bar(v: int) -> str:
        return (f"<div class='q-bar'><div class='q-bar-fill' style='width:"
                f"{max(0, min(100, int(v)))}%;background:{_color(v)}'></div></div>")

    if delta is None:
        d_html, d_color = "首次", "var(--muted)"
    elif delta == 0:
        d_html, d_color = "持平", "var(--muted)"
    elif delta > 0:
        d_html, d_color = f"↑{delta}", "var(--ok)"
    else:
        d_html, d_color = f"↓{abs(delta)}", "var(--bad)"

    total_html = (f"<span style='color:{_color(int(total))}'>{total}</span>"
                  if isinstance(total, int) else "<span style='color:var(--muted)'>—</span>")

    sys.path.insert(0, str(ROOT / "extensions" / "requirements_to_cases"))
    import case_quality as cq  # noqa: E402
    dim_rows = "".join(
        f"<div class='q-row'><span class='q-k'>{_h(cq.LABELS[d])}</span>"
        f"{_bar(int(v))}<span class='q-v' style='color:{_color(int(v))}'>{v}</span>"
        f"<span class='q-hint'>{_h(cq.HINTS[d])}</span></div>"
        for d, v in ((d, dims.get(d)) for d in cq.DIMENSIONS) if v is not None
    )
    spark = cq.sparkline(cq.trend_series(hist))
    spark_html = (f"<div class='q-trend'><span class='q-k'>趋势</span>{spark}</div>"
                  if spark else "")

    notes = "".join(f"<li>{_h(n)}</li>" for n in (q.get("notes") or []))
    notes_html = f"<ul class='q-notes'>{notes}</ul>" if notes else ""

    meta = " · ".join(filter(None, [
        f"用例 {counts.get('cases', 0)} 条",
        (f"需求 {counts.get('requirements')} 条（覆盖 {counts.get('covered', 0)}）"
         if counts.get("requirements") else ""),
        (f"重复 {counts.get('dup')} 条" if counts.get("dup") else ""),
        f"历史 {len(hist)} 次",
    ]))

    return f"""<div class='card'>
  <div class='card-title'>用例结构质量分 <span class='count'>{_h(q.get('scored_at', ''))}</span></div>
  <div class='q-head'>
    <div class='q-total'>{total_html}<span class='q-total-max'>/100</span></div>
    <div class='q-delta' style='color:{d_color}'>环比 {d_html}</div>
    <div class='q-meta'>{_h(meta)}</div>
  </div>
  <div class='q-dims'>{dim_rows}</div>
  {spark_html}
  <div class='q-disclaimer'>结构分只反映<strong>形式完整性</strong>（覆盖/三类/可执行/具体/去重），
    <strong>不代表用例质量好坏</strong>——语义正确性请用 LLM judge 抽样评测。
    满分只说明没查出形式缺陷；趋势下跌才说明生成环节可能退化。</div>
  {notes_html}
</div>"""


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
        table_html = ("<div class='reg-empty'>尚未执行核心回归。"
                      "运行 <code>run</code> 或 <code>regression</code> 后此处会展示结果。</div>")

    skip_txt = f"，跳过 {reg.get('skipped', 0)}" if reg.get("skipped") else ""
    # 三种状态：未执行 / 通过 / 未通过。必须区分"未执行"——否则只跑了性能安全时，
    # 报告会把空的核心回归渲染成"存在失败"，给出错误的门禁结论。
    if not results and not reg.get("total"):
        all_pass = False
        gate_badge = ("<span class='gate' style='background:var(--warn-soft);"
                      "color:var(--warn)'>尚未执行</span>")
        gate_desc = "核心回归尚未执行"
    else:
        all_pass = bool(reg.get("all_pass"))
        gate_badge = ("<span class='gate ok'>全部通过</span>" if all_pass
                      else "<span class='gate bad'>存在失败</span>")
        gate_desc = "最近一次核心回归符合门禁" if all_pass else "最近一次核心回归未通过门禁"
    _sub_reg = (f"核心回归通过 {reg.get('passed')}/{reg.get('total')}{skip_txt}"
                if (results or reg.get("total")) else "核心回归尚未执行")

    # 本次生成方式（可追溯）：模式 + 是否注入了历史易错点 + 需求/用例条数
    _rmeta = _read_run_meta(pdir)
    gen_item = ""
    if _rmeta:
        _pv = _rmeta.get("provenance") or {}
        _parts = [_h(str(_rmeta.get("mode", "")))]
        if _rmeta.get("lessons_injected"):
            _parts.append("注入历史易错点")
        if _rmeta.get("cases"):
            _parts.append(f"{_rmeta.get('cases')} 条用例")
        # 重点覆盖：native 与 backfilled **分开展示** ——
        # 只报"覆盖 5/5"会把"流水线补齐的"和"模型自发覆盖的"混为一谈。
        if _rmeta.get("focus_total"):
            _fn = _rmeta.get("focus_native", 0)
            _fp = _rmeta.get("focus_persisted", 0)
            _fb = _rmeta.get("focus_backfilled", 0)
            _tail = []
            if _fp:
                _tail.append(f"{_fp} 条沿用上轮")
            if _fb:
                _tail.append(f"{_fb} 条本轮补齐")
            _parts.append(f"重点覆盖 {_fn}/{_rmeta.get('focus_total')}（生成即覆盖）"
                          + (f"[{'/'.join(_tail)}]" if _tail else ""))
        # 降级必须显式标红：这批用例看着"生成好了"，其实是模板占位，
        # 不标出来就会被当成 LLM 质量的产物直接拿去评审/执行。
        _degraded = bool(_pv.get("degraded") or _rmeta.get("degraded"))
        _style = "font-size:14px;font-weight:700"
        _tip = ""
        if _degraded:
            _reason = _h(str(_pv.get("degrade_reason") or "原因未记录")).replace("'", "&#39;")
            _parts.append("⚠️ 降级产出")
            _style += ";color:var(--bad)"
            _tip = f" title='本次 LLM 未生效、已退回规则版：{_reason}'"
        elif _pv.get("confidence"):
            _parts.append(f"可信度 {_h(str(_pv['confidence']))}")
        gen_item = ("<div class='summary-item'><span class='k'>生成模式</span>"
                    f"<span class='v' style='{_style}'{_tip}>"
                    f"{' · '.join(_parts)}</span></div>")

    # 性能与安全冒烟（执行过才有；门禁独立于核心回归，各自给结论）
    _pf = _read_perf_security(pdir)
    ps_item = ""
    ps_card_html = ""
    if _pf:
        _ps_ok = bool(_pf.get("all_pass"))
        ps_item = ("<div class='summary-item'><span class='k'>性能与安全</span>"
                   f"<span class='v' style='font-size:15px;font-weight:800;"
                   f"color:var({'--ok' if _ps_ok else '--bad'})'>"
                   f"{'通过' if _ps_ok else '未通过'}</span></div>")
        ps_card_html = _perf_security_card_html(_pf)

    # Web UI 冒烟（执行过才有；门禁独立于核心回归与性能安全，各自给结论）
    _wf = _read_web(pdir)
    web_item = ""
    web_card_html = ""
    if _wf:
        _w_ok = bool(_wf.get("all_pass"))
        web_item = ("<div class='summary-item'><span class='k'>Web UI</span>"
                    f"<span class='v' style='font-size:15px;font-weight:800;"
                    f"color:var({'--ok' if _w_ok else '--bad'})'>"
                    f"{'通过' if _w_ok else '未通过'}</span></div>")
        web_card_html = _web_card_html(_wf)

    # 待提交缺陷草稿（把"流水线上的红"变成能提交给开发的缺陷单）
    _dp = _read_defects(pdir)
    defects_card_html = _defects_card_html(_dp) if _dp else ""

    # 失败项新旧对比（回答"先看哪一个"，不参与门禁判定）
    _diff = _read_diff(pdir)
    diff_card_html = _diff_card_html(_diff) if _diff else ""

    # 用例结构质量分（执行过需求→用例才有；只做展示与趋势，不做硬门禁）
    _q = _read_quality(pdir)
    quality_item = ""
    quality_card_html = ""
    if _q and _q.get("total") is not None:
        _qt = int(_q["total"])
        _qc = ("var(--ok)" if _qt >= 80 else ("var(--warn)" if _qt >= 60 else "var(--bad)"))
        quality_item = ("<div class='summary-item'><span class='k'>用例结构分</span>"
                        f"<span class='v' style='font-size:15px;font-weight:800;color:{_qc}'>"
                        f"{_qt}</span></div>")
        quality_card_html = _quality_card_html(_q)

    # AI 探索测试（执行过才有；**可降级阶段**：降级也照实展示，不渲染成绿灯）
    _ag = _read_agentic(pdir)
    agentic_item = ""
    agentic_card_html = ""
    if _ag:
        if _agentic_contract().is_ok(_ag):
            agentic_item = ("<div class='summary-item'><span class='k'>AI 探索</span>"
                            f"<span class='v' style='font-size:15px;font-weight:800;color:var(--ok)'>"
                            f"已执行 {_h(_ag.get('bug_count', 0))} 发现</span></div>")
        else:
            _reason = _agentic_contract().DEGRADE_LABEL.get(
                str(_ag.get('reason')), str(_ag.get('reason') or '未执行'))
            _reason = _h(_reason).replace("'", "&#39;")     # 属性值用单引号包裹，先转义
            agentic_item = ("<div class='summary-item'><span class='k'>AI 探索</span>"
                            f"<span class='v' style='font-size:15px;font-weight:800;color:var(--warn)'"
                            f" title='{_reason}'>已降级</span></div>")
        agentic_card_html = _agentic_card_html(pdir, _ag)

    # 效率口径（§10 #2）：本轮各阶段耗时。**未执行的阶段照实写「未执行」**，
    # 绝不写 0 秒 —— 否则"没跑"在报告上看起来像"跑得飞快"。
    _tmg = timing_mod.load((_rmeta or {}).get("timing"))
    timing_item = ""
    timing_card_html = ""
    if _tmg:
        timing_item = (
            "<div class='summary-item'><span class='k'>流水线耗时</span>"
            f"<span class='v' style='font-size:15px;font-weight:800'"
            f" title='{_h(_tmg.get('scope', ''))}'>"
            f"{_h(timing_mod.format_seconds(_tmg.get('total_seconds')))}</span></div>")
        timing_card_html = _timing_card_html(_tmg)

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
.tag.warn{{background:var(--warn-soft);color:var(--warn);}}
.ps-h{{font-size:13px;font-weight:800;color:var(--ink);margin:18px 0 10px;padding-left:9px;border-left:3px solid var(--primary);}}
.ps-sub{{font-size:12px;color:var(--muted);margin:6px 0 10px;line-height:1.7;}}
.ps-note{{font-size:12.5px;padding:10px 12px;border-radius:var(--radius-sm);margin:8px 0;line-height:1.6;}}
.ps-note.warn{{background:var(--warn-soft);color:#92400e;}}
.ps-note.bad{{background:var(--bad-soft);color:#991b1b;}}
.ps-list{{margin:6px 0 0;padding-left:20px;font-size:12.5px;line-height:1.7;}}
.ps-note a{{color:inherit;text-decoration:underline;}}
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
.q-head{{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap;margin-bottom:16px;}}
.q-total{{font-size:34px;font-weight:800;line-height:1;color:var(--ink);}}
.q-total-max{{font-size:13px;color:var(--muted-2);font-weight:600;margin-left:2px;}}
.q-delta{{font-size:13px;font-weight:700;}}
.q-meta{{font-size:12px;color:var(--muted);margin-left:auto;}}
.q-dims{{display:flex;flex-direction:column;gap:8px;}}
.q-row{{display:grid;grid-template-columns:64px 1fr 36px;gap:10px;align-items:center;font-size:12.5px;}}
.q-k{{font-size:12px;color:var(--muted);font-weight:700;}}
.q-bar{{height:8px;background:var(--surface);border:1px solid var(--border-2);border-radius:999px;overflow:hidden;}}
.q-bar-fill{{height:100%;border-radius:999px;}}
.q-v{{font-size:12.5px;font-weight:800;text-align:right;font-family:var(--mono);}}
.q-hint{{grid-column:2 / span 2;font-size:11px;color:var(--muted-2);margin-top:-4px;}}
.q-trend{{margin-top:16px;display:flex;align-items:center;gap:10px;}}
.q-disclaimer{{margin-top:14px;font-size:11.5px;color:var(--muted);background:var(--surface);
  border:1px solid var(--border-2);border-radius:var(--radius-sm);padding:9px 12px;line-height:1.65;}}
.q-notes{{margin:10px 0 0;padding-left:20px;font-size:12px;color:var(--muted);line-height:1.7;}}
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
  <div class='subtitle'>生成时间：{now} · {_sub_reg}</div>
</header>

<div class='summary-card'>
  <div class='summary-item'><span class='k'>门禁状态</span><span class='v'>{gate_badge}</span></div>
  <div class='summary-item'><span class='k'>场景总数</span><span class='v'>{reg.get('total', 0)}</span></div>
  <div class='summary-item'><span class='k'>通过</span><span class='v' style='color:var(--ok)'>{reg.get('passed', 0)}</span></div>
  <div class='summary-item'><span class='k'>失败</span><span class='v' style='color:var(--bad)'>{reg.get('failed', 0)}</span></div>
  {gen_item}
  {ps_item}
  {web_item}
  {agentic_item}
  {quality_item}
  {timing_item}
  <div class='summary-item' style='margin-left:auto;'><span class='k' style='text-align:right;'>{gate_desc}</span></div>
</div>

<div class='card'>
  <div class='card-title'>核心业务回归结果 <span class='count'>{len(results)}</span></div>
  {table_html}
</div>

{ps_card_html}

{web_card_html}

{agentic_card_html}

{defects_card_html}

{diff_card_html}

{quality_card_html}

{timing_card_html}

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


def _load_run_store():
    """加载 web_console/run_store（用 importlib 直接加载文件，避免触发 web_console 包的副作用）。"""
    import importlib.util
    name = "_pm_run_store"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, str(ROOT / "web_console" / "run_store.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ----------------------------------------------------------------------------
# 全流程 run / 核心回归的**阶段实现**与**阶段注册表**（审阅报告 §4.4 / §7 序 7）
#
# 这些 `_stage_*` 原先是 cmd_run / cmd_regression 里的顺序调用，现按"一阶段一函数"
# 抽出：阶段之间不再靠"局部变量作用域"隐式传值，而经 `_RunCtx` 显式传递产出；
# 顺序也不再靠书写位置，而由注册表里的 `requires` 声明、经稳定拓扑排序解析出来。
#
# ★ 唯一一条"顺序错了会静默出错"的强约束：`diff` 必须在 `snapshot` 之前 ——
#   否则历史里最新一条就是本次自己，自己跟自己比，结论永远是"没有新增失败"。
#   它现在写在 `_run_stages()` / `_regression_stages()` 的 `snapshot.requires` 上，
#   由 `tests/test_pipeline_registry.py` 的等价性守卫守住。
# ----------------------------------------------------------------------------
@dataclass
class _RunCtx:
    """阶段之间共享的上下文。

    字段的**读写**即阶段间的数据依赖（谁产出、谁消费），与 `requires` 声明的
    顺序依赖相互印证：顺序若被调换导致字段未就绪，会立刻在运行期暴露，
    而不是悄悄用到上一次的旧值。
    """
    args: Any
    pid: str
    pdir: Path
    base_url: str
    run_store: Any
    lessons: Any                            # extensions/memory/lessons 模块
    use_llm: bool = False
    use_agentic: bool = False
    inject_text: str = ""                   # 两类记忆合成后的注入文本
    lessons_count: int = 0                  # 注入的易错点条数（溯源用）
    lessons_injected: bool = False
    knowledge_injected: bool = False
    knowledge_picked: List[Dict[str, Any]] = field(default_factory=list)
    snapshot_trigger: str = "run"           # 快照来源标记（run / regression）
    # ---- 阶段产出 ----
    cases: Optional[Path] = None
    reg: Dict[str, Any] = field(default_factory=dict)
    focus: Optional[Dict[str, Any]] = None
    diff_payload: Optional[Dict[str, Any]] = None
    meta_fields: Dict[str, Any] = field(default_factory=dict)
    mode: str = "规则版"
    req_count: int = 0
    case_count: int = 0
    provenance: Optional[Dict[str, Any]] = None
    degraded: bool = False
    qfail: Optional[Tuple[Any, int, str]] = None
    # 效率口径的原料：`run_stages(on_stage=...)` 逐阶段回调进来的 `StageOutcome`。
    # 只是**收集**，不出结论 —— 汇总/判定在 `common/timing.py`（口径唯一）。
    timing_outcomes: List[Any] = field(default_factory=list)


def _stage_requirements(ctx: _RunCtx) -> None:
    """① 需求 → 用例（LLM 增强 / 智能体编排 / 规则版，失败自动降级）。"""
    ctx.cases = _step_requirements(
        ctx.pid, ctx.pdir, use_llm=ctx.use_llm, agentic=ctx.use_agentic,
        extra_context=ctx.inject_text,
        injected={"lessons": ctx.lessons_count,
                  "knowledge": len(ctx.knowledge_picked)})


def _stage_focus(ctx: _RunCtx) -> None:
    """情景记忆回灌闭环（Phase 2）：**注入之后必须校验**。

    只注入不校验 = 把"提示词发出去了"当成"结果达成了"，这是假绿的另一种形态：
    模型没采纳、或清单项与本次需求无关时，流水线照样打印"已注入历史易错点"。
    校验发现缺口就补成骨架用例，但**如实标注是回灌补齐的**，不冒充生成时自发覆盖。
    """
    try:
        _f_items = ctx.lessons.cluster_failures(
            ctx.lessons.extract_failures(ctx.run_store.list_snapshots(ctx.pid, limit=500)))
        if ctx.cases and Path(ctx.cases).is_file() and _f_items:
            ctx.focus = _step_focus(ctx.pdir, Path(ctx.cases), _f_items)
    except Exception as e:      # 校验失败不能中断流水线
        # 必须出声：跳过校验 = 回灌闭环没有兑现，而流水线表面照常完成。
        log.warning("  [重点覆盖] 校验跳过（回灌闭环未兑现）：%s", e)


def _stage_meta(ctx: _RunCtx) -> None:
    """记录本次生成方式（可追溯）：模式 / 是否注入历史易错点 / 需求与用例条数，并写首份 run_meta。

    溯源信息**从产物回读**，不拿参数重新推一遍 ——
    两处各推一次迟早漂移（比如降级只发生在一处），而 cases.md 里的标记就是事实。
    """
    ctx.mode = ("智能体多步自审编排" if (ctx.use_llm and ctx.use_agentic)
                else ("LLM 增强" if ctx.use_llm else "规则版"))
    ctx.case_count = 0
    if ctx.cases and Path(ctx.cases).is_file():
        try:
            _, _rows = _parse_cases(Path(ctx.cases).read_text(encoding="utf-8"))
            ctx.case_count = len(_rows)
        except Exception:
            # 可忽略：用例条数仅供 run_meta 溯源展示，解析失败记 0，不参与判定。
            ctx.case_count = 0
    ctx.req_count = 0
    try:
        sys.path.insert(0, str(ROOT / "extensions" / "requirements_to_cases"))
        import generate_cases as _gc  # noqa: E402
        _rf = ctx.pdir / "requirements.md"
        if _rf.is_file():
            ctx.req_count = len(_gc.parse_requirements(_rf.read_text(encoding="utf-8")))
    except Exception:
        # 可忽略：需求条数仅供 run_meta 溯源展示，取不到记 0，不参与判定。
        ctx.req_count = 0
    ctx.provenance = None
    if ctx.cases and Path(ctx.cases).is_file():
        try:
            sys.path.insert(0, str(ROOT / "extensions" / "requirements_to_cases"))
            import provenance as _pvmod  # noqa: E402
            ctx.provenance = _pvmod.parse(Path(ctx.cases).read_text(encoding="utf-8"))
        except Exception:
            # 可忽略：溯源标记读不到就按"无标记"处理，不参与判定。
            ctx.provenance = None
    ctx.degraded = bool((ctx.provenance or {}).get("degraded"))

    _focus_fields: Dict[str, Any] = {}
    if ctx.focus:
        # 分开记：native（生成即覆盖）与 backfilled（回灌补齐）不能合并成一个"覆盖率"——
        # 合并之后"补出来的覆盖"会和"模型真学会了"长得一样。
        _focus_fields = {"focus_total": ctx.focus.get("total"),
                         "focus_native": ctx.focus.get("native"),
                         "focus_persisted": ctx.focus.get("persisted"),
                         "focus_backfilled": ctx.focus.get("backfilled"),
                         "focus_rate": ctx.focus.get("rate")}
    ctx.meta_fields = dict(
        mode=ctx.mode, use_llm=ctx.use_llm, agentic=ctx.use_agentic,
        lessons_injected=bool(ctx.lessons_injected),
        knowledge_injected=bool(ctx.knowledge_injected),
        requirements=ctx.req_count, cases=ctx.case_count,
        degraded=ctx.degraded, provenance=ctx.provenance, **_focus_fields)
    _write_run_meta(ctx.pdir, **ctx.meta_fields)


def _stage_quality(ctx: _RunCtx) -> None:
    """L3 评测常态化：每次 run 都算一次结构质量分（确定性、零依赖、不联网），落盘供报告/看板/控制台读。

    默认**只展示与看趋势、不做门禁**；只有显式传了 --quality-min 才按未达标处理
    （且真正的失败退出放到最后判定，先把报告生成出来）。
    """
    _qmin = getattr(ctx.args, "quality_min", None)
    if not (ctx.cases and Path(ctx.cases).is_file()):
        return
    try:
        sys.path.insert(0, str(ROOT / "extensions" / "requirements_to_cases"))
        import case_quality as _cq  # noqa: E402
        _q = _step_quality(ctx.pdir, Path(ctx.cases).read_text(encoding="utf-8"),
                           requirement_count=ctx.req_count or None, mode=ctx.mode)
        if _q and _q.get("total") is not None:
            ctx.meta_fields["quality"] = _q["total"]
        if _qmin is not None:
            # 算不出分数时 check_min 判**不达标** —— 把"没算出来"当"达到要求"是假绿
            _qok, _qmsg = _cq.check_min(_q or {"total": None}, _qmin)
            ctx.meta_fields["quality_gate"] = {"min": int(_qmin),
                                               "total": (_q or {}).get("total"),
                                               "passed": bool(_qok)}
            if not _qok:
                ctx.qfail = ((_q or {}).get("total"), int(_qmin), _qmsg)
            _write_run_meta(ctx.pdir, **ctx.meta_fields)
    except Exception as e:  # 打分失败绝不能拖垮主流程
        log.warning("  [质量分] 计算失败（不影响流程）：%s", e)


def _stage_api(ctx: _RunCtx) -> None:
    """② 接口自动化（pytest；被测服务不可达会自动 skip）。"""
    _step_api(ctx.pid, ctx.pdir, ctx.base_url)


def _stage_regression(ctx: _RunCtx) -> None:
    """③ 核心业务回归。"""
    ctx.reg = _step_regression(ctx.pid, ctx.pdir)


def _stage_perf(ctx: _RunCtx) -> None:
    """④ 性能与安全冒烟（--perf）：独立门禁，结论并入 run_meta 便于追溯。

    开关**不在本函数里判**，而是声明在注册表的 `enabled=` 上（见 `_flag`）——
    "进来先 return"会让"本次没跑"与"跑得飞快"在效率口径里长得一模一样（都是 ≈0 秒）。
    """
    pf = _step_perf_security(ctx.pdir)
    ctx.meta_fields["perf_security"] = {"all_pass": bool(pf.get("all_pass")),
                                        "summary": pf.get("summary", "")}
    _write_run_meta(ctx.pdir, **ctx.meta_fields)


def _stage_web(ctx: _RunCtx) -> None:
    """⑤ Web UI 冒烟（--web）：独立门禁。

    缺 web.yaml 时**不静默跳过**——静默跳过等于悄悄放松门禁，所以显式告警并记入 run_meta。
    开关（--web 有没有开）声明在注册表的 `enabled=` 上，不在这里判。
    """
    if not (ctx.pdir / "web.yaml").is_file():
        log.warning("  [Web 冒烟] ⚠ 未执行：缺少 %s；"
                    "本次不产出 Web 结论（门禁不应据此判绿），请补场景后重跑 --web。",
                    ctx.pdir / "web.yaml")
        ctx.meta_fields["web"] = {"executed": False,
                                  "reason": "缺少 web.yaml，未执行"}
    else:
        wf = _step_web(ctx.pdir, browser=getattr(ctx.args, "browser", None))
        ctx.meta_fields["web"] = {"executed": True,
                                  "all_pass": bool(wf.get("all_pass")),
                                  "summary": wf.get("summary", "")}
    _write_run_meta(ctx.pdir, **ctx.meta_fields)


def _stage_agentic(ctx: _RunCtx) -> None:
    """⑥ AI 探索测试（--explore）：**可降级阶段**，不是门禁。

    缺 mission.yaml 时同样**不静默跳过**（与 --web 同源口径）：显式告警并记入 run_meta。
    注意：降级**不影响退出码** —— 探索只产出"发现"，不产出 pass/fail；
    但它"有没有真的跑"必须留在案（否则报告看不出少了这一环）。
    开关（--explore 有没有开）声明在注册表的 `enabled=` 上，不在这里判。
    """
    _mission_arg = getattr(ctx.args, "mission", None)
    if not (ctx.pdir / "mission.yaml").is_file() and not _mission_arg:
        log.warning("  [AI 探索] ⚠ 未执行：缺少 %s；本次不产出探索结论"
                    "（不得据此判为「已探索」），请补任务声明后重跑 --explore。",
                    ctx.pdir / "mission.yaml")
        ctx.meta_fields["agentic"] = {"executed": False, "status": None,
                                      "reason": "not_configured",
                                      "summary": "缺少 mission.yaml，未执行"}
    else:
        _ag = _step_agentic(
            ctx.pdir,
            mission=Path(_mission_arg) if _mission_arg else None,
            max_steps=int(getattr(ctx.args, "explore_max_steps", 30) or 30),
            timeout=getattr(ctx.args, "explore_timeout", None),
            headed=bool(getattr(ctx.args, "explore_headed", False)),
        )
        ctx.meta_fields["agentic"] = _agentic_contract().summarize_for_meta(_ag)
    _write_run_meta(ctx.pdir, **ctx.meta_fields)


def _stage_diff(ctx: _RunCtx) -> None:
    """失败项新旧对比：把「这次新红的」从「一直红的」里挑出来。

    ★ **必须在写本次快照之前**（注册表里 `snapshot.requires` 含 `"diff"`）——
    否则历史里最新一条就是本次自己，自己跟自己比，结论永远是"没有新增失败"。
    这条顺序约束原先只写在注释里，现在是 `requires` 上的一条边。
    """
    try:
        ctx.diff_payload = _step_diff(ctx.pid, ctx.pdir, ctx.reg, ctx.run_store)
    except Exception as e:      # 对比失败不能拖垮主流程
        log.warning("  [新旧对比] 失败（不影响流程）：%s", e)


def _stage_snapshot(ctx: _RunCtx) -> None:
    """★ 写本次快照 + 重建 lessons.md（失败根因闭环）。"""
    ctx.run_store.insert_snapshot(ctx.pid, ctx.reg, trigger=ctx.snapshot_trigger)
    ctx.lessons.rebuild_from_snapshots(
        ctx.run_store.list_snapshots(ctx.pid, limit=500), ctx.pdir)


def _stage_defects(ctx: _RunCtx) -> None:
    """缺陷草稿：把"流水线上的红"整理成能提交给开发的缺陷单（**不自动提单**）。"""
    try:
        _step_defects(ctx.pid, ctx.pdir, ctx.reg, ctx.base_url, diff=ctx.diff_payload)
    except Exception as e:      # 草稿生成失败不能拖垮主流程
        log.warning("  [缺陷草稿] 生成失败（不影响流程）：%s", e)


def _finalize_timing(ctx: _RunCtx) -> None:
    """把已测各阶段耗时**定格**成效率口径载荷，写进 run_meta。

    为什么在报告之前做：报告是**持久化产物的渲染器**（其它卡片都从各自产物读），
    所以口径也必须先落盘、再渲染 —— 否则卡片读的是内存里的临时状态，与别家不同源。
    代价是范围止于"报告生成前"（report 自身渲染无法预知自己的耗时），
    该排除项写在载荷的 `excluded` 里，不靠读者猜。
    """
    payload = timing_mod.build(timing_mod.from_outcomes(ctx.timing_outcomes))
    ctx.meta_fields["timing"] = payload
    _write_run_meta(ctx.pdir, **ctx.meta_fields)
    for line in timing_mod.render_lines(payload):
        log.info("%s", line)


def _stage_report(ctx: _RunCtx) -> None:
    """报告总览（聚合各阶段结论）。

    时序：**先定格效率口径**（把已测阶段耗时写进 run_meta），再渲染报告。
    """
    _finalize_timing(ctx)
    _step_report(ctx.pid, ctx.pdir, ctx.cases, ctx.reg)


def _flag(name: str):
    """注册表用的小工具：把 CLI 开关声明成阶段的「本次该不该跑」（`Stage.enabled`）。

    为什么不在阶段体内"进去先 return"：那样"没跑"会以 ≈0 秒的形态混进效率口径，
    报告上看起来像"这个能力很快"，实际是"这个能力根本没发生"。
    """
    return lambda ctx: bool(getattr(ctx.args, name, False))


def _run_stages() -> List[Stage]:
    """全流程 `run` 的阶段注册表 —— **顺序的唯一声明处**（§4.4）。

    `requires` 里既有**数据依赖**（消费上游产出），也有**写序约束**，两者不区分对待：
    对执行顺序的约束力完全一样。

    ★ 承重的一条：`snapshot` 依赖 `diff`。删掉它，`resolve_order` 会把"写快照"提到
      "新旧对比"之前 → 结论永远是"没有新增失败"，而且**不会有别的测试变红**。
      `tests/test_pipeline_registry.py` 就是那条会变红的线。
    """
    return [
        Stage("requirements", _stage_requirements,
              doc="需求→用例（LLM 增强 / 智能体编排 / 规则版，失败自动降级）"),
        Stage("focus", _stage_focus, requires=("requirements",),
              doc="重点覆盖校验：情景记忆回灌闭环（Phase 2）"),
        Stage("meta", _stage_meta, requires=("requirements", "focus"),
              doc="记录本次生成溯源并写首份 run_meta"),
        Stage("quality", _stage_quality, requires=("requirements",),
              doc="用例结构质量分（默认只展示，不做门禁）"),
        Stage("api", _stage_api,
              doc="接口自动化（pytest；被测不可达自动 skip）"),
        Stage("regression", _stage_regression, doc="核心业务回归"),
        # 下面三个是**可选能力**：开关写在 `enabled=` 上而不是阶段体内 ——
        # 判否时整段跳过，效率口径里记为"未执行"，而不是伪装成"0.000s 很快"。
        Stage("perf", _stage_perf, requires=("regression",), enabled=_flag("perf"),
              doc="④ 性能与安全冒烟（--perf）"),
        Stage("web", _stage_web, requires=("regression",), enabled=_flag("web"),
              doc="⑤ Web UI 冒烟（--web）"),
        Stage("agentic", _stage_agentic, requires=("regression",), enabled=_flag("explore"),
              doc="⑥ AI 探索测试（--explore，可降级阶段）"),
        Stage("diff", _stage_diff, requires=("regression",),
              doc="失败项新旧对比（消费历史快照）"),
        Stage("snapshot", _stage_snapshot, requires=("diff",),
              doc="★写本次快照 + 重建 lessons（**必须在 diff 之后**）"),
        Stage("defects", _stage_defects, requires=("regression", "diff"),
              doc="缺陷草稿（整理失败项，不自动提单）"),
        Stage("report", _stage_report,
              requires=("requirements", "api", "quality", "regression",
                        "perf", "web", "agentic", "defects"),
              doc="报告总览（聚合各阶段结论）"),
    ]


def _regression_stages() -> List[Stage]:
    """核心回归 `regression` 的阶段注册表（与 run **共用** `_stage_*` 实现）。

    同样把"`diff` 必须在 `snapshot` 之前"这条约束显式声明出来 —— 原先两处命令各写
    一遍注释，现在收敛到同一套 `requires`，口径只此一处。
    """
    return [
        Stage("regression", _stage_regression, doc="核心业务回归"),
        Stage("diff", _stage_diff, requires=("regression",),
              doc="失败项新旧对比（消费历史快照）"),
        Stage("snapshot", _stage_snapshot, requires=("diff",),
              doc="★写本次快照 + 重建 lessons（**必须在 diff 之后**）"),
        Stage("defects", _stage_defects, requires=("regression", "diff"),
              doc="缺陷草稿（不自动提单）"),
    ]


def cmd_run(args: argparse.Namespace) -> None:
    meta = load_project(args.id)
    pdir = PROJECTS_DIR / args.id
    base_url = (meta.get("env", {}) or {}).get("base_url", "http://localhost:8080")
    print(f"== 全流程测试：{args.id}（环境 {base_url}）==")
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "artifacts").mkdir(exist_ok=True)

    # P2 情景记忆：读历史易错点注入需求→用例；run 之后写快照并重建 lessons.md
    sys.path.insert(0, str(ROOT / "extensions" / "memory"))
    import lessons as ls  # noqa: E402
    import knowledge as kn  # noqa: E402
    run_store = _load_run_store()
    run_store.init_db(ROOT / "runs.db")
    inject = ls.to_inject_prompt(pdir)
    ls_injected = inject        # 单独留存：run_meta 要能区分两类记忆各自有没有注入
    # 两类记忆互补，不要合并成一个概念：
    #   lessons.md   = 流水线自动生成，短期，回答"哪些场景容易红"
    #   knowledge.md = 人工维护，长期，回答"这个项目的取值约定是什么"
    _req_text = ""
    try:
        _rf = pdir / "requirements.md"
        if _rf.is_file():
            _req_text = _rf.read_text(encoding="utf-8")
    except Exception:
        _req_text = ""
    kinject = kn.to_inject_prompt(pdir, _req_text)
    kpicked = kn.explain(pdir, _req_text)["picked"] if kinject else []
    if kinject:
        k_titles = "、".join(p["title"] or "(无标题)" for p in kpicked)
        print(f"  [长期记忆] 从项目知识库拾取 {len(kpicked)} 段注入：{k_titles}")
        inject = ((inject or "") + "\n\n" + kinject).strip() if inject else kinject

    # 两类记忆合成一段，统一交给需求→用例；提示要能说清各自有没有生效，
    # 否则"注入了但没效果"时无从判断是没检索到还是模型没采纳。
    if ls_injected and kinject:
        print(f"  [记忆注入] 情景记忆 + 项目知识均已注入（共 {len(inject)} 字符）")
    elif ls_injected:
        print(f"  [情景记忆] 检测到历史易错点，已注入需求→用例生成（{len(inject)} 字符）")
    elif kinject:
        print(f"  [长期记忆] 已注入与本次需求相关的项目知识（{len(inject)} 字符）")

    # 注入量（条/段）要记进溯源标记：只知道"注入了"不够，
    # 下次看到"注入 0 段"才能判断是知识库没内容，还是检索没命中。
    _lcount = 0
    if ls_injected:
        try:
            _lmd = ls.load_lessons(pdir) or ""
            _lcount = len([_l for _l in _lmd.splitlines()
                           if re.match(r"^\d+\.\s", _l.strip())])
        except Exception:
            # 可忽略：注入计数仅供溯源标记展示，取不到就记 0，不参与判定。
            _lcount = 0

    ctx = _RunCtx(
        args=args, pid=args.id, pdir=pdir, base_url=base_url,
        run_store=run_store, lessons=ls,
        use_llm=bool(getattr(args, "llm", False)),
        use_agentic=bool(getattr(args, "agentic", False)),
        inject_text=inject, lessons_count=_lcount,
        lessons_injected=bool(ls_injected), knowledge_injected=bool(kinject),
        knowledge_picked=kpicked, snapshot_trigger="run")

    # ---- 注册表驱动执行 ----
    # 阶段顺序由 _run_stages() 的 requires 经**稳定拓扑排序**解析决定，不再靠本函数里的
    # 书写位置（§4.4：把隐式顺序变成显式依赖）。改顺序 = 改注册表，且有等价性守卫。
    stages = _run_stages()
    log.info("E 层阶段顺序（拓扑解析）：%s", " → ".join(resolve_order(stages)))
    # 横切关注点：逐阶段收集耗时（`StageOutcome`），供效率口径（§10 #2）汇总。
    # 只是**收集**——汇总与"测不到什么"的声明都在 `common/timing.py`（口径唯一）。
    run_stages(stages, ctx, on_stage=ctx.timing_outcomes.append)

    print(f"\n✅ 全流程完成。报告：{pdir / 'artifacts' / 'report.html'}")
    # 效率口径：把"这一轮花了多久、花在哪"如实打出来。
    # 未执行 / 未测量项由口径层显式声明（"未执行"不写成 0 秒），这里不另作解释。
    print("  " + timing_mod.summary_line(ctx.meta_fields.get("timing")))

    # 可选质量门禁（--quality-min）：放在报告之后判定，保证失败时也有报告可查。
    if ctx.qfail:
        got, need, why = ctx.qfail
        raise SystemExit(
            f"\n❌ 用例结构质量分未达标：{why}（--quality-min {need}；实际 {got}）。\n"
            f"   注意：结构分只反映形式完整性，未达标不代表用例一定有问题，"
            f"请先人工看一眼报告里的用例再决定是否调整阈值。")


def cmd_regression(args: argparse.Namespace) -> None:
    meta = load_project(args.id)
    pdir = PROJECTS_DIR / args.id
    base_url = (meta.get("env", {}) or {}).get("base_url", "http://localhost:8080")
    print(f"== 核心业务回归：{args.id}（环境 {base_url}）==")

    # P2 情景记忆：写快照 + 重建 lessons.md（失败根因闭环）
    run_store = _load_run_store()
    run_store.init_db(ROOT / "runs.db")
    sys.path.insert(0, str(ROOT / "extensions" / "memory"))
    import lessons as ls  # noqa: E402

    # 与全流程 run **共用**阶段实现与注册表机制：`diff` 必须在 `snapshot` 之前
    # 这条约束不再靠注释，而是 _regression_stages() 里 snapshot.requires 的一条边
    # （原先两处命令各写一遍注释，现在口径只此一处）。
    ctx = _RunCtx(
        args=args, pid=args.id, pdir=pdir, base_url=base_url,
        run_store=run_store, lessons=ls, snapshot_trigger="regression")

    stages = _regression_stages()
    log.info("E 层阶段顺序（拓扑解析）：%s", " → ".join(resolve_order(stages)))
    run_stages(stages, ctx)

    sys.exit(0 if ctx.reg["all_pass"] else 1)


def cmd_defects(args: argparse.Namespace) -> None:
    """按最近一次门禁结果重算缺陷草稿（不重跑测试）。

    只做**整理**，不重跑、不提单 —— 改了缺陷描述想重新生成时用它。
    """
    meta = load_project(args.id)
    pdir = PROJECTS_DIR / args.id
    base_url = (meta.get("env", {}) or {}).get("base_url", "")
    sys.path.insert(0, str(ROOT / "extensions" / "reporting"))
    import defects as df  # noqa: E402

    reg = _read_regression(pdir)
    if not reg:
        print(f"⚠ {args.id} 尚未执行过核心回归，缺陷草稿只会有性能安全/Web 两道的结论。")
    payload = _step_defects(args.id, pdir, reg, base_url)
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print()
        print((pdir / "artifacts" / df.DEFECTS_MD).read_text(encoding="utf-8"))


def cmd_rerun(args: argparse.Namespace) -> None:
    pdir = PROJECTS_DIR / args.id
    print(f"== 单场景重跑：{args.id} / {args.scene} ==")
    reg = _step_rerun(args.id, pdir, args.scene)
    # 退出码语义：本次重跑是否执行成功，而非整项目门禁。
    # 重跑只影响一个场景，其它场景的历史失败不该算到本次头上（否则任务永远显示"失败"）。
    # 整体门禁请仍以 `regression` / `dashboard` 的退出码为准。
    sys.exit(0 if not reg.get("error") else 1)


def cmd_perf_security(args: argparse.Namespace) -> None:
    """性能 + 安全冒烟（独立子命令）。

    门禁语义与核心回归一致：环境不可达 → 全 SKIP → all_pass=False，退出码 1。
    """
    meta = load_project(args.id)
    pdir = PROJECTS_DIR / args.id
    base_url = (meta.get("env", {}) or {}).get("base_url", "")
    label = {"perf": "性能", "security": "安全"}.get(getattr(args, "only", None), "性能与安全")
    print(f"== {label}冒烟：{args.id}（环境 {base_url}）==")
    res = _step_perf_security(pdir,
                             users=getattr(args, "users", None),
                             iterations=getattr(args, "iterations", None),
                             only=getattr(args, "only", None))
    # 同步刷新报告，让「性能与安全」卡片立即可见（核心回归可能尚未跑过，报告会标注"尚未执行"）
    cases = pdir / "artifacts" / "cases.md"
    _step_report(args.id, pdir, cases if cases.is_file() else None, _read_regression(pdir))
    sys.exit(0 if res.get("all_pass") else 1)


def cmd_web(args: argparse.Namespace) -> None:
    """Web UI 冒烟（独立子命令）。

    门禁语义与核心回归一致：环境不可达 / 浏览器起不来 → 全 SKIP → all_pass=False，退出码 1。
    """
    meta = load_project(args.id)
    pdir = PROJECTS_DIR / args.id
    base_url = ((meta.get("env", {}) or {}).get("web_base_url")
                or (meta.get("env", {}) or {}).get("base_url", ""))
    print(f"== Web UI 冒烟：{args.id}（环境 {base_url or '未配置'}）==")
    res = _step_web(pdir, only=getattr(args, "only", None),
                    headed=bool(getattr(args, "headed", False)),
                    browser=getattr(args, "browser", None))
    # 同步刷新报告，让「Web UI 冒烟」卡片立即可见（核心回归可能尚未跑过）
    cases = pdir / "artifacts" / "cases.md"
    _step_report(args.id, pdir, cases if cases.is_file() else None, _read_regression(pdir))
    sys.exit(0 if res.get("all_pass") else 1)


def cmd_explore(args: argparse.Namespace) -> None:
    """AI 探索测试（独立子命令）。

    门禁语义与 ④/⑤ **不同**：探索不产出 pass/fail，所以
      - `ok`        → 退出码 0；
      - `degraded`  → 退出码 **3**（"没执行"，既不是通过也不是产品失败）。
    刻意与"门禁失败=1"区分开：把"未执行"和"执行了但不过"混成同一个码，
    正是本项目反复强调的"三态要分开"。
    """
    meta = load_project(args.id)
    pdir = PROJECTS_DIR / args.id
    base_url = (meta.get("env", {}) or {}).get("base_url", "")
    print(f"== AI 探索测试：{args.id}（环境 {base_url or '未配置'}）==")
    res = _step_agentic(pdir,
                        mission=Path(args.mission) if getattr(args, "mission", None) else None,
                        max_steps=int(getattr(args, "max_steps", 30) or 30),
                        timeout=getattr(args, "timeout", None),
                        headed=bool(getattr(args, "headed", False)))
    # 同步刷新报告，让「AI 探索」卡片立即可见；并留案到 run_meta（增量更新，不动别家字段）
    ac = _agentic_contract()
    _merge_run_meta(pdir, agentic=ac.summarize_for_meta(res))
    cases = pdir / "artifacts" / "cases.md"
    _step_report(args.id, pdir, cases if cases.is_file() else None, _read_regression(pdir))
    if getattr(args, "json", False):
        print(json.dumps(res, ensure_ascii=False, indent=2))
    sys.exit(0 if ac.is_ok(res) else 3)


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
            "ps": _read_perf_security(pdir),   # ④ 性能与安全冒烟（可能未执行）
            "web": _read_web(pdir),            # ⑤ Web UI 冒烟（可能未执行）
            "quality": _read_quality(pdir),    # 用例结构质量分（可能未执行）
            "defects": _read_defects(pdir),    # 缺陷草稿（可能未执行）
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
        ps = r.get("ps")
        if ps:
            pg = "✅ 通过" if ps.get("all_pass") else "❌ 未通过"
            perf = ps.get("perf") or {}
            ov = perf.get("overall") or {}
            extra = (f" · P95 {ov.get('p95_ms')}ms / 错误率 "
                     f"{float(ov.get('error_rate') or 0) * 100:.2f}%" if ov else "")
            print(f"  {'':<16}{'':<22}性能与安全：{pg}{extra}")
        wf = r.get("web")
        if wf:
            wg = "✅ 通过" if wf.get("all_pass") else "❌ 未通过"
            print(f"  {'':<16}{'':<22}Web UI：{wg} · {wf.get('summary', '')}")
        df = r.get("defects")
        if df and (df.get("counts") or {}).get("total"):
            c = df["counts"]
            print(f"  {'':<16}{'':<22}待确认缺陷：{c['total']} 条"
                  f"（S1 {c['by_severity'].get('S1', 0)} / S2 {c['by_severity'].get('S2', 0)}"
                  f" / S3 {c['by_severity'].get('S3', 0)}）· 草稿见 artifacts/defects.md")
            if c.get("env"):
                print(f"  {'':<16}{'':<22}另有 {c['env']} 项环境问题（已单列，不按缺陷处理）")
        q = r.get("quality")
        if q and q.get("total") is not None:
            d = q.get("delta")
            arrow = "首次" if d is None else ("持平" if d == 0 else (f"↑{d}" if d > 0 else f"↓{abs(d)}"))
            print(f"  {'':<16}{'':<22}用例结构分：{q['total']}/100（环比 {arrow}）"
                  f" · {len(q.get('history') or [])} 次历史")

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
        ps = r.get("ps")
        if not ps:
            ps_detail, ps_badge = "<span class='muted'>未执行</span>", "<span class='muted'>—</span>"
        else:
            _pok = bool(ps.get("all_pass"))
            ps_badge = "通过 ✅" if _pok else "未通过 ❌"
            _pov = (ps.get("perf") or {}).get("overall") or {}
            _s = ps.get("security") or {}
            ps_detail = (f"P95 {_pov.get('p95_ms', 0)}ms · 错误率 "
                         f"{float(_pov.get('error_rate') or 0) * 100:.2f}% · "
                         f"安全 {_s.get('passed_count', 0)}通过/{_s.get('failed', 0)}失败"
                         + (f"/{_s.get('warned')}提示" if _s.get("warned") else ""))
        wf = r.get("web")
        if not wf:
            web_detail, web_badge = "<span class='muted'>未执行</span>", "<span class='muted'>—</span>"
        else:
            _wok = bool(wf.get("all_pass"))
            web_badge = "通过 ✅" if _wok else "未通过 ❌"
            web_detail = (f"{wf.get('passed', 0)}通过/{wf.get('failed', 0)}失败"
                          f"/{wf.get('skipped', 0)}跳过"
                          + (f" · 抖动 {wf.get('flaky')}" if wf.get("flaky") else "")
                          + (f" · 配置问题 {len(wf.get('config_issues') or [])}"
                             if wf.get("config_issues") else ""))
        # 用例结构质量分：只展示与看趋势，**不是门禁**（结构分可被注水刷高）
        q = r.get("quality")
        if not q or q.get("total") is None:
            q_detail, q_badge = "<span class='muted'>未执行</span>", "<span class='muted'>—</span>"
        else:
            _qt = int(q["total"])
            _qc = ("var(--ok,#047857)" if _qt >= 80
                   else ("var(--warn,#b45309)" if _qt >= 60 else "var(--bad,#b91c1c)"))
            q_detail = (f"<b style='color:{_qc}'>{_qt}</b>/100"
                        + f" · 历史 {len(q.get('history') or [])} 次")
            d = q.get("delta")
            if d is None:
                q_badge = "<span class='muted'>首次</span>"
            elif d == 0:
                q_badge = "<span class='muted'>持平</span>"
            else:
                q_badge = (f"<span style='color:#047857'>↑{d}</span>" if d > 0
                           else f"<span style='color:#b91c1c'>↓{abs(d)}</span>")
        # 待确认缺陷草稿：只报数不提单；环境问题/配置问题不计入
        df = r.get("defects")
        if not df or not (df.get("counts") or {}).get("total"):
            df_detail = "<span class='muted'>—</span>"
        else:
            c = df["counts"]
            sev = c.get("by_severity") or {}
            df_detail = (f"<b style='color:#b91c1c'>{c['total']}</b> 条"
                         + (f" · S1 {sev.get('S1', 0)}" if sev.get("S1") else "")
                         + (f"<br><a href='projects/{_h(r['pid'])}/artifacts/defects.md'>"
                            "缺陷草稿</a>" if True else ""))
        trs.append(
            f"<tr class='{cls}'><td><b>{_h(r['pid'])}</b></td>"
            f"<td>{_h(r['name'])}</td><td>{_h(r['owner'])}</td>"
            f"<td><code>{_h(r['base_url'])}</code></td>"
            f"<td>{detail}</td><td class='gate-cell'>{badge}</td>"
            f"<td>{ps_detail}</td><td class='gate-cell'>{ps_badge}</td>"
            f"<td>{web_detail}</td><td class='gate-cell'>{web_badge}</td>"
            f"<td>{q_detail}</td><td class='gate-cell'>{q_badge}</td>"
            f"<td>{df_detail}</td>"
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
td.gate-cell{{font-weight:700;white-space:nowrap;}}
.muted{{color:#9ca3af;}}</style>
</head><body>
<h1>跨项目测试总览</h1>
<div class='meta'>生成时间：{now} · 项目数：{len(rows)} · 数据来源：各项目最近一次核心回归 / 性能与安全冒烟 / Web UI 冒烟 / 用例结构质量分</div>
<div class='meta'>「待确认缺陷」由失败项整理而成，严重程度是<b>规则推断的建议值</b>，需人工复核后提交；环境问题与配置问题<b>不计入</b>。</div>
<div class='meta'>「用例结构分」只反映形式完整性（覆盖/三类/可执行/具体/去重），<b>不是质量判定、也不做门禁</b>；看它的<b>趋势</b>——分数突然下跌说明生成环节可能退化。</div>
<table><tr><th>项目ID</th><th>名称</th><th>负责人</th><th>测试环境</th><th>核心回归</th><th>回归门禁</th><th>性能与安全</th><th>门禁</th><th>Web UI</th><th>门禁</th><th>用例结构分</th><th>环比</th><th>待确认缺陷</th><th>明细</th></tr>
{''.join(trs)}
</table>
<div class='meta'>重新生成：python project_manager.py dashboard · 性能与安全冒烟：python project_manager.py perf-security &lt;项目ID&gt; · Web UI 冒烟：python project_manager.py web &lt;项目ID&gt;</div>
</body></html>"""
    out = ROOT / "projects_dashboard.html"
    out.write_text(html_doc, encoding="utf-8")
    print(f"\n看板已生成：{out}")


def run_pipeline(req_file: str, run_api: bool = False, api_base: Optional[str] = None,
                 use_llm: bool = False, agentic: bool = False,
                 out_dir: Optional[str] = None, cases_path: Optional[str] = None) -> Path:
    """统一流水线：需求 -> 用例 -> [接口自动化] -> 报告总览。

    供 CLI ``run`` 与轻量入口（software_testing_agent.py）共用，消除双入口漂移。
    不依赖 LLM；传入 use_llm=True 且配置 LLM_API_KEY（OpenAI 兼容，如 Qwen/DeepSeek，
    provider 由 LLM_PROVIDER 决定）时需求→用例走 LLM 增强，失败自动降级规则版。
    agentic=True（需配合 use_llm）走智能体多步自审编排，质量更高但 4 次 LLM 调用。

    out_dir / cases_path：把产物落到指定目录（**给自动化测试用，默认行为不变**）。
    不传时仍写仓库（cases.md 是受版本控制的产物、报告落仓库根），这是正常用法；
    但测试若沿用默认路径就会**污染工作区**（还得靠 `git checkout` 事后还原），
    且仓库根的文件可能被编辑器/预览面板占用 → Windows 上偶发 PermissionError。
    让调用方能指定落盘位置，测试即可完全隔离，不需要任何事后清理。
    """
    # 1) 需求 -> 用例
    sys.path.insert(0, str(ROOT / "extensions" / "requirements_to_cases"))
    import generate_cases as gc  # noqa: F401  (延迟导入，避免基座依赖常驻)
    text = Path(req_file).read_text(encoding="utf-8")
    md = gc.generate_from_text(text, use_llm=use_llm, agentic=agentic, source=req_file)
    cases_md = Path(cases_path) if cases_path else ROOT / "extensions" / "requirements_to_cases" / "cases.md"
    cases_md.parent.mkdir(parents=True, exist_ok=True)
    cases_md.write_text(md, encoding="utf-8")
    items = gc.parse_requirements(text)
    _, rows = _parse_cases(md)
    _mode = "智能体多步编排" if (use_llm and agentic) else ("LLM 增强" if use_llm else "规则版")
    print(f"[1/3] 需求→用例：解析 {len(items)} 条需求，{_mode}生成 {len(rows)} 条用例 → {cases_md}")

    # 结构质量分：这个轻量入口没有项目目录（产物落在仓库根），无处落盘，
    # 但"每次生成都算分"要成立 —— 至少把结论打到 stdout，不能一声不吭。
    try:
        import case_quality as _cq  # noqa: E402  (同目录，已在 sys.path 中)
        _q = _cq.score_cases_md(md, requirement_count=len(items))
        _total = _q.get("total")
        if _total is None:
            print("[1/3] 用例结构质量分：无法计分（用例为空或缺少可判定维度）")
        else:
            _notes = _q.get("notes") or []
            _suffix = f"（{'; '.join(_notes)}）" if _notes else ""
            print(f"[1/3] 用例结构质量分：{_total}/100{_suffix}")
    except Exception as e:
        log.warning("[1/3] 用例结构质量分：计算失败：%s", e)

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
    out = (Path(out_dir) if out_dir else ROOT) / "test_report_index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
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
    r.add_argument("--llm", action="store_true",
                   help="需求→用例采用 LLM 增强（需 LLM_API_KEY；失败自动降级规则版）")
    r.add_argument("--agentic", action="store_true",
                   help="LLM 增强启用智能体多步自审编排（分析→初版→自评审→终版，质量更高但 4 次调用）；需配合 --llm")
    r.add_argument("--perf", action="store_true",
                   help="追加 ④ 性能与安全冒烟（并发延迟 + 鉴权/注入/泄露/配置检查）")
    r.add_argument("--web", action="store_true",
                   help="追加 ⑤ Web UI 冒烟（Playwright 声明式场景，需项目下存在 web.yaml）")
    r.add_argument("--explore", action="store_true",
                   help="追加 ⑥ AI 探索测试（基座自主探索；需项目下存在 mission.yaml）。"
                        "该阶段**可降级**：无 key/超时/异常时降级并出声，不会让全流程变红")
    r.add_argument("--mission", default=None,
                   help="配合 --explore：指定 mission 文件（默认 <项目>/mission.yaml）")
    r.add_argument("--explore-max-steps", type=int, default=30, metavar="N",
                   help="配合 --explore：基座软步数上限（默认 30）")
    r.add_argument("--explore-timeout", type=float, default=None, metavar="SEC",
                   help="配合 --explore：阶段墙钟上限（秒），默认取 STA_EXPLORE_TIMEOUT / 1800")
    r.add_argument("--explore-headed", action="store_true",
                   help="配合 --explore：浏览器带界面跑（调试用）")
    r.add_argument("--quality-min", type=int, default=None, metavar="N",
                   help="【可选，默认不启用】用例结构质量分低于 N 时按失败退出（非 0）。"
                        "默认不卡：结构分是形式检查，可以注水刷高，作为默认门禁会鼓励刷分；"
                        "需要防「生成环节退化」的场景再显式开启")
    r.set_defaults(func=cmd_run)

    ps = sub.add_parser("perf-security", help="④ 性能与安全冒烟（独立于核心回归）")
    ps.add_argument("id")
    ps.add_argument("--only", choices=["perf", "security"], help="只跑其中一项")
    ps.add_argument("--users", type=int, help="并发数（覆盖 project.yaml 中的配置）")
    ps.add_argument("--iterations", type=int, help="每用户请求次数（覆盖配置）")
    ps.set_defaults(func=cmd_perf_security)

    df = sub.add_parser("defects", help="把失败的门禁项整理成可提交的缺陷草稿（不自动提单）")
    df.add_argument("id")
    df.add_argument("--json", action="store_true", help="输出 JSON 而非 Markdown")
    df.set_defaults(func=cmd_defects)

    wb = sub.add_parser("web", help="Web UI 冒烟（Playwright 声明式场景，独立于核心回归）")
    wb.add_argument("id")
    wb.add_argument("--only", help="只跑指定场景名（或标签）")
    wb.add_argument("--headed", action="store_true", help="有头模式（便于排查）")
    wb.add_argument("--browser", choices=["chromium", "firefox", "webkit"],
                    help="覆盖 web.yaml 中的浏览器")
    wb.set_defaults(func=cmd_web)

    ex = sub.add_parser("explore", help="⑥ AI 探索测试（基座自主探索；可降级阶段）")
    ex.add_argument("id")
    ex.add_argument("--mission", default=None, help="mission 文件（默认 <项目>/mission.yaml）")
    ex.add_argument("--max-steps", type=int, default=30, help="基座软步数上限（默认 30）")
    ex.add_argument("--timeout", type=float, default=None,
                    help="阶段墙钟上限（秒），默认取 STA_EXPLORE_TIMEOUT / 1800；0 表示不限")
    ex.add_argument("--headed", action="store_true", help="浏览器带界面跑（调试用）")
    ex.add_argument("--json", action="store_true", help="输出契约 JSON")
    ex.set_defaults(func=cmd_explore)

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
    # run_id 贯通：控制台触发时由 STA_RUN_ID 带入（同一次运行在 CLI 与 Web 侧同一条 id），
    # 直接命令行运行则自行生成。之后每一行日志都带它，跨进程也能串起来。
    adopt_env_run_id("cli")            # run_id 已进格式前缀，消息里不重复
    args = build_parser().parse_args()
    log.info("== 运行开始 cmd=%s（run_id 来源：%s）==", args.cmd,
             "STA_RUN_ID（由控制台传入）" if os.environ.get(RUN_ID_ENV) else "本次生成")
    try:
        args.func(args)
    except SystemExit:
        raise                       # 退出码语义由各子命令决定，不要在这里改写
    except Exception:
        # 未捕获异常必须留下带 run_id 的记录：否则崩溃只剩 traceback，
        # 而它混在业务输出里，正是"红了却定位不到"的来源。
        log.exception("命令 %s 执行失败（未捕获异常）", args.cmd)
        raise


if __name__ == "__main__":
    main()
