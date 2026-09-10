"""软件测试智能体 · 本地 Web 控制台（轻量版）。

用 Flask 把 project_manager.py 的既有能力包成可视化界面：
  - 项目总览：项目卡片 + 最近回归门禁状态
  - 新建项目向导：表单填写 -> 调 `project_manager.py create`
  - 一键执行：跑全流程 / 跑核心回归（后台执行，前端轮询实时日志）
  - 报告中心：直接打开项目报告 / 跨项目看板
  - Skills 管理：列出 agent-skills/ 下的技能，支持启用/停用

设计约定：
  - 不重复实现业务：所有执行都走子进程调用 project_manager.py（与 CLI 同源）。
  - 任务状态存内存（单机本地工具，重启即清空，符合轻量定位）。
  - .env 由 project_manager 的 main() 自行加载，Web 层不碰密钥。

启动：  .venv/Scripts/python web_console/app.py   （默认 http://127.0.0.1:8765）
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, render_template, send_file, request

# 打包模式（PyInstaller）识别：资源被解包到 sys._MEIPASS（只读），
# 运行数据（projects/、报告、可写配置）应落在 exe 所在目录（可读写）。
FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    EXE_DIR = Path(sys.executable).resolve().parent
    # 让随后 import 的 project_manager 使用同一可读写根（它靠 STA_ROOT 决定 ROOT）
    os.environ.setdefault("STA_ROOT", str(EXE_DIR))
    RES_DIR = Path(getattr(sys, "_MEIPASS", EXE_DIR))   # 模板 / project_manager.py / docs 等资源
    DATA_ROOT = EXE_DIR                                # projects / 报告 / 可写配置
else:
    EXE_DIR = Path(__file__).resolve().parent.parent
    RES_DIR = EXE_DIR
    DATA_ROOT = EXE_DIR

ROOT = RES_DIR  # agent-skills / docs / models 等资源根（frozen 下为 _MEIPASS）
sys.path.insert(0, str(RES_DIR))
import project_manager as pm  # noqa: E402  复用 load_projects / load_project
import web_console.run_store as run_store  # noqa: E402  任务历史落盘 SQLite
run_store.init_db(DATA_ROOT / "runs.db")

# frozen 模式下，把只读资源复制到可读写的 DATA_ROOT：
#  - agent-skills：技能开关要写 .disabled 标记，必须可写
#  - .env：密钥落本地（不随二进制发布真实凭据），首次运行写模板
if FROZEN:
    import shutil
    _sk_src = RES_DIR / "agent-skills"
    _sk_dst = DATA_ROOT / "agent-skills"
    if _sk_src.is_dir() and not _sk_dst.is_dir():
        shutil.copytree(_sk_src, _sk_dst)
    _env_dst = DATA_ROOT / ".env"
    if not _env_dst.is_file():
        _env_dst.write_text(
            "# 测试环境密钥（不入库；请按本项目 project.yaml 的变量名填写）\n"
            "APP_USERNAME=admin\nAPP_PASSWORD=macro123\n",
            encoding="utf-8")

app = Flask(__name__, template_folder=str(RES_DIR / "web_console" / "templates"))


def pm_script() -> str:
    """project_manager.py 的路径：打包后从资源目录取，否则用仓库里的源文件。"""
    return str(RES_DIR / "project_manager.py")

# ----------------------------------------------------------------------------
# 任务管理（内存态）
# ----------------------------------------------------------------------------
TASKS: Dict[str, Dict[str, Any]] = {}
_TASK_LOCK = threading.Lock()


def _spawn_task(kind: str, pid: str, args: List[str],
                scene: Optional[str] = None) -> str:
    """后台起一个 project_manager 子进程，输出实时落进 task['log']。"""
    tid = uuid.uuid4().hex[:12]
    task: Dict[str, Any] = {
        "id": tid, "kind": kind, "pid": pid, "status": "running",
        "started": datetime.now().strftime("%H:%M:%S"),
        "finished": None, "exit_code": None, "log": "",
        "created_at": int(time.time()),
        "started_ts": time.time(),
        "command": f"project_manager.py {' '.join(args)}",
        "scene": scene,
    }
    with _TASK_LOCK:
        TASKS[tid] = task
    # 落盘：即便进程随后被重启，历史仍可在 任务 页看到
    run_store.insert_run(task)

    def _run() -> None:
        try:
            proc = subprocess.run(
                [pm.python_exe(), pm_script(), *args],
                cwd=str(DATA_ROOT), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=1800,
            )
            task["log"] = (proc.stdout or "") + (proc.stderr or "")
            task["exit_code"] = proc.returncode
            # regression 命令门禁语义：0=通过（或仅 run 正常结束）
            task["status"] = "success" if proc.returncode == 0 else "failed"
        except Exception as e:  # pragma: no cover
            task["log"] = f"执行异常：{e}"
            task["status"] = "failed"
            task["exit_code"] = -1
        finally:
            task["finished"] = datetime.now().strftime("%H:%M:%S")
            task["finished_ts"] = time.time()
            run_store.update_run(tid, task["status"], task["finished"],
                                task["exit_code"], task["log"],
                                finished_ts=task["finished_ts"])
            # 回归类任务完成后留档一份结果快照，供「趋势」分析。
            # 只在真正产生了 regression.json 时写（执行异常时文件可能是旧的，
            # 但内容仍是本次落盘的最新结果，故以 status 为准，失败也留档以便看趋势断点）。
            if kind in ("run", "regression", "rerun") and pid and pid != "*":
                try:
                    reg = _read_regression(pm.PROJECTS_DIR / pid)
                    if reg:
                        run_store.insert_snapshot(pid, reg, trigger=kind,
                                                  scene=task.get("scene"))
                except Exception as e:  # 留档失败不影响任务本身
                    print(f"[snapshot] 写入失败（已忽略）：{e}")

    threading.Thread(target=_run, daemon=True).start()
    return tid


# ----------------------------------------------------------------------------
# 项目 API
# ----------------------------------------------------------------------------
def _read_regression(pdir: Path) -> Optional[Dict[str, Any]]:
    f = pdir / "artifacts" / "regression.json"
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


@app.get("/api/projects")
def api_projects() -> Any:
    # 停用的项目默认不展示（也不参与看板/门禁）；?include_disabled=1 时一并列出，
    # 否则界面上就再也找不到它、无法重新启用。
    include_disabled = request.args.get("include_disabled") in ("1", "true", "yes")
    items: List[Dict[str, Any]] = []
    for pdir in pm.load_projects(include_disabled=include_disabled):
        meta = pm.load_project(pdir.name)
        env = (meta.get("env") or {})
        items.append({
            "pid": meta.get("project_id", pdir.name),
            "name": meta.get("name", ""),
            "description": meta.get("description", ""),
            "owner": meta.get("owner", ""),
            "base_url": env.get("base_url", ""),
            "auth_type": (env.get("auth") or {}).get("type", "none"),
            "created_at": meta.get("created_at", ""),
            "has_report": (pdir / "artifacts" / "report.html").is_file(),
            "reg": _read_regression(pdir),
            "disabled": pm.is_disabled(pdir),
        })
    return jsonify({"projects": items})


# 可编辑文件白名单：只允许改这三类，避免 Web 层被当成任意文件写入口
EDITABLE_FILES = {
    "requirements": "requirements.md",
    "regression": "regression.yaml",
}


@app.get("/api/projects/<pid>/files")
def api_project_files(pid: str) -> Any:
    """读取项目详情：需求 / 回归配置 / 已生成用例（只读）。"""
    pdir = pm.PROJECTS_DIR / pid
    if not (pdir / "project.yaml").is_file():
        return jsonify({"ok": False, "error": f"项目 {pid} 不存在"}), 404
    out: Dict[str, Any] = {"pid": pid}
    for key, fname in EDITABLE_FILES.items():
        f = pdir / fname
        out[key] = f.read_text(encoding="utf-8") if f.is_file() else ""
    cases = pdir / "artifacts" / "cases.md"
    out["cases"] = cases.read_text(encoding="utf-8") if cases.is_file() else ""
    return jsonify({"ok": True, "files": out})


def _llm_available() -> bool:
    """是否配置了 LLM key（决定需求→用例能否走智能生成）。"""
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("GOOGLE_API_KEY"))


@app.get("/api/llm/status")
def api_llm_status() -> Any:
    """供前端提示「规则版 / 智能生成」是否可用。"""
    return jsonify({"ok": True, "available": _llm_available()})


@app.post("/api/projects/<pid>/cases")
def api_project_cases(pid: str) -> Any:
    """按当前需求文本立即生成测试用例（无需跑全流程）。

    body 可带 {requirements: "..."}：先落盘再生成，保证「所见即所生成」。
    无 LLM key 时走零依赖规则版。
    """
    pdir = pm.PROJECTS_DIR / pid
    if not (pdir / "project.yaml").is_file():
        return jsonify({"ok": False, "error": f"项目 {pid} 不存在"}), 404

    data = request.get_json(silent=True) or {}
    if isinstance(data.get("requirements"), str):
        (pdir / "requirements.md").write_text(data["requirements"], encoding="utf-8")

    req_file = pdir / "requirements.md"
    if not req_file.is_file():
        return jsonify({"ok": False, "error": "尚无 requirements.md，请先填写需求"}), 400

    try:
        sys.path.insert(0, str(ROOT / "extensions" / "requirements_to_cases"))
        import generate_cases as gc  # noqa: E402
        items = gc.parse_requirements(req_file.read_text(encoding="utf-8"))
        if not items:
            return jsonify({"ok": False,
                            "error": "未解析到需求条目，请用编号或 - / * 项目符号书写需求"}), 400
        cases = gc.gen_cases(items)
        out = pdir / "artifacts" / "cases.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(gc.to_markdown(cases, str(req_file)), encoding="utf-8")
    except Exception as e:  # 生成失败不能把服务打挂
        return jsonify({"ok": False, "error": f"生成失败：{e}"}), 500

    return jsonify({
        "ok": True,
        "requirements": len(items),
        "cases": len(cases),
        "markdown": out.read_text(encoding="utf-8"),
        "llm_available": _llm_available(),
    })


@app.post("/api/projects/<pid>/files")
def api_project_files_save(pid: str) -> Any:
    """保存需求 / 回归配置。保存前做轻量校验，避免写坏配置导致后续执行失败。"""
    pdir = pm.PROJECTS_DIR / pid
    if not (pdir / "project.yaml").is_file():
        return jsonify({"ok": False, "error": f"项目 {pid} 不存在"}), 404
    data = request.get_json(silent=True) or {}

    if "regression" in data:
        text = data["regression"] or ""
        try:
            import yaml
            loaded = yaml.safe_load(text)
            if not isinstance(loaded, dict) or "core_business" not in loaded:
                return jsonify({"ok": False,
                                "error": "regression.yaml 需包含顶层 core_business 列表"}), 400
        except ImportError:
            pass  # 无 PyYAML 时跳过语法校验（执行器本身也依赖它）
        except Exception as e:
            return jsonify({"ok": False, "error": f"YAML 语法错误：{e}"}), 400

    saved = []
    for key, fname in EDITABLE_FILES.items():
        if key in data:
            (pdir / fname).write_text(data[key] or "", encoding="utf-8")
            saved.append(fname)
    return jsonify({"ok": True, "saved": saved})


@app.post("/api/projects")
def api_create_project() -> Any:
    data = request.get_json(silent=True) or {}
    pid = (data.get("pid") or "").strip()
    if not re.fullmatch(r"[a-zA-Z0-9_-]{2,40}", pid):
        return jsonify({"ok": False, "error": "项目标识需为 2-40 位英文/数字/-/_"}), 400
    if (pm.PROJECTS_DIR / pid / "project.yaml").is_file():
        return jsonify({"ok": False, "error": f"项目 {pid} 已存在"}), 400

    req_md = (data.get("requirements") or "").strip()
    req_file: Optional[Path] = None
    if req_md:
        req_file = Path(tempfile.gettempdir()) / f"_sta_req_{uuid.uuid4().hex[:8]}.md"
        req_file.write_text(req_md, encoding="utf-8")

    args = ["create", "--id", pid,
            "--name", data.get("name") or pid,
            "--base-url", data.get("base_url") or "http://localhost:8080",
            "--owner", data.get("owner") or "未指定",
            "--description", data.get("description") or (data.get("name") or pid),
            "--auth-type", data.get("auth_type") or "none",
            "--login-url", data.get("login_url") or "/admin/login",
            "--user-env", data.get("user_env") or f"{pid.upper().replace('-', '_')}_USER",
            "--pass-env", data.get("pass_env") or f"{pid.upper().replace('-', '_')}_PASS"]
    if req_file:
        args += ["--requirements-file", str(req_file)]

    proc = subprocess.run(
        [pm.python_exe(), pm_script(), *args],
        cwd=str(DATA_ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if req_file and req_file.exists():
        req_file.unlink()
    if proc.returncode != 0:
        return jsonify({"ok": False,
                        "error": (proc.stderr or proc.stdout or "创建失败")[-400:]}), 400
    return jsonify({"ok": True, "pid": pid,
                    "log": (proc.stdout or "")[-600:]})


@app.post("/api/projects/<pid>/disable")
def api_project_disable(pid: str) -> Any:
    """停用 / 启用项目。停用后不出现在列表与看板，历史数据保留。"""
    pdir = pm.PROJECTS_DIR / pid
    if not (pdir / "project.yaml").is_file():
        return jsonify({"ok": False, "error": f"项目 {pid} 不存在"}), 404
    body = request.get_json(silent=True) or {}
    disabled = bool(body.get("disabled", True))
    pm.set_disabled(pdir, disabled)
    return jsonify({"ok": True, "pid": pid, "disabled": disabled})


@app.delete("/api/projects/<pid>")
def api_project_delete(pid: str) -> Any:
    """删除项目：移除目录 + 清空其任务历史与回归快照。需显式 ?confirm=1。"""
    if request.args.get("confirm") not in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "删除需带 confirm=1"}), 400
    pdir = pm.PROJECTS_DIR / pid
    if not (pdir / "project.yaml").is_file():
        return jsonify({"ok": False, "error": f"项目 {pid} 不存在"}), 404
    try:
        shutil.rmtree(pdir)
    except Exception as e:  # pragma: no cover
        return jsonify({"ok": False, "error": f"删除目录失败：{e}"}), 500
    try:
        run_store.delete_project(pid)
    except Exception as e:  # 留档清理失败不影响目录已删除的事实
        print(f"[delete] 清理历史失败（已忽略）：{e}")
    return jsonify({"ok": True, "pid": pid})


@app.post("/api/projects/<pid>/run")
def api_run(pid: str) -> Any:
    if not (pm.PROJECTS_DIR / pid / "project.yaml").is_file():
        return jsonify({"ok": False, "error": f"项目 {pid} 不存在"}), 404
    tid = _spawn_task("run", pid, ["run", pid])
    return jsonify({"ok": True, "task_id": tid})


@app.post("/api/projects/<pid>/regression")
def api_regression(pid: str) -> Any:
    if not (pm.PROJECTS_DIR / pid / "project.yaml").is_file():
        return jsonify({"ok": False, "error": f"项目 {pid} 不存在"}), 404
    tid = _spawn_task("regression", pid, ["regression", pid])
    return jsonify({"ok": True, "task_id": tid})


@app.post("/api/projects/<pid>/rerun_scene")
def api_rerun_scene(pid: str) -> Any:
    """重跑单个核心场景（结果合并回 regression.json，不覆盖完整报告）。"""
    if not (pm.PROJECTS_DIR / pid / "project.yaml").is_file():
        return jsonify({"ok": False, "error": f"项目 {pid} 不存在"}), 404
    body = request.get_json(silent=True) or {}
    scene = str(body.get("name") or "").strip()
    if not scene:
        return jsonify({"ok": False, "error": "缺少场景名"}), 400
    tid = _spawn_task("rerun", pid, ["rerun", pid, "--scene", scene], scene=scene)
    return jsonify({"ok": True, "task_id": tid})


@app.get("/api/projects/<pid>/trends")
def api_project_trends(pid: str) -> Any:
    """回归趋势：历史快照序列 + 每个场景的结果序列（供趋势图/场景历史条）。"""
    try:
        limit = int(request.args.get("limit", 60))
    except ValueError:
        limit = 60
    # days=7/30 之类的时间范围；留空或 0 表示全部。
    # 过滤交给 SQL（run_store.list_snapshots 的 since），避免"先截断再筛"漏数据。
    since: Optional[int] = None
    days = request.args.get("days")
    if days:
        try:
            d = float(days)
            if d > 0:
                since = int(time.time() - d * 86400)
        except ValueError:
            since = None
    snaps = run_store.list_snapshots(pid, limit=max(1, min(limit, 500)), since=since)
    # 场景级历史：以最近快照里的场景名为准，逐场景抽历史序列
    names: List[str] = []
    for s in reversed(snaps):
        for r in s.get("results") or []:
            n = str(r.get("name", ""))
            if n and n not in names:
                names.append(n)
    # 场景历史留长一些：前端要据此统计"哪些场景反复失败"
    scenes = {n: run_store.scene_history(snaps, n, limit=60) for n in names}
    return jsonify({"ok": True, "pid": pid, "snapshots": [
        {k: v for k, v in s.items() if k != "results"} for s in snaps
    ], "scenes": scenes})


@app.get("/api/projects/<pid>/scenes")
def api_project_scenes(pid: str) -> Any:
    """列出项目 regression.yaml 中声明的核心场景名（供单场景重跑下拉）。"""
    reg_file = pm.PROJECTS_DIR / pid / "regression.yaml"
    if not reg_file.is_file():
        return jsonify({"ok": False, "error": "该项目无 regression.yaml"}), 404
    try:
        import yaml
        reg = yaml.safe_load(reg_file.read_text(encoding="utf-8")) or {}
    except Exception as e:  # pragma: no cover
        return jsonify({"ok": False, "error": str(e)}), 500
    items = reg.get("core_business", []) or []
    return jsonify({"ok": True, "scenes": [
        {"name": it.get("name", "?"), "type": it.get("type", "api_smoke")}
        for it in items
    ]})


@app.post("/api/dashboard")
def api_dashboard() -> Any:
    tid = _spawn_task("dashboard", "*", ["dashboard"])
    return jsonify({"ok": True, "task_id": tid})


@app.get("/api/tasks/<tid>")
def api_task(tid: str) -> Any:
    task = TASKS.get(tid)
    if not task:
        task = run_store.get_run(tid)  # 重启后仍可回看历史任务
    if not task:
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    return jsonify({"ok": True, "task": task})


@app.get("/api/tasks")
def api_tasks() -> Any:
    """合并历史（SQLite 持久化）与内存态（正在跑的实时日志）。"""
    with _TASK_LOCK:
        live = list(TASKS.values())
    persisted = {t["id"]: t for t in run_store.list_runs(limit=200, preview=True)}
    merged = dict(persisted)
    for t in live:
        merged[t["id"]] = t  # 内存态优先（日志更实时）
    tasks = sorted(
        merged.values(),
        key=lambda t: (t.get("created_at") or 0, t.get("started") or ""),
        reverse=True,
    )
    return jsonify({"tasks": tasks[:30]})


# ----------------------------------------------------------------------------
# Skills API（agent-skills/ 目录 + .disabled 开关）
# ----------------------------------------------------------------------------
def _skills_root() -> Path:
    return (DATA_ROOT if FROZEN else ROOT) / "agent-skills"


def _parse_skill_md(path: Path) -> Dict[str, Any]:
    """解析 SKILL.md frontmatter 里的 name/description（宽松解析，容忍缺字段）。"""
    name, desc = path.parent.name, ""
    try:
        text = path.read_text(encoding="utf-8")
        m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
        if m:
            fm = m.group(1)
            nm = re.search(r"^name:\s*(.+)$", fm, re.M)
            dm = re.search(r"^description:\s*(.+)$", fm, re.M | re.S)
            if nm:
                name = nm.group(1).strip().strip('"').strip("'")
            if dm:
                desc = dm.group(1).strip().strip('"').strip("'").split("\n")[0][:120]
    except Exception:
        pass
    return {"name": name, "description": desc}


@app.get("/api/skills")
def api_skills() -> Any:
    root = _skills_root()
    skills: List[Dict[str, Any]] = []
    if root.is_dir():
        for d in sorted(root.iterdir()):
            sm = d / "SKILL.md"
            if not sm.is_file():
                continue
            info = _parse_skill_md(sm)
            info.update({
                "dir": d.name,
                "enabled": not (d / ".disabled").is_file(),
            })
            skills.append(info)
    return jsonify({"skills": skills, "root": str(root)})


@app.post("/api/skills/<dirname>/toggle")
def api_skill_toggle(dirname: str) -> Any:
    d = _skills_root() / dirname
    if not (d / "SKILL.md").is_file():
        return jsonify({"ok": False, "error": "技能不存在"}), 404
    marker = d / ".disabled"
    if marker.is_file():
        marker.unlink()
        enabled = True
    else:
        marker.write_text("", encoding="utf-8")
        enabled = False
    return jsonify({"ok": True, "enabled": enabled})


# ----------------------------------------------------------------------------
# 资料库 API（docs/ 目录下的 Markdown 文档）
# ----------------------------------------------------------------------------
def _docs_root() -> Path:
    return ROOT / "docs"


@app.get("/api/docs")
def api_docs_list() -> Any:
    root = _docs_root()
    docs: List[Dict[str, Any]] = []
    if root.is_dir():
        for f in sorted(root.glob("*.md")):
            st = f.stat()
            docs.append({
                "name": f.name,
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
    return jsonify({"docs": docs, "root": str(root)})


@app.get("/api/docs/<name>")
def api_doc_read(name: str) -> Any:
    # 防目录穿越：只允许 docs/*.md
    if not re.fullmatch(r"[A-Za-z0-9_\-\u4e00-\u9fa5]+\.md", name):
        return jsonify({"ok": False, "error": "非法的文档名"}), 400
    f = _docs_root() / name
    if not f.is_file():
        return jsonify({"ok": False, "error": "文档不存在"}), 404
    return jsonify({"ok": True, "name": name,
                    "content": f.read_text(encoding="utf-8")})


# ----------------------------------------------------------------------------
# 自动化 API（各项目的核心回归场景）
# ----------------------------------------------------------------------------
def _load_regression_scenarios(pdir: Path) -> List[Dict[str, Any]]:
    ry = pdir / "regression.yaml"
    if not ry.is_file():
        return []
    try:
        import yaml  # noqa
    except ImportError:
        return []
    try:
        data = yaml.safe_load(ry.read_text(encoding="utf-8")) or {}
        items = data.get("core_business", []) or []
        # 只保留前端展示需要的字段，避免把 {{密码}} 这类占位符细节全量下发
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            out.append({
                "name": it.get("name", ""),
                "type": it.get("type", ""),
                "method": it.get("method", ""),
                "path": it.get("path", ""),
                "marker": it.get("marker", ""),
                "target": it.get("target", ""),
            })
        return out
    except Exception:
        return []


@app.get("/api/automation")
def api_automation() -> Any:
    items: List[Dict[str, Any]] = []
    for pdir in pm.load_projects():
        meta = pm.load_project(pdir.name)
        reg = _read_regression(pdir)
        items.append({
            "pid": meta.get("project_id", pdir.name),
            "name": meta.get("name", pdir.name),
            "base_url": (meta.get("env") or {}).get("base_url", ""),
            "scenarios": _load_regression_scenarios(pdir),
            "reg": reg,
        })
    return jsonify({"automations": items})


# ----------------------------------------------------------------------------
# 模型维护 API（默认 provider/model 配置 + 密钥状态）
# ----------------------------------------------------------------------------
MODELS_FILE = DATA_ROOT / "models_config.json"


@app.get("/api/models")
def api_models_get() -> Any:
    cfg: Dict[str, Any] = {}
    if MODELS_FILE.is_file():
        try:
            cfg = json.loads(MODELS_FILE.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    return jsonify({
        "config": cfg,
        "keys": {
            "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "google": bool(os.environ.get("GOOGLE_API_KEY")),
        },
    })


@app.post("/api/models")
def api_models_save() -> Any:
    data = request.get_json(silent=True) or {}
    cfg = {
        "provider": str(data.get("provider", "anthropic")),
        "model": str(data.get("model", "")).strip(),
        "base_url": str(data.get("base_url", "")).strip(),
    }
    MODELS_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    return jsonify({"ok": True, "config": cfg})


# ----------------------------------------------------------------------------
# 报告 / 页面
# ----------------------------------------------------------------------------
@app.get("/")
def index() -> Any:
    return render_template("index.html")


@app.get("/favicon.ico")
def favicon() -> Any:
    return "", 204  # 避免每次打开页面都刷一条 404


@app.get("/reports/<pid>/report.html")
def project_report(pid: str) -> Any:
    f = pm.PROJECTS_DIR / pid / "artifacts" / "report.html"
    if not f.is_file():
        return "报告尚未生成，请先执行全流程测试", 404
    return send_file(f)


@app.get("/reports/dashboard.html")
def dashboard_report() -> Any:
    f = DATA_ROOT / "projects_dashboard.html"
    if not f.is_file():
        return "看板尚未生成，请点击右上角“刷新看板”", 404
    return send_file(f)


if __name__ == "__main__":
    print("=" * 56)
    print("  软件测试智能体 · Web 控制台")
    print("  http://127.0.0.1:8765")
    print("=" * 56)
    app.run(host="127.0.0.1", port=8765, debug=False)
