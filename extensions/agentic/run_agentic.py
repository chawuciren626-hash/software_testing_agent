"""S1 独立入口：AI 探索测试（把 B 世界的探索能力接进确定性流水线）。

定位（依据 `docs/HARNESS_ARCHITECTURE_REVIEW.md` §3.3 / §7 序 6）
----------------------------------------------------------------
**独立入口 / 独立进程**，由 `project_manager` 以**子进程 + 明确契约**调用：

    输入  : 一个 mission 文件（项目目录下 `mission.yaml`，或 `--mission` 指定）
    输出  : 一个结构化契约文件 `artifacts/agentic.json`（见 agentic_contract.py）
    再进入: 现有的报告链（项目报告卡片 / run_meta 溯源 / 看板）

为什么不 import 进 `project_manager`（决策 D2）：① 会把 langgraph/langchain/langmem
这些**重依赖**拖进刻意精简的 CI 硬门禁安装路径；② 一个进程里混"确定性门禁"与
"LLM 不确定性"会让超时/异常语义互相污染；③ 与刚消除的双入口漂移反向。

降级（本阶段的验收判据）
------------------------
`无 key / 超时 / 异常` 任一情况 → **降级**：写 `status=degraded` + **原因枚举**，
并把原因**报到 stderr**（"降级必须出声"）。降级**不等于失败**：探索不是门禁，
它只是流水线里一个**可降级阶段**；本轮结论回落到确定性链路（回归/性能安全/Web）。

退出码语义
----------
**契约文件是唯一权威**。只要把 `agentic.json` 写成功（`ok` 或 `degraded`）就返回 0；
只有当连契约都写不出来（解释器/bug 级崩溃）才返回非 0 —— 此时调用方
（`project_manager._step_agentic`）会**再兜一层**，合成降级结果。
"两处都兜"是刻意的：单点兜底一旦失效，表现就是"悄悄没了这一环"。

用法
----
    python extensions/agentic/run_agentic.py --project-dir projects/<id>
    python extensions/agentic/run_agentic.py --project-dir projects/<id> --mission m.yaml \\
        --max-steps 20 --timeout 600
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 本文件所在目录（extensions/agentic）用于 import 契约模块；
# 仓库根用于定位 src/ 与 extensions/。
_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
for _p in (str(_HERE), str(ROOT / "src"), str(ROOT / "extensions")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agentic_contract as C  # noqa: E402  (同目录，零依赖)

# 统一日志出口（唯一定义处，§4.2 / §7 序 3）。
# 约定：**结果**走 stdout 的 `print`（可被管道接走），**诊断**走 `log.*`（stderr + 可选文件）。
# 探索入口属 A 世界（extensions/），因此受序 3 的静态纪律约束（守护见 tests/test_obs.py）。
from common.obs import get_logger  # noqa: E402

log = get_logger("agentic")

DEFAULT_MISSION = "mission.yaml"
DEFAULT_MAX_STEPS = 30


# --------------------------------------------------------------------------- #
# 同源探测：不另立一套"有没有 key"的口径
# --------------------------------------------------------------------------- #
def probe_provider() -> Tuple[str, str]:
    """问基座自己"当前 LLM 提供方是什么"。

    **必须同源**：直接调用 `agentic_explorer.utils.llm.get_active_provider()`，
    而不是在这里重写一遍"看哪些环境变量算有 key"。否则同一件事会有两个答案，
    而"有 key 却跑不动"正是最难查的那种不一致。

    该模块只 import 标准库（重依赖都是函数内惰性导入），所以这个探测**不需要装
    langgraph 也能跑** —— 降级路径因此可以在 CI 硬门禁里被真实验证。

    返回 ``(provider, detail)``；``provider == "unknown"`` 表示判不出来。
    """
    try:
        from agentic_explorer.utils.llm import get_active_provider
    except Exception as e:                                  # 判不出来也是"没有 key"
        return "unknown", f"无法导入基座 LLM 判定模块（{type(e).__name__}: {e}）"
    try:
        return str(get_active_provider() or "unknown"), "agentic_explorer.utils.llm"
    except Exception as e:                                  # 防御：判定本身抛错
        return "unknown", f"{type(e).__name__}: {e}"


def load_env_file() -> Optional[Path]:
    """载入 .env（密钥分离）。

    实现收敛到 `extensions/common/auth.load_dotenv`（唯一定义处，§4.1）——
    独立入口**必须自己 load**，否则凭据大面积缺失（这是踩过的坑）。
    """
    try:
        from common.auth import load_dotenv
        return load_dotenv()
    except Exception as e:
        # 必须出声：载不到 .env 就等于"凭据可能全缺"，而这会静默变成一次无谓的降级。
        log.warning("载入 .env 失败（凭据可能缺失）：%s", e)
        return None


# --------------------------------------------------------------------------- #
# 子进程命令与环境（纯函数，便于单测）
# --------------------------------------------------------------------------- #
def bworld_cmd(python: str, mission: Path, max_steps: int,
               headed: bool = False) -> List[str]:
    """构造基座探索命令（`agent-explorer` 的模块入口形式）。"""
    cmd = [str(python), "-m", "agentic_explorer.main",
           "--missions", str(mission), "--max-steps", str(int(max_steps))]
    if headed:
        cmd.append("--headed")
    return cmd


def bworld_env(project_dir: Path, base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """构造子进程环境：把**项目**声明的目标与凭据翻译成基座认识的变量名。

    映射依据（口径唯一）：目标地址取 `project.yaml` 的 `env.base_url`；
    用户名/口令取 `common.auth.resolve_auth` —— 该函数按 `project.yaml` 声明的
    `*_env` 变量名去**进程环境**（`os.environ`）里取真值，这里不另写取值规则。

    返回的字典全部用 `setdefault` 写入 —— CI/命令行显式注入的同名变量**优先**，不被覆盖。
    `base_env` 只作为"底层环境"（默认 `os.environ`）；凭据真值仍来自进程环境，
    所以测试里要用 `monkeypatch.setenv` 提供凭据，而不是伪造 `base_env`。
    """
    env = dict(os.environ if base_env is None else base_env)
    env.setdefault("PYTHONIOENCODING", "utf-8")   # 中文产物在 Windows 上不乱码

    # APP_CONFIG：让基座去读项目自带的 config.yaml（存在时）；不存在则退回 APP_URL 兜底。
    # 先于 project.yaml 处理 —— 两者互不依赖，缺 project.yaml 不该顺带丢掉 config.yaml。
    cfg = Path(project_dir) / "config.yaml"
    if cfg.is_file():
        env.setdefault("APP_CONFIG", str(cfg))

    project_yaml = Path(project_dir) / "project.yaml"
    if not project_yaml.is_file():
        return env
    try:
        from common.yamlio import load_yaml
        from common.auth import resolve_auth
        project = load_yaml(project_yaml) or {}
    except Exception as e:
        # 必须出声：读不出 project.yaml 的后果是"子进程拿不到目标地址与凭据"，
        # 那会表现成一次看起来无厘头的降级 —— 不留痕就无从归因。
        log.warning("读取 %s 失败，探索子进程将只带基础环境：%s", project_yaml, e)
        return env
    if not isinstance(project, dict):
        log.warning("%s 结构异常（非映射），忽略其环境映射。", project_yaml)
        return env

    env_cfg = project.get("env", {}) or {}
    base_url = str(env_cfg.get("base_url") or "")
    if base_url:
        env.setdefault("APP_URL", base_url)

    # 凭据真值只有一个来源：**进程环境**（口径唯一 —— resolve_auth 读 os.getenv，
    # 按 project.yaml 里声明的变量名取真值）。这里不另写一套取值规则。
    try:
        auth = resolve_auth(project)
    except Exception:
        auth = {}
    if auth.get("username"):
        env.setdefault("APP_USERNAME", str(auth["username"]))
    if auth.get("password"):
        env.setdefault("APP_PASSWORD", str(auth["password"]))
    return env


# --------------------------------------------------------------------------- #
# 跑基座 + 收集产物
# --------------------------------------------------------------------------- #
def _spawn_bworld(cmd: List[str], *, cwd: Path, env: Dict[str, str],
                  timeout: Optional[float], log_file: Path) -> int:
    """起子进程跑基座，stdout/stderr 合并落 `log_file`；超时由 `subprocess` 抛 `TimeoutExpired`。

    单独成函数是为了能在单测里被替换 —— 否则"超时/异常降级"这两条路只能靠真跑，
    而真跑需要 key + 活的被测应用，等于这两条路永远没人验。
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)
    cwd.mkdir(parents=True, exist_ok=True)
    with open(log_file, "w", encoding="utf-8") as fh:
        fh.write("$ " + " ".join(cmd) + "\n\n")
        fh.flush()
        proc = subprocess.run(
            cmd, cwd=str(cwd), env=env,
            stdout=fh, stderr=subprocess.STDOUT,
            timeout=(timeout if timeout and timeout > 0 else None),
            encoding="utf-8", errors="replace",
        )
    return int(proc.returncode)


def find_bworld_result(run_dir: Path) -> Optional[Path]:
    """在探索目录里找**最新一次**任务的 `report_<thread_id>/result.json`。"""
    cands = sorted(Path(run_dir).glob("report_*/result.json"),
                   key=lambda p: p.stat().st_mtime)
    return cands[-1] if cands else None


def _rel(path: Optional[Path]) -> Optional[str]:
    """把绝对路径尽量表达成**相对仓库根**的形式（报告/看板里更可读、可移植）。"""
    if path is None:
        return None
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except Exception:
        # 可忽略：只是"能不能表达成相对路径"的美化，转不出来就退回绝对路径，
        # 不影响任何判定（报告里的链接由 project_manager 侧再做一次相对化）。
        return str(path)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run_stage(*, project_dir: Any,
              mission: Optional[Any] = None,
              python: Optional[str] = None,
              max_steps: int = DEFAULT_MAX_STEPS,
              timeout: Optional[float] = None,
              headed: bool = False,
              out: Optional[Any] = None,
              run_dir: Optional[Any] = None,
              log_file: Optional[Any] = None,
              env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """公开入口：**保证不抛异常**。

    内部任何未被下层捕获的异常都转成 `degraded(error)` 契约并落盘 ——
    这是"探索是可降级阶段"的最后一道保险：它再糟也不能拖垮流水线，
    但也**绝不能**因此变成"看起来跑过了"（所以写的是 degraded 而不是空/成功）。
    """
    pdir = Path(project_dir)
    out_p = Path(out) if out else C.result_path(pdir)
    try:
        return _run_stage(
            project_dir=project_dir, mission=mission, python=python,
            max_steps=max_steps, timeout=timeout, headed=headed,
            out=out, run_dir=run_dir, log_file=log_file, env=env)
    except Exception as e:      # 兜底：把"脚本级意外"也收进契约，别让它冒泡成调用方的崩溃
        log.warning("探索阶段内部异常（已兜底为降级）：%s: %s", type(e).__name__, e)
        payload = C.build_degraded(
            C.DegradeReason.ERROR,
            f"探索阶段内部异常（已兜底，不影响流水线）：{type(e).__name__}: {e}",
            elapsed_s=0.0)
        try:
            C.write_result(out_p, payload)
        except Exception:
            # 可忽略（本地）：连契约都写不出时本进程已无能为力，
            # 交由 project_manager._step_agentic 的**进程级兜底**统一出声并合成降级结果。
            pass
        return payload


def _run_stage(*, project_dir: Any,
               mission: Optional[Any] = None,
               python: Optional[str] = None,
               max_steps: int = DEFAULT_MAX_STEPS,
               timeout: Optional[float] = None,
               headed: bool = False,
               out: Optional[Any] = None,
               run_dir: Optional[Any] = None,
               log_file: Optional[Any] = None,
               env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """主体流程（供 `run_stage` 包兜底）：探测 → 降级判定 → 跑基座 → 归一化落盘。"""
    t0 = time.monotonic()
    pdir = Path(project_dir)
    mission_p = Path(mission) if mission else pdir / DEFAULT_MISSION
    out_p = Path(out) if out else C.result_path(pdir)
    run_p = Path(run_dir) if run_dir else C.run_dir(pdir)
    log_p = Path(log_file) if log_file else C.log_path(pdir)
    py = python or sys.executable
    tmo = C.default_timeout(env if env is not None else os.environ) if timeout is None else float(timeout)

    def _elapsed() -> float:
        return time.monotonic() - t0

    # ① 同源探测 LLM 提供方（不 import 重依赖，故无 key 时"快速失败"而非"跑很久才报错"）
    provider, detail = probe_provider()

    # ② 动手之前的降级判定：缺任务 / 无 key → 直接降级，不进子进程
    decided = C.decide_degrade(provider=provider, mission_path=mission_p)
    if decided is not None:
        reason, message = decided
        if reason == C.DegradeReason.NO_LLM_KEY:
            message = f"{message}（探测来源：{detail}）"
        payload = C.build_degraded(reason, message, provider=provider, elapsed_s=_elapsed())
        C.write_result(out_p, payload)
        return payload

    # ③ 真正跑基座（子进程：重依赖与不确定性都关在这个进程里）
    run_p.mkdir(parents=True, exist_ok=True)
    child_env = bworld_env(pdir, env)
    cmd = bworld_cmd(py, mission_p, max_steps, headed=headed)
    try:
        rc = _spawn_bworld(cmd, cwd=run_p, env=child_env,
                           timeout=(tmo if tmo > 0 else None), log_file=log_p)
    except subprocess.TimeoutExpired:
        payload = C.build_degraded(
            C.DegradeReason.TIMEOUT,
            f"探索超过 {tmo:.0f}s 墙钟上限，已终止（子进程被回收）。完整日志：{_rel(log_p)}",
            provider=provider, elapsed_s=_elapsed())
        C.write_result(out_p, payload)
        return payload
    except Exception as e:      # 起不来（解释器不存在 / 权限 / OOM…）
        payload = C.build_degraded(
            C.DegradeReason.ERROR,
            f"启动探索子进程失败：{type(e).__name__}: {e}",
            provider=provider, elapsed_s=_elapsed())
        C.write_result(out_p, payload)
        return payload

    # ④ 收集产物：拿不到结构化结果就是"降级"，绝不写成成功
    res_p = find_bworld_result(run_p)
    bres = C.load_result(res_p) if res_p else None
    if bres is None:
        payload = C.build_degraded(
            C.DegradeReason.ERROR,
            f"探索进程退出码 {rc}，但未产出结构化结果"
            f"（应出现 {_rel(run_p)}/report_*/result.json）。完整日志：{_rel(log_p)}",
            provider=provider, elapsed_s=_elapsed())
        C.write_result(out_p, payload)
        return payload

    thread_id = bres.get("thread_id")
    report_dir = res_p.parent if res_p else None
    tape = (report_dir / "action_tape.jsonl") if report_dir else None
    bug_items = bres.get("bug_items")
    if not isinstance(bug_items, list):      # 老版本基座只给计数：退回计数，不编造内容
        bug_items = []
    bug_count = int(bres.get("bugs") or 0)

    payload = C.build_ok(
        provider=provider,
        thread_id=str(thread_id) if thread_id else None,
        end_reason=C.normalize_end_reason(bres.get("end_reason")),
        goal_verdict=bres.get("goal_verdict"),
        reasons=bres.get("reasons") or [],
        turns=int(bres.get("turns") or 0),
        tokens=int(bres.get("tokens") or 0),
        actions=int(bres.get("actions") or 0),
        bugs=bug_items or ([f"（基座未提供原文，仅报告了 {bug_count} 个发现）"] if bug_count else []),
        # 计数以基座报告为准：没有原文时**不能**用"占位符条数(1)"冒充真实发现数
        bug_count=bug_count if not bug_items else None,
        report_dir=_rel(report_dir),
        action_tape=_rel(tape) if tape and tape.is_file() else None,
        log=_rel(log_p),
        elapsed_s=_elapsed(),
    )
    C.write_result(out_p, payload)
    return payload


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="AI 探索测试（独立入口；输出 artifacts/agentic.json 契约）")
    ap.add_argument("--project-dir", required=True, help="项目目录（projects/<id>）")
    ap.add_argument("--mission", default=None,
                    help=f"mission 文件；默认 <项目目录>/{DEFAULT_MISSION}")
    ap.add_argument("--out", default=None, help="契约输出路径；默认 <项目目录>/artifacts/agentic.json")
    ap.add_argument("--run-dir", default=None, help="基座产物目录；默认 <项目目录>/artifacts/agentic_run")
    ap.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS, help="基座软步数上限")
    ap.add_argument("--timeout", type=float, default=None,
                    help="墙钟上限（秒）；默认取 STA_EXPLORE_TIMEOUT，未设则 1800；0 表示不限")
    ap.add_argument("--headed", action="store_true", help="浏览器带界面跑（调试用）")
    ap.add_argument("--python", default=None, help="跑基座用的解释器；默认当前解释器")
    ap.add_argument("--fail-on-degraded", action="store_true",
                    help="降级时返回非 0（供「显式要求探索」的调用方使用）")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    # .env 必须在探测之前载入：凭据就在里面。
    load_env_file()

    payload = run_stage(
        project_dir=args.project_dir,
        mission=args.mission,
        python=args.python,
        max_steps=args.max_steps,
        timeout=args.timeout,
        headed=args.headed,
        out=args.out,
        run_dir=args.run_dir,
    )

    if C.is_ok(payload):
        # 结果通道：stdout 一行结论（可被管道/控制台采集）
        print(C.render_summary_line(payload))
        print(f"  契约：{args.out or C.result_path(args.project_dir)}")
        return 0

    # 降级：**出声**（诊断走日志 → stderr + 可选文件），并且明确"这不算通过"。
    # 用 log 而不是 print(file=sys.stderr)：只有走日志才带 run_id、能分级、能被采集
    # （序 3 的判据 #11；`tests/test_obs.py` 会静态扫描这类写法）。
    log.warning(C.render_degrade_warning(payload))
    print(C.render_summary_line(payload))
    return 3 if args.fail_on_degraded else 0


if __name__ == "__main__":
    raise SystemExit(main())
