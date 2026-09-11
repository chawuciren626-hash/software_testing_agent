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
sys.path.insert(0, str(RES_DIR / "extensions" / "reporting"))  # gate_notify
import project_manager as pm  # noqa: E402  复用 load_projects / load_project
import web_console.run_store as run_store  # noqa: E402  任务历史落盘 SQLite
run_store.init_db(DATA_ROOT / "runs.db")

# 启动即把根目录 .env 载入进程环境，使 Web 端的 LLM 配置（LLM_* 变量）对
# extensions/requirements_to_cases 的 in-process 调用（需求→用例智能生成）即时可见。
pm._load_dotenv()

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
# 改了 templates/index.html 后刷新页面即生效。
# 否则 debug=False 时 Jinja 会缓存模板，出现"明明改了前端却没变化"的假象——
# 排查时极容易被带偏（以为是浏览器缓存，其实是服务端模板缓存）。
app.jinja_env.auto_reload = True
app.config["TEMPLATES_AUTO_RELOAD"] = True

# 访问鉴权（**默认关闭**）：配了 .env 的 STA_CONSOLE_TOKEN 才生效。
# 不配时行为与之前完全一致 —— 本地单人用不需要摩擦，但端口一旦暴露到局域网
# 它就是个"任何人都能触发测试任务"的入口，所以做成可开启。
import web_console.auth as console_auth  # noqa: E402
console_auth.install(app)


def pm_script() -> str:
    """project_manager.py 的路径：打包后从资源目录取，否则用仓库里的源文件。"""
    return str(RES_DIR / "project_manager.py")

# ----------------------------------------------------------------------------
# 任务管理（内存态）
# ----------------------------------------------------------------------------
TASKS: Dict[str, Dict[str, Any]] = {}
_TASK_LOCK = threading.Lock()


def _spawn_task(kind: str, pid: str, args: List[str],
                scene: Optional[str] = None,
                extra_args: Optional[List[str]] = None) -> str:
    """后台起一个 project_manager 子进程，输出**逐行实时**落进 task['log']。

    extra_args：追加到命令末尾的开关（如 --llm / --agentic）。
    用 Popen 逐行读取，前端轮询 /api/tasks/<tid> 即可看到日志**实时增长**，
    而不是等整个流程跑完才一次性出现。
    """
    full_args = list(args) + list(extra_args or [])
    tid = uuid.uuid4().hex[:12]
    task: Dict[str, Any] = {
        "id": tid, "kind": kind, "pid": pid, "status": "running",
        "started": datetime.now().strftime("%H:%M:%S"),
        "finished": None, "exit_code": None, "log": "",
        "created_at": int(time.time()),
        "started_ts": time.time(),
        "command": f"project_manager.py {' '.join(full_args)}",
        "scene": scene,
    }
    with _TASK_LOCK:
        TASKS[tid] = task
    # 落盘：即便进程随后被重启，历史仍可在 任务 页看到
    run_store.insert_run(task)

    def _run() -> None:
        proc: Optional[subprocess.Popen] = None
        timer: Optional[threading.Timer] = None
        try:
            proc = subprocess.Popen(
                [pm.python_exe(), pm_script(), *full_args],
                cwd=str(DATA_ROOT),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
            # 硬超时保护（与旧实现一致的 30 分钟）：到点直接 kill，避免僵尸进程
            timer = threading.Timer(1800, proc.kill)
            timer.daemon = True
            timer.start()
            buf: List[str] = []
            assert proc.stdout is not None
            for line in proc.stdout:
                buf.append(line)
                task["log"] = "".join(buf)  # 增量可见：前端轮询即实时刷新
            proc.stdout.close()
            rc = proc.wait()
            task["exit_code"] = rc
            # regression 命令门禁语义：0=通过（或仅 run 正常结束）
            task["status"] = "success" if rc == 0 else "failed"
        except Exception as e:  # pragma: no cover
            task["log"] = (task.get("log") or "") + f"\n执行异常：{e}"
            task["status"] = "failed"
            task["exit_code"] = -1
        finally:
            if timer is not None:
                timer.cancel()
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


def _read_perf_security(pdir: Path) -> Optional[Dict[str, Any]]:
    """读取 ④ 性能与安全冒烟结果（未执行返回 None）。"""
    f = pdir / "artifacts" / "perf_security.json"
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _read_web(pdir: Path) -> Optional[Dict[str, Any]]:
    """读取 ⑤ Web UI 冒烟结果（未执行返回 None）。"""
    f = pdir / "artifacts" / "web.json"
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
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
            "perf_security": _read_perf_security(pdir),
            "web": _read_web(pdir),
            "quality": pm._read_quality(pdir),
            "defects": pm._read_defects(pdir),
            "diff": pm._read_diff(pdir),
            "disabled": pm.is_disabled(pdir),
        })
    return jsonify({"projects": items})


# 可编辑文件白名单：只允许改这三类，避免 Web 层被当成任意文件写入口
EDITABLE_FILES = {
    "requirements": "requirements.md",
    "regression": "regression.yaml",
    "web": "web.yaml",
    # knowledge.md 是**人工维护**的长期记忆，本来就该能在控制台里写，
    # 不能改就意味着用户得去翻磁盘文件 —— 那它很快就再也无人更新。
    "knowledge": "knowledge.md",
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
    defects_md = pdir / "artifacts" / "defects.md"
    out["defects"] = defects_md.read_text(encoding="utf-8") if defects_md.is_file() else ""
    ps_file = pdir / "artifacts" / "perf_security.json"
    perf_sec: Optional[Dict[str, Any]] = None
    if ps_file.is_file():
        try:
            perf_sec = json.loads(ps_file.read_text(encoding="utf-8"))
        except Exception:
            perf_sec = None
    return jsonify({"ok": True, "files": out, "run_meta": pm._read_run_meta(pdir),
                    "perf_security": perf_sec, "web": _read_web(pdir),
                    "quality": pm._read_quality(pdir),
                    "defects": pm._read_defects(pdir),
                    "diff": pm._read_diff(pdir)})


def _llm_available() -> bool:
    """LLM 增强是否可用：取决于当前 provider 是否已配置 key 或本地 base。

    见 generate_cases.llm_configured()。
    """
    try:
        sys.path.insert(0, str(ROOT / "extensions" / "requirements_to_cases"))
        import generate_cases as gc  # noqa: E402
    except Exception:
        return False
    return gc.llm_configured()


@app.get("/api/llm/status")
def api_llm_status() -> Any:
    """供前端提示「规则版 / 智能生成」是否可用。"""
    return jsonify({"ok": True, "available": _llm_available()})


@app.get("/api/projects/<pid>/lessons")
def api_project_lessons(pid: str) -> Any:
    """读取项目的情景记忆（P2 自动生成的历史失败根因 / 重点覆盖清单）。"""
    pdir = pm.PROJECTS_DIR / pid
    if not (pdir / "project.yaml").is_file():
        return jsonify({"ok": False, "error": f"项目 {pid} 不存在"}), 404
    lp = pdir / "lessons.md"
    content = lp.read_text(encoding="utf-8") if lp.is_file() else ""
    return jsonify({"ok": True, "has": bool(content), "content": content})


@app.get("/api/projects/<pid>/knowledge")
def api_project_knowledge(pid: str) -> Any:
    """项目知识库（S2 长期记忆，人工维护）：内容 + **本次会命中哪些段落**。

    为什么把检索解释一起回传：写好了 knowledge.md 却不知道它有没有被用上，
    是最容易让人放弃维护它的情况。把命中词摆在界面上，用户立刻能判断
    "该在标题里多写几个核心词"，而不是靠猜。
    """
    pdir = pm.PROJECTS_DIR / pid
    if not (pdir / "project.yaml").is_file():
        return jsonify({"ok": False, "error": f"项目 {pid} 不存在"}), 404
    sys.path.insert(0, str(ROOT / "extensions" / "memory"))
    import knowledge as kn  # noqa: E402

    content = kn.load_knowledge(pdir) or ""
    if not content.strip():
        return jsonify({"ok": True, "has": False, "content": "",
                        "template": kn.TEMPLATE, "picked": [],
                        "total_sections": 0, "query_terms": [],
                        "reason": f"尚无 {kn.KNOWLEDGE_FILE}（可直接在控制台填写）"})
    rf = pdir / "requirements.md"
    req_text = rf.read_text(encoding="utf-8") if rf.is_file() else ""
    info = kn.explain(pdir, req_text)
    info.update({"ok": True, "has": True, "content": content, "template": "",
                 "reason": ""})
    return jsonify(info)


@app.get("/api/projects/<pid>/quality")
def api_project_quality(pid: str) -> Any:
    """用例结构质量分（与 CLI / 报告同源，读 `artifacts/quality.json`）。

    额外回传 `text`（纯文本摘要）与 `labels` / `hints`（维度中文名），
    前端就不必自己维护一份维度文案 —— 两处各写一份迟早会漂移。
    """
    pdir = pm.PROJECTS_DIR / pid
    if not (pdir / "project.yaml").is_file():
        return jsonify({"ok": False, "error": f"项目 {pid} 不存在"}), 404
    q = pm._read_quality(pdir)
    if not q:
        return jsonify({"ok": True, "has": False, "total": None, "history": []})
    try:
        sys.path.insert(0, str(ROOT / "extensions" / "requirements_to_cases"))
        import case_quality as cq  # noqa: E402
        labels, hints, dims_meta, text = cq.LABELS, cq.HINTS, cq.DIMENSIONS, cq.render_text(q)
    except Exception:      # 元信息拿不到也要能展示分数，别让整个面板挂掉
        labels, hints, dims_meta, text = {}, {}, [], ""
    payload: Dict[str, Any] = {"ok": True, "has": True, "text": text,
                               "labels": labels, "hints": hints,
                               "dims_order": list(dims_meta)}
    payload.update(q)      # total / dims / delta / history / notes / counts / scored_at
    return jsonify(payload)


@app.get("/api/projects/<pid>/diff")
def api_project_diff(pid: str) -> Any:
    """失败项新旧对比（与报告同源，读 `artifacts/diff.json`）。

    额外回传 `labels`（判定中文名）与 `text`（纯文本摘要），前端不另抄一份文案。
    """
    pdir = pm.PROJECTS_DIR / pid
    if not (pdir / "project.yaml").is_file():
        return jsonify({"ok": False, "error": f"项目 {pid} 不存在"}), 404
    d = pm._read_diff(pdir)
    if not d:
        return jsonify({"ok": True, "has": False, "items": [], "counts": {},
                        "baseline": {"available": False,
                                     "reason": "尚未生成（跑一次「全流程」或「核心回归」后自动计算）"}})
    labels, descs, text = {}, {}, ""
    try:
        sys.path.insert(0, str(ROOT / "extensions" / "reporting"))
        import trend_diff as td  # noqa: E402
        labels, descs = td.STATUS_LABEL, td.STATUS_DESC
        text = td.render_text(d)
    except Exception:      # 元信息拿不到也要能展示结论，别让整个面板挂掉
        pass
    payload: Dict[str, Any] = {"ok": True, "has": True, "labels": labels,
                               "descs": descs, "text": text}
    payload.update(d)      # baseline / items / counts / notes / headline / focus_new
    return jsonify(payload)


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
    use_llm = bool(data.get("llm"))
    use_agentic = bool(data.get("agentic")) and use_llm
    if isinstance(data.get("requirements"), str):
        (pdir / "requirements.md").write_text(data["requirements"], encoding="utf-8")

    req_file = pdir / "requirements.md"
    if not req_file.is_file():
        return jsonify({"ok": False, "error": "尚无 requirements.md，请先填写需求"}), 400

    # 情景记忆：注入项目历史易错点（若已生成 lessons.md）
    extra_context = None
    try:
        sys.path.insert(0, str(ROOT / "extensions" / "memory"))
        import lessons as ls  # noqa: E402
        extra_context = ls.to_inject_prompt(pdir)
    except Exception:
        extra_context = None

    try:
        sys.path.insert(0, str(ROOT / "extensions" / "requirements_to_cases"))
        import generate_cases as gc  # noqa: E402
        text = req_file.read_text(encoding="utf-8")
        md = gc.generate_from_text(text, use_llm=use_llm, agentic=use_agentic,
                                   source=str(req_file), extra_context=extra_context)
        items = gc.parse_requirements(text)
        out = pdir / "artifacts" / "cases.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
    except Exception as e:  # 生成失败不能把服务打挂
        return jsonify({"ok": False, "error": f"生成失败：{e}"}), 500

    _, case_rows = pm._parse_cases(md)

    # 与 CLI `run` 同源：生成用例就打分。
    # 不做的话，Web 生成完用例后「用例质量」页还是上一次 run 的分数 ——
    # 两处口径不一致比没有口径更糟，而且趋势会在这里断档。
    _mode = ("智能体多步自审编排" if (use_llm and use_agentic)
             else ("LLM 增强" if use_llm else "规则版"))
    quality: Optional[Dict[str, Any]] = None
    try:
        quality = pm._step_quality(pdir, md, requirement_count=len(items), mode=_mode)
    except Exception as e:      # 打分失败不能让生成用例这个主操作失败
        print(f"[质量分] 计算失败（不影响生成）：{e}")
    try:
        pm._merge_run_meta(pdir, mode=_mode, use_llm=use_llm, agentic=use_agentic,
                           lessons_injected=bool(extra_context),
                           requirements=len(items), cases=len(case_rows),
                           **({"quality": quality["total"]}
                              if quality and quality.get("total") is not None else {}))
    except Exception as e:
        print(f"[run_meta] 写入失败（不影响生成）：{e}")

    return jsonify({
        "ok": True,
        "requirements": len(items),
        "cases": len(case_rows),
        "markdown": out.read_text(encoding="utf-8"),
        "llm_used": use_llm and _llm_available(),
        "llm_available": _llm_available(),
        "agentic_used": use_agentic and _llm_available(),
        "lessons_injected": bool(extra_context),
        "quality": quality,
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
    """跑全流程。body 可带 {llm: true, agentic: true} 以启用 LLM 增强 / 多步自审编排。"""
    if not (pm.PROJECTS_DIR / pid / "project.yaml").is_file():
        return jsonify({"ok": False, "error": f"项目 {pid} 不存在"}), 404
    body = request.get_json(silent=True) or {}
    use_llm = bool(body.get("llm")) and _llm_available()
    use_agentic = bool(body.get("agentic")) and use_llm
    extra: List[str] = []
    if use_llm:
        extra.append("--llm")
    if use_agentic:
        extra.append("--agentic")
    tid = _spawn_task("run", pid, ["run", pid], extra_args=extra)
    return jsonify({"ok": True, "task_id": tid, "llm": use_llm, "agentic": use_agentic})


@app.post("/api/projects/<pid>/regression")
def api_regression(pid: str) -> Any:
    if not (pm.PROJECTS_DIR / pid / "project.yaml").is_file():
        return jsonify({"ok": False, "error": f"项目 {pid} 不存在"}), 404
    tid = _spawn_task("regression", pid, ["regression", pid])
    return jsonify({"ok": True, "task_id": tid})


@app.post("/api/projects/<pid>/perf-security")
def api_perf_security(pid: str) -> Any:
    """④ 性能与安全冒烟。

    body 可选：{only: "perf"|"security", users: int, iterations: int}。
    门禁语义与回归一致：环境不可达 → 全 SKIP → 退出码非零（防 CI 假绿）。
    """
    if not (pm.PROJECTS_DIR / pid / "project.yaml").is_file():
        return jsonify({"ok": False, "error": f"项目 {pid} 不存在"}), 404
    body = request.get_json(silent=True) or {}
    extra: List[str] = []
    only = str(body.get("only") or "").strip()
    if only in ("perf", "security"):
        extra += ["--only", only]
    for key, flag in (("users", "--users"), ("iterations", "--iterations")):
        try:
            v = int(body.get(key))
        except (TypeError, ValueError):
            continue
        if v > 0:
            extra += [flag, str(v)]
    tid = _spawn_task("perf-security", pid, ["perf-security", pid], extra_args=extra)
    return jsonify({"ok": True, "task_id": tid})


@app.post("/api/projects/<pid>/web")
def api_web(pid: str) -> Any:
    """⑤ Web UI 冒烟（Playwright 声明式场景）。

    body 可选：{only: "场景名或标签", headed: bool, browser: "chromium|firefox|webkit"}。
    门禁语义与回归一致：环境不可达 / 浏览器起不来 → 全 SKIP → 退出码非零（防 CI 假绿）。
    """
    pdir = pm.PROJECTS_DIR / pid
    if not (pdir / "project.yaml").is_file():
        return jsonify({"ok": False, "error": f"项目 {pid} 不存在"}), 404
    if not (pdir / "web.yaml").is_file():
        return jsonify({"ok": False,
                        "error": "缺少 web.yaml：请先在「项目详情 → Web 场景」中声明场景"}), 400
    body = request.get_json(silent=True) or {}
    extra: List[str] = []
    only = str(body.get("only") or "").strip()
    if only:
        extra += ["--only", only]
    if body.get("headed") in (True, "1", "true", "yes"):
        extra += ["--headed"]
    browser = str(body.get("browser") or "").strip().lower()
    if browser in ("chromium", "firefox", "webkit"):
        extra += ["--browser", browser]
    tid = _spawn_task("web", pid, ["web", pid], extra_args=extra)
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


@app.get("/api/gates")
def api_gates() -> Any:
    """跨项目门禁总览：三态结论（通过 / 未通过 / 未执行）。

    与 CLI 的 `gate_notify.py` 同源同口径 —— 避免"控制台说绿、CLI 说红"两套结论。
    `?project=<pid>` 可只看指定项目（CI 用法：只看本次参与门禁的那个）。
    `?include_disabled=1` 与 `/api/projects` 同款；默认跳过已停用项目，
    否则停用项目会带着历史失败产物把门禁拖红，而项目页又看不到它 → 无法解释的幽灵红。
    """
    try:
        import gate_notify  # noqa: E402  延迟导入：仅在需要时解析
    except Exception as e:  # 依赖缺失不该让整个控制台挂掉
        return jsonify({"ok": False, "error": f"门禁摘要模块不可用：{e}"}), 500
    only = request.args.get("project")
    include_disabled = request.args.get("include_disabled") in ("1", "true", "yes")
    rows = gate_notify.collect_gates(pm.PROJECTS_DIR, only=[only] if only else None,
                                     include_disabled=include_disabled)
    disabled = [] if include_disabled else gate_notify.disabled_projects(pm.PROJECTS_DIR)
    return jsonify({
        "ok": True,
        "rows": rows,
        "disabled": disabled,
        "summary": gate_notify.summarize(rows, disabled=len(disabled)),
        "text": gate_notify.render_text(rows, disabled=disabled),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


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


def _load_web_scenarios(pdir: Path) -> List[Dict[str, Any]]:
    """读取 web.yaml 里的 Web 场景清单（只给前端展示必要字段）。

    只暴露场景名/标签/步骤数，不外泄选择器细节与 {{username}} 之类占位符。
    """
    wy = pdir / "web.yaml"
    if not wy.is_file():
        return []
    try:
        import yaml  # noqa
    except ImportError:
        return []
    try:
        data = yaml.safe_load(wy.read_text(encoding="utf-8")) or {}
        w = data.get("web", data) or {}
        out: List[Dict[str, Any]] = []
        for it in (w.get("scenarios") or []):
            if not isinstance(it, dict):
                continue
            steps = it.get("steps") or []
            n_assert = sum(1 for s in steps if isinstance(s, dict) and
                           any(str(k).startswith("expect_") for k in s))
            out.append({
                "name": it.get("name", ""),
                "tags": [str(t) for t in (it.get("tags") or [])],
                "steps": len(steps) if isinstance(steps, list) else 0,
                "assertions": n_assert,
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
            "web_scenarios": _load_web_scenarios(pdir),
            "web": _read_web(pdir),
            "has_web_yaml": (pdir / "web.yaml").is_file(),
        })
    return jsonify({"automations": items})


# ----------------------------------------------------------------------------
# 模型维护 API（默认 provider/model 配置 + 密钥状态）
# ----------------------------------------------------------------------------
MODELS_FILE = DATA_ROOT / "models_config.json"


def _update_env_file(path: Path, updates: Dict[str, str]) -> List[str]:
    """把给定键值对写回 .env：保留注释与无关行；已存在则原地替换，不存在则追加。

    值为空字符串的键会被跳过（不覆盖已有值）。返回实际写入的键列表。
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    seen: set[str] = set()
    out: List[str] = []
    for line in lines:
        m = re.match(r"^\s*([A-Za-z0-9_]+)\s*=", line)
        if m and m.group(1) in updates and updates[m.group(1)]:
            out.append(f'{m.group(1)}="{updates[m.group(1)]}"')
            seen.add(m.group(1))
        else:
            out.append(line)
    for k, v in updates.items():
        if k not in seen and v:
            out.append(f'{k}="{v}"')
    path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    return [k for k, v in updates.items() if k in seen or (k not in seen and v)]


@app.get("/api/models")
def api_models_get() -> Any:
    """返回当前 LLM 配置（以 .env 中的 LLM_* 为唯一来源），并掩码展示 key。"""
    provider = os.environ.get("LLM_PROVIDER", "openai")
    model = os.environ.get("LLM_MODEL", "")
    base_url = os.environ.get("LLM_BASE_URL", "")
    api_key = os.environ.get("LLM_API_KEY", "")
    try:
        sys.path.insert(0, str(ROOT / "extensions" / "requirements_to_cases"))
        import generate_cases as gc  # noqa: E402
        configured = gc.llm_configured()
    except Exception:
        configured = bool(api_key) or any(h in base_url for h in ("localhost", "127.0.0.1"))
    return jsonify({
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "api_key_masked": ("***" + api_key[-4:]) if api_key else "",
        "has_api_key": bool(api_key),
        "keys": {
            "openai": bool(api_key),
            "gemini": bool(os.environ.get("GOOGLE_API_KEY")),
            "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "llm_configured": configured,
        },
    })


@app.post("/api/models")
def api_models_save() -> Any:
    """在线保存 LLM 配置，写回根目录 .env（LLM_PROVIDER/LLM_MODEL/LLM_BASE_URL/LLM_API_KEY）。

    不入库、不写孤立 JSON；key 留空表示不修改已有密钥。
    """
    data = request.get_json(silent=True) or {}
    field_map = {
        "provider": "LLM_PROVIDER",
        "model": "LLM_MODEL",
        "base_url": "LLM_BASE_URL",
        "api_key": "LLM_API_KEY",
    }
    updates = {}
    for field, env_key in field_map.items():
        val = data.get(field)
        if isinstance(val, str) and val.strip():
            updates[env_key] = val.strip()
    if not updates:
        return jsonify({"ok": False, "error": "没有可保存的字段"})
    env_path = ROOT / ".env"
    try:
        _update_env_file(env_path, updates)
    except Exception as e:
        return jsonify({"ok": False, "error": f"写入 .env 失败：{e}"})
    # 同步到当前进程环境，使后续调用立即生效
    for k, v in updates.items():
        os.environ[k] = v
    return jsonify({"ok": True, "updated": list(updates.keys())})


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


# ----------------------------------------------------------------------------
# 失败证据静态服务（Web 冒烟截图 / 可复现脚本）
# ----------------------------------------------------------------------------
# 白名单后缀：只放行浏览器证据，避免把这里变成任意文件读取入口。
_EVIDENCE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".spec.ts", ".txt", ".json")


def _safe_evidence_path(pid: str, name: str, sub: str = "") -> Optional[Path]:
    """把 URL 里的文件名解析成真实的 artifacts 路径；任何可疑输入直接拒绝。"""
    # pid 与文件名都不允许出现路径分隔符或上跳，杜绝 ../../ 穿越
    for part in (pid, name):
        if not part or "/" in part or "\\" in part or ".." in part:
            return None
    if not name.lower().endswith(_EVIDENCE_SUFFIXES):
        return None
    art = (pm.PROJECTS_DIR / pid / "artifacts").resolve()
    f = (art / sub / name).resolve() if sub else (art / name).resolve()
    try:
        f.relative_to(art)   # 解析后必须仍在 artifacts 内（防符号链接逃逸）
    except ValueError:
        return None
    return f if f.is_file() else None


@app.get("/reports/<pid>/web_shots/<name>")
def project_web_shot(pid: str, name: str) -> Any:
    """Web 冒烟失败截图（artifacts/web_shots/*.png）。"""
    f = _safe_evidence_path(pid, name, sub="web_shots")
    if not f:
        return "证据文件不存在", 404
    return send_file(f)


@app.get("/reports/<pid>/<name>")
def project_artifact_file(pid: str, name: str) -> Any:
    """项目 artifacts/ 下的证据文件（如 web_repro_*.spec.ts）。"""
    if name == "report.html":     # 交给更具体的路由处理
        return "报告尚未生成，请先执行全流程测试", 404
    f = _safe_evidence_path(pid, name)
    if not f:
        return "文件不存在或不允许访问", 404
    return send_file(f)


if __name__ == "__main__":
    print("=" * 56)
    print("  软件测试智能体 · Web 控制台")
    print("  http://127.0.0.1:8765")
    # 明确告知鉴权状态：不说清楚的话，"开了还是没开"只能靠试。
    # 注意：**绝不回显 token 本身**。
    if console_auth.enabled():
        print(f"  访问鉴权：已开启（token 来自环境变量 {console_auth.TOKEN_ENV}）")
        print("  关闭方式：删除 .env 中的该行并重启")
    else:
        print(f"  访问鉴权：未开启（如需开启，在 .env 设 {console_auth.TOKEN_ENV}）")
    print("=" * 56)
    app.run(host="127.0.0.1", port=8765, debug=False)
