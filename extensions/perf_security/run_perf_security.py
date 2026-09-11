"""性能 + 安全 冒烟执行器（零新增依赖：requests + 线程池）。

路线图第 ④ 块：性能与安全。
设计取舍——**不引入 locust**：本项目的定位是"开发/CI 阶段的质量门禁"，
需要的是秒级完成、可直接进流水线、失败即非零退出的冒烟；locust 更适合
专职压测场景（需要独立环境与较长观测窗口）。因此这里用
`concurrent.futures.ThreadPoolExecutor` + `requests` 实现轻量并发，
指标口径向压测标准看齐（p50/p95/p99 / 错误率 / 吞吐 / 阈值门禁）。
`locustfile_api.py` 作为专职压测入口保留，供需要长压时使用。

复用与一致性：
- 认证/环境解析完全复用 extensions/regression/run_regression.py 的约定
  （project.yaml 的 env.auth + 密钥分离：只存环境变量名，真值在 .env）。
- 压测目标若未显式声明，则**从 regression.yaml 的 api_smoke 项自动派生**，
  保证"压的就是回归的那批接口"，避免两处声明漂移。

⚠ 防假绿（与核心回归同源的教训，非常重要）：
  1. 不少后端（mall-admin 即是）HTTP 状态码恒为 200、成败写在 body 的业务码里。
     因此错误判定必须**看业务码**，只统计 HTTP 状态码会把 500 当成功。
  2. 环境不可达导致全部请求失败/无有效样本时 → SKIP，**不判绿**，
     避免 CI 拿到"性能通过"的虚假信号。
  3. 检查项必须"确有执行"才可能通过（executed > 0）。

写操作保护：路径含 register/create/add/save/delete/remove/update/modify/upload
  的目标默认**不参与压测**（避免造脏数据），需要时必须在该目标上显式写
  `write: true` 才执行。

用法：
    python run_perf_security.py --project projects/mall-admin/project.yaml \
        --regression projects/mall-admin/regression.yaml \
        --json projects/mall-admin/artifacts/perf_security.json
    python run_perf_security.py --project .../project.yaml --only security
    python run_perf_security.py --project .../project.yaml --users 16 --iterations 10
也可被 project_manager 作为模块导入：run_all(project_path, regression_path, out_json)。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import math
import os
import random
import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


# ---------------------------------------------------------------------------
# 复用核心回归的 YAML / 认证 / 占位替换 / 点路径取值（同源，不重复实现）
# ---------------------------------------------------------------------------
_REGRESSION_DIR = Path(__file__).resolve().parent.parent / "regression"
if str(_REGRESSION_DIR) not in sys.path:
    sys.path.insert(0, str(_REGRESSION_DIR))
try:
    import run_regression as rr  # noqa: E402
    _load_yaml = rr._load_yaml
    _resolve_auth = rr._resolve_auth
    _substitute = rr._substitute
    _dig = rr._dig
except Exception:  # pragma: no cover - 独立运行时兜底
    rr = None  # type: ignore

    def _load_yaml(path: Path) -> Dict[str, Any]:  # type: ignore
        import yaml
        return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

    def _resolve_auth(project: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore
        auth = ((project.get("env", {}) or {}).get("auth", {}) or {})
        return {
            "type": auth.get("type", "none"),
            "login_url": auth.get("login_url", ""),
            "token_field": auth.get("token_field", "token"),
            "username": os.getenv(auth["username_env"], "") if auth.get("username_env") else "",
            "password": os.getenv(auth["password_env"], "") if auth.get("password_env") else "",
            "token": os.getenv(auth["token_env"], "") if auth.get("token_env") else "",
        }

    def _substitute(body: Any, auth: Dict[str, Any]) -> Any:  # type: ignore
        if isinstance(body, str):
            return body.replace("{{username}}", auth["username"]).replace("{{password}}", auth["password"])
        if isinstance(body, dict):
            return {k: _substitute(v, auth) for k, v in body.items()}
        if isinstance(body, list):
            return [_substitute(v, auth) for v in body]
        return body

    def _dig(data: Any, dotted: str) -> Any:  # type: ignore
        cur = data
        for part in dotted.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        return cur


def _fallback_load_dotenv() -> Optional[Path]:
    """极简 .env 加载（当 run_regression 未提供 load_dotenv 时兜底）。"""
    root = Path(os.environ.get("STA_ROOT") or Path(__file__).resolve().parents[2])
    p = root / ".env"
    if not p.is_file():
        return None
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return p


# 密钥分离：真实口令只在 .env（gitignore）。独立运行本模块时必须自行加载，
# 否则凭据为空 → 登录失败 → 受保护接口全部 401 → 表现为"性能/安全大面积失败"。
_load_dotenv = getattr(rr, "load_dotenv", None) if rr is not None else None
if _load_dotenv is None:  # pragma: no cover
    _load_dotenv = _fallback_load_dotenv


DEFAULT_TIMEOUT = 10

# 误伤保护：这些路径默认不压（写操作会造脏数据）
_WRITE_HINTS = ("register", "create", "add", "save", "insert", "delete",
                "remove", "update", "modify", "upload", "import", "reset",
                "logout", "batch")

# 堆栈/敏感信息泄露特征（只用于"失败响应"的扫描，避免误伤正常业务文案）
_STACK_SIGNS = (
    "traceback (most recent call last)", "org.springframework.", "java.lang.",
    "javax.servlet", "java.sql.", "sqlexception", "sqlsyntaxerror", "badgrammar",
    "mysqlsyntaxerror", "psqlexception", "oracle.jdbc", "ora-0", "sqlite3.operationalerror",
    "stack trace", "whitelabel error page", "internal server error",
    "no such method", "nullpointerexception", "site-packages", "at com.",
    "at org.", "at java.", "\\users\\", "/usr/local/", "/var/www", "/home/",
)
# 拒绝/未授权的语义特征（多后端通用）
_AUTH_DENY_WORDS = ("未登录", "未授权", "token", "unauthorized", "forbidden",
                    "权限", "禁止", "登录已过期", "denied", "authentication")


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def _percentile(sorted_vals: List[float], p: float) -> float:
    """线性插值分位数（与常见压测工具口径一致）。"""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * (p / 100.0)
    lo = int(math.floor(k))
    hi = min(lo + 1, len(sorted_vals) - 1)
    return float(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo))


def _json_or_none(resp: Any) -> Optional[Any]:
    try:
        return resp.json()
    except Exception:
        return None


def _business_code(payload: Any) -> Any:
    """抽取业务码：兼容 code / status / errcode / errCode / success 等常见封装。"""
    if isinstance(payload, dict):
        for k in ("code", "status", "errcode", "errCode", "resultCode"):
            if k in payload:
                return payload[k]
    return None


# 各后端"成功码"口径不一（0 / 200 / "success"…），命中其一即视为成功，
# 避免因为口径差异把正常响应判成失败。
_SUCCESS_SENTINELS = {0, 200, "0", "200", "success", "SUCCESS", "ok", "OK", True}


def _business_ok(payload: Any, expect_code: Any = None) -> Optional[bool]:
    """按业务码判定成功。

    返回 None 表示"该项不适用业务码判定"（调用方回落到 HTTP 状态码）。
    关键：**未显式声明期望业务码时不做业务码判定**。
    因为像 mall-admin 的 `GET /` 是安全过滤器的兜底路由，HTTP 200 但 body 里
    code=401；若强行按业务码判定会把"正常的未授权响应"误判成性能失败。
    显式声明（regression 的 expect_json.code / perf 的 expect_code）才启用业务码口径。
    """
    if expect_code is None:
        return None
    if not isinstance(payload, dict):
        return None
    code = _business_code(payload)
    if code is None:
        if isinstance(payload.get("success"), bool):
            return payload["success"]
        return None
    if str(code) == str(expect_code):
        return True
    if code in _SUCCESS_SENTINELS:
        return True
    return False


def _looks_rejected(resp: Any, payload: Any) -> bool:
    """判断"请求被拒绝/未授权"。多后端通用：状态码 → 业务码 → 语义文案。

    注意：不能只看"有没有 data"，也不能只看 HTTP 200；
    这里要求候选中至少一条成立，且优先信任 HTTP 401/403 与业务码。
    """
    if resp is None:
        return False
    if resp.status_code in (401, 403):
        return True
    if resp.status_code == 400 and _business_code(payload) is None:
        return True
    b_ok = _business_ok(payload, 200)
    if b_ok is False:
        return True
    text = (resp.text or "")[:2000]
    low = text.lower()
    if any(w in text or w in low for w in _AUTH_DENY_WORDS):
        # 语义命中还需"没有拿到实质数据"佐证，避免正常响应里出现 "token" 字样被误判
        data = payload.get("data") if isinstance(payload, dict) else None
        if data in (None, "", [], {}):
            return True
    return False


def _has_token(payload: Any, token_field: str = "token") -> bool:
    """响应体里是否真的下发了 token/凭据（用于登录成功判定）。"""
    if not isinstance(payload, dict):
        return False
    if _dig(payload, token_field) not in (None, ""):
        return True
    for container in ("data", "result", "payload"):
        sub = payload.get(container)
        if isinstance(sub, dict):
            if _dig(sub, token_field) not in (None, ""):
                return True
            if _dig(sub, "access_token") not in (None, "") or _dig(sub, "jwt") not in (None, ""):
                return True
        elif isinstance(sub, str) and sub:
            # 某些后端直接把 token 字符串放在 data 里
            return True
    return False


def _auth_header(payload: Any, token_field: str) -> Optional[str]:
    """从登录响应体构造 Authorization 头（兼容 tokenHead 前缀）。"""
    head = ""
    token = ""
    if isinstance(payload, dict):
        head = str(payload.get("tokenHead") or "")
        token = str(_dig(payload, token_field) or "")
        if not token and isinstance(payload.get("data"), dict):
            head = head or str(payload["data"].get("tokenHead") or "")
            token = str(_dig(payload["data"], token_field) or "")
        if not token and isinstance(payload.get("data"), str):
            token = payload["data"]
    if not token:
        return None
    return f"{head}{token}".strip() if head else f"Bearer {token}"


# ---------------------------------------------------------------------------
# 登录（拿原始响应，安全项需要看失败回显）
# ---------------------------------------------------------------------------
def _is_local_url(url: str) -> bool:
    """目标是否为本地/内网地址（本地、回环、私有网段、.local 内网域名）。"""
    try:
        host = urlsplit(url).hostname or ""
    except Exception:
        return False
    host = host.lower()
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    if host.startswith(("10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.",
                        "172.2", "172.30.", "172.31.")):
        return True
    return host.endswith((".local", ".internal", ".lan"))


def _session(url: str) -> Any:
    """按目标选择会话——**性能指标必须绕开 HTTP 代理**。

    这台机器（以及不少公司环境）会设置 HTTP_PROXY/HTTPS_PROXY；一旦请求走代理，
    就多了一跳网络与排队，p95/p99 与吞吐会被代理彻底污染，指标失去参考价值
    （更糟的是会得到"看起来很慢"的错误结论）。因此对本地/内网目标强制直连
    （trust_env=False），公网目标仍沿用环境变量里的代理配置。
    """
    s = requests.Session()
    if _is_local_url(url):
        s.trust_env = False
    return s


def _login_raw(base_url: str, auth: Dict[str, Any],
               username: Optional[str] = None,
               password: Optional[str] = None,
               session: Optional[Any] = None) -> Tuple[Any, Any]:
    url = base_url.rstrip("/") + (auth.get("login_url") or "/login")
    sess = session or _session(url)
    try:
        resp = sess.post(
            url,
            json={"username": auth["username"] if username is None else username,
                  "password": auth["password"] if password is None else password},
            timeout=DEFAULT_TIMEOUT,
        )
    except Exception:
        return None, None
    return resp, _json_or_none(resp)


def _obtain_auth_header(base_url: str, auth: Dict[str, Any],
                        session: Optional[Any] = None) -> Tuple[Optional[str], Dict[str, Any]]:
    """执行一次正常登录，返回 (Authorization 头, 诊断信息)。"""
    if auth.get("type") == "bearer" and auth.get("token"):
        return f"Bearer {auth['token']}", {"mode": "bearer", "ok": True}
    if auth.get("type") != "form":
        return None, {"mode": auth.get("type", "none"), "ok": False,
                      "reason": "无需认证或未配置登录"}
    resp, payload = _login_raw(base_url, auth, session=session)
    if resp is None:
        return None, {"mode": "form", "ok": False, "reason": "登录请求失败（环境不可达）"}
    header = _auth_header(payload, auth.get("token_field", "token"))
    return header, {"mode": "form", "ok": bool(header),
                    "status": resp.status_code,
                    "reason": "" if header else "登录未取到 token（凭据或环境异常）"}


def _request(base_url: str, target: Dict[str, Any], auth_header: Optional[str],
             auth_ctx: Dict[str, Any], timeout: int = DEFAULT_TIMEOUT,
             session: Optional[Any] = None) -> Tuple[Any, float]:
    """按目标声明发起一次请求，返回 (响应/异常对象, 耗时毫秒)。"""
    method = (target.get("method") or "GET").upper()
    path = target.get("path", "/")
    url = base_url.rstrip("/") + path
    body = _substitute(target.get("body"), auth_ctx)
    headers = {"Content-Type": "application/json"}
    if target.get("auth") == "required" and auth_header:
        headers["Authorization"] = auth_header
    extra = target.get("headers") or {}
    if isinstance(extra, dict):
        headers.update({str(k): str(v) for k, v in extra.items()})
    sess = session or _session(url)
    t0 = time.perf_counter()
    try:
        if method == "GET":
            resp = sess.get(url, params=body, headers=headers, timeout=timeout)
        else:
            resp = sess.request(method, url, json=body, headers=headers, timeout=timeout)
    except Exception as e:
        return e, (time.perf_counter() - t0) * 1000.0
    return resp, (time.perf_counter() - t0) * 1000.0


# ---------------------------------------------------------------------------
# 压测目标解析：显式声明优先，否则从 regression.yaml 的 api_smoke 派生
# ---------------------------------------------------------------------------
def _is_write_path(path: str) -> bool:
    low = (path or "").lower()
    return any(h in low for h in _WRITE_HINTS)


def _derive_targets(project: Dict[str, Any],
                    regression: Optional[Dict[str, Any]],
                    limit: int = 3) -> Tuple[List[Dict[str, Any]], str]:
    """确定压测目标：(targets, source_desc)。

    优先级：project.yaml 的 perf_security.perf.targets（显式）
            → regression.yaml 的 api_smoke 项（同源派生，只读接口）
            → 内置兜底（健康检查 + 登录）。
    """
    cfg = ((project.get("perf_security") or {}).get("perf") or {})
    declared = cfg.get("targets") or []
    if declared:
        picked, skipped = [], []
        for t in declared:
            if _is_write_path(t.get("path", "")) and not t.get("write"):
                skipped.append(t.get("name") or t.get("path"))
                continue
            picked.append(t)
        desc = "project.yaml（显式声明）" + (f"；跳过写操作目标 {skipped}" if skipped else "")
        return picked, desc

    if regression:
        picked = []
        for it in (regression.get("core_business") or []):
            if it.get("type", "api_smoke") != "api_smoke":
                continue
            if str(it.get("auth")) == "required" and not it.get("path"):
                continue
            if _is_write_path(it.get("path", "")):
                continue
            picked.append({
                "name": it.get("name", it.get("path", "?")),
                "method": it.get("method", "GET"),
                "path": it.get("path", "/"),
                "body": it.get("body"),
                "auth": it.get("auth"),
                # 与 regression 的断言口径严格对齐：只有 regression 显式声明了
                # expect_json.code 才启用业务码判定，否则只看 HTTP 状态码。
                "expect_code": it.get("expect_code",
                                      _dig(it.get("expect_json") or {}, "code")),
            })
            if len(picked) >= limit:
                break
        if picked:
            return picked, f"由 regression.yaml 派生（只读接口，取前 {len(picked)} 项）"

    auth = _resolve_auth(project)
    fallback: List[Dict[str, Any]] = [{"name": "健康检查", "method": "GET", "path": "/",
                                       "expect_code": 200}]
    if auth.get("login_url"):
        fallback.append({"name": "登录", "method": "POST", "path": auth["login_url"],
                         "body": {"username": "{{username}}", "password": "{{password}}"},
                         "expect_code": 200})
    return fallback, "内置兜底（未声明目标且无 regression.yaml）"


def _perf_config(project: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    cfg = ((project.get("perf_security") or {}).get("perf") or {})
    thr = cfg.get("thresholds") or {}
    return {
        "users": int(getattr(args, "users", None) or cfg.get("users") or 8),
        "iterations": int(getattr(args, "iterations", None) or cfg.get("iterations") or 5),
        "warmup": int(cfg.get("warmup") or 2),
        "timeout": int(cfg.get("timeout") or DEFAULT_TIMEOUT),
        "thresholds": {
            "p95_ms": float(thr.get("p95_ms") or 0),
            "p99_ms": float(thr.get("p99_ms") or 0),
            "max_error_rate": float(thr.get("max_error_rate") or 0),
            "min_rps": float(thr.get("min_rps") or 0),
        },
    }


# ---------------------------------------------------------------------------
# 性能冒烟
# ---------------------------------------------------------------------------
def _run_one_target(base_url: str, target: Dict[str, Any], auth_header: Optional[str],
                    auth_ctx: Dict[str, Any], users: int, iterations: int,
                    timeout: int) -> Dict[str, Any]:
    expect_code = target.get("expect_code")
    total = max(1, users * iterations)
    # 热路径共用一个会话：绕开代理 + 复用连接池，避免把建连开销算进延迟
    sess = _session(base_url + (target.get("path") or "/"))

    def _once(_i: int) -> Dict[str, Any]:
        resp, ms = _request(base_url, target, auth_header, auth_ctx, timeout, session=sess)
        if isinstance(resp, Exception):
            return {"ms": ms, "ok": False, "kind": "UNREACHABLE", "note": str(resp)[:120]}
        payload = _json_or_none(resp)
        if resp.status_code >= 400:
            ok, kind = False, f"HTTP {resp.status_code}"
        else:
            b_ok = _business_ok(payload, expect_code)
            if b_ok is False:
                ok, kind = False, f"业务码 {_business_code(payload)}"
            else:
                ok, kind = True, "OK"
        return {"ms": ms, "ok": ok, "kind": kind, "note": ""}

    # 预热（不计入统计）：排除首连 TCP/TLS 与后端懒加载带来的长尾
    for _ in range(max(0, int(target.get("_warmup", 0)))):
        _once(0)

    samples: List[Dict[str, Any]] = []
    t0 = time.perf_counter()
    with cf.ThreadPoolExecutor(max_workers=max(1, users)) as pool:
        for r in pool.map(_once, range(total)):
            samples.append(r)
    elapsed = max(1e-6, time.perf_counter() - t0)

    ms_all = sorted(s["ms"] for s in samples)
    unreachable = sum(1 for s in samples if s["kind"] == "UNREACHABLE")
    ok_n = sum(1 for s in samples if s["ok"])
    fail_n = total - ok_n
    fails_detail: Dict[str, int] = {}
    for s in samples:
        if not s["ok"]:
            fails_detail[s["kind"]] = fails_detail.get(s["kind"], 0) + 1

    rec: Dict[str, Any] = {
        "name": target.get("name") or target.get("path"),
        "method": (target.get("method") or "GET").upper(),
        "path": target.get("path", "/"),
        "auth": target.get("auth") or "none",
        "expect_code": expect_code,
        "requests": total,
        "ok": ok_n,
        "failed": fail_n,
        "error_rate": round(fail_n / total, 4),
        "unreachable": unreachable,
        "rps": round(total / elapsed, 2),
        "elapsed_s": round(elapsed, 3),
        "concurrency": users,
        "min_ms": round(ms_all[0], 1),
        "avg_ms": round(statistics.fmean(ms_all), 1),
        "p50_ms": round(_percentile(ms_all, 50), 1),
        "p95_ms": round(_percentile(ms_all, 95), 1),
        "p99_ms": round(_percentile(ms_all, 99), 1),
        "max_ms": round(ms_all[-1], 1),
        "fail_kinds": fails_detail,
        "threshold_fails": [],
        "_ms": ms_all,      # 供整体分位重算；序列化前会剔除
    }
    if unreachable == total:
        rec["result"] = "SKIP"
    elif ok_n == 0:
        rec["result"] = "FAIL"
    else:
        rec["result"] = "PASS"
    return rec


def _apply_thresholds(rec: Dict[str, Any], thr: Dict[str, Any]) -> None:
    fails: List[str] = list(rec.get("threshold_fails") or [])
    if thr.get("p95_ms") and rec["p95_ms"] > thr["p95_ms"]:
        fails.append(f"P95 {rec['p95_ms']}ms > {thr['p95_ms']}ms")
    if thr.get("p99_ms") and rec["p99_ms"] > thr["p99_ms"]:
        fails.append(f"P99 {rec['p99_ms']}ms > {thr['p99_ms']}ms")
    if thr.get("max_error_rate") and rec["error_rate"] > thr["max_error_rate"]:
        fails.append(f"错误率 {rec['error_rate'] * 100:.2f}% > {thr['max_error_rate'] * 100:.2f}%")
    if thr.get("min_rps") and rec["rps"] < thr["min_rps"]:
        fails.append(f"吞吐 {rec['rps']} rps < {thr['min_rps']} rps")
    rec["threshold_fails"] = fails
    if fails and rec["result"] == "PASS":
        rec["result"] = "FAIL"


def run_perf(project: Dict[str, Any], base_url: str, auth: Dict[str, Any],
             auth_header: Optional[str], regression: Optional[Dict[str, Any]],
             cfg: Dict[str, Any]) -> Dict[str, Any]:
    targets, source = _derive_targets(project, regression)
    users, iterations = cfg["users"], cfg["iterations"]
    print(f"  [性能冒烟] 目标来源：{source}")
    print(f"  [性能冒烟] 并发 {users} × 每用户 {iterations} 次 = "
          f"{users * iterations} 请求/目标，预热 {cfg['warmup']} 次")

    results: List[Dict[str, Any]] = []
    for t in targets:
        t = dict(t)
        t["_warmup"] = cfg["warmup"]
        rec = _run_one_target(base_url, t, auth_header, auth, users, iterations, cfg["timeout"])
        _apply_thresholds(rec, cfg["thresholds"])
        results.append(rec)
        line = (f"  [{rec['result']}] {rec['name']} [{rec['method']} {rec['path']}] "
                f"成功 {rec['ok']}/{rec['requests']} · P50 {rec['p50_ms']}ms "
                f"P95 {rec['p95_ms']}ms P99 {rec['p99_ms']}ms · {rec['rps']} rps")
        print(line)
        if rec["fail_kinds"]:
            print(f"        失败构成：{rec['fail_kinds']}")
        for tf in rec["threshold_fails"]:
            print(f"        ⚠ 未达阈值：{tf}")

    executed = [r for r in results if r["result"] != "SKIP"]
    tot_req = sum(r["requests"] for r in executed)
    tot_ok = sum(r["ok"] for r in executed)
    # 整体分位：用全部样本真实重算（而非拼接各目标分位，避免口径失真）
    all_ms: List[float] = []
    for r in results:
        all_ms.extend(r.get("_ms") or [])
    all_ms.sort()
    overall = {
        "targets": len(results),
        "executed": len(executed),
        "requests": tot_req,
        "ok": tot_ok,
        "failed": tot_req - tot_ok,
        "error_rate": round((tot_req - tot_ok) / tot_req, 4) if tot_req else 0.0,
        "rps": round(sum(r["rps"] for r in executed), 2),
        "samples": len(all_ms),
        "min_ms": round(all_ms[0], 1) if all_ms else 0.0,
        "avg_ms": round(statistics.fmean(all_ms), 1) if all_ms else 0.0,
        "p50_ms": round(_percentile(all_ms, 50), 1) if all_ms else 0.0,
        "p95_ms": round(_percentile(all_ms, 95), 1) if all_ms else 0.0,
        "p99_ms": round(_percentile(all_ms, 99), 1) if all_ms else 0.0,
        "max_ms": round(all_ms[-1], 1) if all_ms else 0.0,
    }
    for r in results:
        r.pop("_ms", None)     # 明细不落 JSON，保持产物精简
    failed_n = sum(1 for r in results if r["result"] == "FAIL")
    skipped_n = sum(1 for r in results if r["result"] == "SKIP")
    if not executed:
        passed, reason = False, "全部目标环境不可达（SKIP），按未通过处理（防 CI 假绿）"
    elif failed_n:
        passed, reason = False, f"{failed_n} 个目标未达阈值或存在失败请求"
    else:
        passed, reason = True, "全部目标达到阈值"
    print(f"  [性能冒烟] 汇总：{'✅ 通过' if passed else '❌ 未通过'} — {reason}")
    return {
        "enabled": True,
        "passed": passed,
        "skipped": not executed,
        "reason": reason,
        "config": {"users": users, "iterations": iterations, "warmup": cfg["warmup"],
                   "thresholds": cfg["thresholds"]},
        "target_source": source,
        "overall": overall,
        "targets": results,
    }


# ---------------------------------------------------------------------------
# 安全冒烟
# ---------------------------------------------------------------------------
def _protected_endpoints(project: Dict[str, Any],
                         regression: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """确定"需要认证"的受保护接口（未授权访问检查的靶子）。"""
    cfg = ((project.get("perf_security") or {}).get("security") or {})
    declared = cfg.get("protected") or []
    if declared:
        return declared
    if regression:
        picked = [{"name": it.get("name", it.get("path", "?")),
                   "method": it.get("method", "GET"),
                   "path": it.get("path", "/"),
                   "body": it.get("body")}
                  for it in (regression.get("core_business") or [])
                  if it.get("type", "api_smoke") == "api_smoke"
                  and str(it.get("auth")) == "required" and it.get("path")
                  and not _is_write_path(it.get("path", ""))]
        if picked:
            return picked
    return [{"name": "当前用户信息", "method": "GET", "path": "/admin/info"}]


def _check_unauthorized(base_url: str, endpoints: List[Dict[str, Any]],
                        auth: Dict[str, Any]) -> Dict[str, Any]:
    """未授权访问：不携带 token 请求受保护接口，必须被拒绝。"""
    if not endpoints:
        return {"name": "未授权访问受保护接口", "category": "auth", "status": "SKIP",
                "detail": "未声明受保护接口，无法检查", "evidence": ""}
    leaks: List[str] = []
    checked = 0
    evid: List[str] = []
    for ep in endpoints:
        t = {"method": ep.get("method", "GET"), "path": ep.get("path", "/"),
             "body": ep.get("body"), "auth": "none"}
        resp, _ = _request(base_url, t, None, auth)
        if isinstance(resp, Exception):
            evid.append(f"{ep.get('path')}: 环境不可达")
            continue
        checked += 1
        payload = _json_or_none(resp)
        rejected = _looks_rejected(resp, payload)
        evid.append(f"{ep.get('path')}: HTTP {resp.status_code} / "
                    f"code={_business_code(payload)} / {'已拒绝' if rejected else '未拒绝'}")
        if not rejected:
            leaks.append(str(ep.get("path")))
    if checked == 0:
        return {"name": "未授权访问受保护接口", "category": "auth", "status": "SKIP",
                "detail": "环境不可达，未能完成检查", "evidence": "; ".join(evid)}
    if leaks:
        return {"name": "未授权访问受保护接口", "category": "auth", "status": "FAIL",
                "detail": f"{len(leaks)} 个受保护接口在无 token 时仍可访问：{', '.join(leaks)}",
                "evidence": "; ".join(evid)}
    return {"name": "未授权访问受保护接口", "category": "auth", "status": "PASS",
            "detail": f"{checked} 个受保护接口在无 token 时均被拒绝", "evidence": "; ".join(evid)}


def _check_wrong_password(base_url: str, auth: Dict[str, Any]) -> Dict[str, Any]:
    """错误口令：不得下发 token。"""
    name = "错误口令登录被拒绝"
    if auth.get("type") != "form" or not auth.get("login_url"):
        return {"name": name, "category": "auth", "status": "SKIP",
                "detail": "项目未配置表单登录，跳过", "evidence": ""}
    wrong = "definitely_wrong_pwd_" + str(random.randint(10000, 99999))
    resp, payload = _login_raw(base_url, auth, password=wrong)
    if resp is None:
        return {"name": name, "category": "auth", "status": "SKIP",
                "detail": "环境不可达，未能完成检查", "evidence": ""}
    got_token = _has_token(payload, auth.get("token_field", "token"))
    rejected = _looks_rejected(resp, payload)
    evid = (f"HTTP {resp.status_code} / code={_business_code(payload)} / "
            f"message={str((payload or {}).get('message', ''))[:60]}")
    if got_token or not rejected:
        return {"name": name, "category": "auth", "status": "FAIL",
                "detail": "错误口令未获拒绝" + ("，且下发了 token" if got_token else ""),
                "evidence": evid}
    return {"name": name, "category": "auth", "status": "PASS",
            "detail": "错误口令被正确拒绝（未下发 token）", "evidence": evid}


def _check_sql_injection(base_url: str, auth: Dict[str, Any]) -> Dict[str, Any]:
    """注入探针：不得绕过认证，也不得回显数据库错误。"""
    name = "SQL 注入探针未绕过认证"
    if auth.get("type") != "form" or not auth.get("login_url"):
        return {"name": name, "category": "injection", "status": "SKIP",
                "detail": "项目未配置表单登录，跳过", "evidence": ""}
    payloads = [
        ("admin' or '1'='1", "' or '1'='1' -- "),
        ("admin'--", "x"),
        ("' union select 1,2,3 -- ", "x"),
        ("admin' OR 1=1#", "x"),
    ]
    bypassed: List[str] = []
    db_errors: List[str] = []
    reachable = False
    evid: List[str] = []
    for u, p in payloads:
        resp, payload = _login_raw(base_url, auth, username=u, password=p)
        if resp is None:
            continue
        reachable = True
        text = (resp.text or "")[:800]
        low = text.lower()
        if _has_token(payload, auth.get("token_field", "token")):
            bypassed.append(u)
        if any(s in low for s in ("sqlexception", "sqlsyntaxerror", "badgrammar",
                                  "mysqlsyntaxerror", "psqlexception", "ora-0",
                                  "sqlite3.operationalerror", "sqlstate")):
            db_errors.append(u)
        evid.append(f"{u[:28]}… → code={_business_code(payload)} / HTTP {resp.status_code}")
    if not reachable:
        return {"name": name, "category": "injection", "status": "SKIP",
                "detail": "环境不可达，未能完成检查", "evidence": ""}
    if bypassed:
        return {"name": name, "category": "injection", "status": "FAIL",
                "detail": f"以下注入载荷疑似绕过认证：{bypassed}", "evidence": "; ".join(evid)}
    if db_errors:
        return {"name": name, "category": "injection", "status": "FAIL",
                "detail": f"响应回显数据库错误（信息泄露）：{db_errors}", "evidence": "; ".join(evid)}
    return {"name": name, "category": "injection", "status": "PASS",
            "detail": f"{len(payloads)} 组注入载荷均未绕过认证、未回显数据库错误",
            "evidence": "; ".join(evid)}


def _check_stacktrace(base_url: str, auth: Dict[str, Any],
                      endpoints: List[Dict[str, Any]]) -> Dict[str, Any]:
    """错误响应不得泄露堆栈/绝对路径/框架内部类名。"""
    name = "错误响应不泄露堆栈信息"
    probes: List[Dict[str, Any]] = [
        {"name": "不存在的接口", "method": "GET", "path": "/__not_exists_probe__"},
        {"name": "畸形 JSON", "method": "POST", "path": (auth.get("login_url") or "/login"),
         "raw_bad_json": True},
        {"name": "非法方法", "method": "DELETE", "path": (auth.get("login_url") or "/login")},
        {"name": "空参数登录", "method": "POST", "path": (auth.get("login_url") or "/login"),
         "body": {"username": "", "password": ""}},
    ]
    for ep in endpoints[:1]:
        probes.append({"name": "受保护接口无 token", "method": ep.get("method", "GET"),
                       "path": ep.get("path", "/")})
    leaks: List[str] = []
    reachable = False
    evid: List[str] = []
    for p in probes:
        url = base_url.rstrip("/") + p["path"]
        sess = _session(url)
        try:
            if p.get("raw_bad_json"):
                resp = sess.post(url, data="{bad json", headers={"Content-Type": "application/json"},
                                 timeout=DEFAULT_TIMEOUT)
            elif p["method"] == "GET":
                resp = sess.get(url, timeout=DEFAULT_TIMEOUT)
            else:
                resp = sess.request(p["method"], url, json=p.get("body") or {},
                                    timeout=DEFAULT_TIMEOUT)
        except Exception:
            continue
        reachable = True
        text = (resp.text or "")[:4000]
        low = text.lower()
        hit = [s for s in _STACK_SIGNS if s in low]
        evid.append(f"{p['name']}: HTTP {resp.status_code}"
                    + (f" ⚠ 命中 {hit[:3]}" if hit else " 干净"))
        if hit:
            leaks.append(f"{p['name']}({', '.join(hit[:2])})")
    if not reachable:
        return {"name": name, "category": "disclosure", "status": "SKIP",
                "detail": "环境不可达，未能完成检查", "evidence": ""}
    if leaks:
        return {"name": name, "category": "disclosure", "status": "FAIL",
                "detail": f"失败响应泄露实现细节：{'; '.join(leaks)}", "evidence": "; ".join(evid)}
    return {"name": name, "category": "disclosure", "status": "PASS",
            "detail": f"{len(probes)} 组异常请求的错误响应均无堆栈/路径泄露",
            "evidence": "; ".join(evid)}


def _check_user_enumeration(base_url: str, auth: Dict[str, Any]) -> Dict[str, Any]:
    """用户枚举：错误口令下，"存在的用户"与"不存在的用户"响应是否可区分。"""
    name = "错误口令响应不泄露用户是否存在"
    if auth.get("type") != "form" or not auth.get("login_url") or not auth.get("username"):
        return {"name": name, "category": "disclosure", "status": "SKIP",
                "detail": "项目未配置表单登录或未提供已知账号，跳过", "evidence": ""}
    wrong = "wrong_pwd_probe_" + str(random.randint(10000, 99999))
    ghost = "no_such_user_" + str(random.randint(100000, 999999))

    def _sig(resp: Any, payload: Any) -> Tuple[Any, Any, Optional[str]]:
        msg = ""
        if isinstance(payload, dict):
            msg = str(payload.get("message") or payload.get("msg") or "")
        return (resp.status_code if resp else None,
                _business_code(payload), re.sub(r"\d+", "#", msg)[:60])

    r1, p1 = _login_raw(base_url, auth, password=wrong)
    r2, p2 = _login_raw(base_url, auth, username=ghost, password=wrong)
    if r1 is None or r2 is None:
        return {"name": name, "category": "disclosure", "status": "SKIP",
                "detail": "环境不可达，未能完成检查", "evidence": ""}
    s1, s2 = _sig(r1, p1), _sig(r2, p2)
    evid = f"已存在用户+错口令 → {s1}；不存在用户+错口令 → {s2}"
    if s1 == s2:
        return {"name": name, "category": "disclosure", "status": "PASS",
                "detail": "两类失败的响应特征一致，无法据此枚举用户", "evidence": evid}
    return {"name": name, "category": "disclosure", "status": "WARN",
            "detail": f"两类失败响应可区分（HTTP/业务码/文案存在差异），可能被用于枚举有效用户名：{s1} vs {s2}",
            "evidence": evid}


def _check_security_headers(base_url: str) -> Dict[str, Any]:
    """安全响应头：核心项缺失为 FAIL；通配 CORS / 建议项缺失为 WARN。"""
    name = "安全响应头检查"
    try:
        resp = _session(base_url).get(base_url.rstrip("/") + "/", timeout=DEFAULT_TIMEOUT)
    except Exception:
        return {"name": name, "category": "config", "status": "SKIP",
                "detail": "环境不可达，未能完成检查", "evidence": ""}
    h = {k.lower(): v for k, v in resp.headers.items()}
    core_missing = [k for k in ("x-content-type-options", "x-frame-options") if k not in h]
    advisory_missing = [k for k in ("content-security-policy", "strict-transport-security",
                                    "referrer-policy") if k not in h]
    cors = h.get("access-control-allow-origin", "")
    evid = (f"nosniff={h.get('x-content-type-options')} · "
            f"frame={h.get('x-frame-options')} · "
            f"CORS={cors or '未设置'} · "
            f"缺失建议项={advisory_missing or '无'}")
    if core_missing:
        return {"name": name, "category": "config", "status": "FAIL",
                "detail": f"缺少核心安全响应头：{core_missing}", "evidence": evid}
    if cors.strip() == "*":
        return {"name": name, "category": "config", "status": "WARN",
                "detail": "核心安全头齐全，但 Access-Control-Allow-Origin 为通配 '*'，"
                          "携带凭据的接口不应无条件放开跨域",
                "evidence": evid}
    if advisory_missing:
        return {"name": name, "category": "config", "status": "WARN",
                "detail": f"核心安全头齐全，建议补充：{advisory_missing}", "evidence": evid}
    return {"name": name, "category": "config", "status": "PASS",
            "detail": "核心与建议安全响应头均齐全", "evidence": evid}


def run_security(project: Dict[str, Any], base_url: str, auth: Dict[str, Any],
                 regression: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    endpoints = _protected_endpoints(project, regression)
    print(f"  [安全冒烟] 受保护接口 {len(endpoints)} 个，执行鉴权/注入/泄露/配置检查")
    checks = [
        _check_unauthorized(base_url, endpoints, auth),
        _check_wrong_password(base_url, auth),
        _check_sql_injection(base_url, auth),
        _check_stacktrace(base_url, auth, endpoints),
        _check_user_enumeration(base_url, auth),
        _check_security_headers(base_url),
    ]
    for c in checks:
        mark = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠", "SKIP": "⏭"}.get(c["status"], "?")
        print(f"  [{c['status']}] {mark} {c['name']} — {c['detail']}")
    passed = sum(1 for c in checks if c["status"] == "PASS")
    failed = sum(1 for c in checks if c["status"] == "FAIL")
    warned = sum(1 for c in checks if c["status"] == "WARN")
    skipped = sum(1 for c in checks if c["status"] == "SKIP")
    executed = len(checks) - skipped
    if executed == 0:
        ok, reason = False, "环境不可达，全部检查跳过，按未通过处理（防 CI 假绿）"
    elif failed:
        ok, reason = False, f"{failed} 项安全检查未通过"
    elif warned:
        ok, reason = True, f"{passed} 项通过；{warned} 项提示（非阻塞，建议修复）"
    else:
        ok, reason = True, f"{passed} 项安全检查全部通过"
    print(f"  [安全冒烟] 汇总：{'✅ 通过' if ok else '❌ 未通过'} — {reason}")
    return {
        "enabled": True, "passed": ok, "reason": reason,
        "total": len(checks), "passed_count": passed, "failed": failed,
        "warned": warned, "skipped": skipped,
        "protected_endpoints": [e.get("path") for e in endpoints],
        "checks": checks,
    }


def _baseline_ok(base_url: str, auth: Dict[str, Any],
                 auth_diag: Dict[str, Any]) -> Tuple[bool, str]:
    """基线校验：先用"已知正确的凭据"跑一次登录，确认目标确实是我们的应用且凭据有效。

    为什么必须有这一步——防止**误报**（比漏报更误导人）：
      地址写错 / 端口被别的服务占用 / 被代理拦截 / 凭据过期时，
      响应会是很普通的错误页（无安全响应头、也不返回 401），
      若直接判定，会得出"未授权访问受保护接口 = 存在漏洞"这种**假漏洞**结论。
      基线不成立时，依赖认证的检查应 SKIP 并说明原因，而不是报 FAIL。
    """
    if auth.get("type") == "form":
        if auth_diag.get("ok"):
            return True, "基线登录成功，环境与凭据有效"
        return False, (f"基线登录失败（{auth_diag.get('reason') or '未知原因'}）："
                       "无法确认目标环境与凭据有效性，依赖认证的安全检查已跳过"
                       "（不判为缺陷，避免误报）")
    if auth.get("type") == "bearer" and auth.get("token"):
        return True, "使用预置 bearer token 作为基线"
    try:
        r = _session(base_url).get(base_url.rstrip("/") + "/", timeout=DEFAULT_TIMEOUT)
    except Exception as e:
        return False, f"基线请求失败（{str(e)[:80]}）：环境不可达"
    if r.status_code >= 500:
        return False, (f"基线请求返回 HTTP {r.status_code}：目标环境异常"
                       "（地址错误 / 端口被其它服务占用 / 被代理拦截）")
    if "text/html" in (r.headers.get("Content-Type") or "") and _json_or_none(r) is None:
        return False, "基线响应是 HTML 而非接口数据：目标可能不是被测应用"
    return True, "基线请求正常（项目未配置认证，按匿名基线校验）"


def _empty_overall() -> Dict[str, Any]:
    return {"targets": 0, "executed": 0, "requests": 0, "ok": 0, "failed": 0,
            "error_rate": 0.0, "rps": 0.0, "samples": 0, "min_ms": 0.0,
            "avg_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0, "max_ms": 0.0}


def _skipped_perf(cfg: Dict[str, Any], reason: str) -> Dict[str, Any]:
    return {"enabled": True, "passed": False, "skipped": True, "reason": reason,
            "config": {"users": cfg["users"], "iterations": cfg["iterations"],
                       "warmup": cfg["warmup"], "thresholds": cfg["thresholds"]},
            "target_source": "未执行", "overall": _empty_overall(), "targets": []}


def _skipped_security(reason: str, endpoints: List[Dict[str, Any]]) -> Dict[str, Any]:
    names = [("未授权访问受保护接口", "auth"), ("错误口令登录被拒绝", "auth"),
             ("SQL 注入探针未绕过认证", "injection"), ("错误响应不泄露堆栈信息", "disclosure"),
             ("错误口令响应不泄露用户是否存在", "disclosure"), ("安全响应头检查", "config")]
    checks = [{"name": n, "category": c, "status": "SKIP", "detail": reason, "evidence": ""}
              for n, c in names]
    return {"enabled": True, "passed": False, "reason": reason,
            "total": len(checks), "passed_count": 0, "failed": 0, "warned": 0,
            "skipped": len(checks), "blocked_by_baseline": True,
            "protected_endpoints": [e.get("path") for e in endpoints],
            "checks": checks}


# ---------------------------------------------------------------------------
# 汇总入口
# ---------------------------------------------------------------------------
def _summarize(perf: Optional[Dict[str, Any]], sec: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    parts, ok = [], True
    if perf is not None:
        if perf.get("enabled"):
            parts.append(f"性能 {'通过' if perf['passed'] else '未通过'}"
                         f"（P95 {perf['overall']['p95_ms']}ms / "
                         f"错误率 {perf['overall']['error_rate'] * 100:.2f}% / "
                         f"{perf['overall']['rps']} rps）")
            ok = ok and bool(perf["passed"])
        else:
            parts.append("性能 未执行")
    if sec is not None:
        if sec.get("enabled"):
            parts.append(f"安全 {sec['passed_count']} 通过 / {sec['failed']} 失败"
                         + (f" / {sec['warned']} 提示" if sec["warned"] else ""))
            ok = ok and bool(sec["passed"])
        else:
            parts.append("安全 未执行")
    return ok, " · ".join(parts)


def run_all(project_path: Path, regression_path: Optional[Path] = None,
            out_json: Optional[Path] = None, only: Optional[str] = None,
            users: Optional[int] = None, iterations: Optional[int] = None,
            args: Optional[argparse.Namespace] = None) -> Dict[str, Any]:
    """执行性能与安全冒烟。only ∈ {None, "perf", "security"}。"""
    if requests is None:
        raise RuntimeError("需要 requests，请先安装依赖")
    _load_dotenv()   # 必须在 _resolve_auth 之前：凭据来自 .env（密钥分离）
    project_path = Path(project_path)
    project = _load_yaml(project_path)
    regression = None
    if regression_path and Path(regression_path).is_file():
        regression = _load_yaml(Path(regression_path))
    elif (project_path.parent / "regression.yaml").is_file():
        regression = _load_yaml(project_path.parent / "regression.yaml")

    base_url = (project.get("env", {}) or {}).get("base_url", "")
    auth = _resolve_auth(project)
    auth_header, auth_diag = _obtain_auth_header(base_url, auth)
    if auth_diag.get("ok"):
        print(f"  [认证] 已获取 {auth_diag.get('mode')} 凭据，受保护接口将携带 token")
    elif auth_diag.get("reason"):
        print(f"  [认证] {auth_diag['reason']}")

    if args is None:
        args = argparse.Namespace(users=users, iterations=iterations)
    elif users is not None:
        args.users = users
    if args is not None and iterations is not None:
        args.iterations = iterations
    cfg = _perf_config(project, args)

    # 基线校验：目标确实是我们的应用且凭据有效时，才做依赖认证的判定
    env_ok, env_reason = _baseline_ok(base_url, auth, auth_diag)
    print(f"  [基线] {'✅ ' if env_ok else '⚠ '}{env_reason}")

    perf = sec = None
    if only != "security":
        perf = (run_perf(project, base_url, auth, auth_header, regression, cfg)
                if env_ok else _skipped_perf(cfg, env_reason))
        if not env_ok:
            print(f"  [性能冒烟] 跳过：{env_reason}")
    if only != "perf":
        if env_ok:
            sec = run_security(project, base_url, auth, regression)
        else:
            sec = _skipped_security(env_reason, _protected_endpoints(project, regression))
            print(f"  [安全冒烟] 跳过：{env_reason}")

    all_pass, summary = _summarize(perf, sec)
    out: Dict[str, Any] = {
        "project_id": project.get("project_id") or project_path.parent.name,
        "base_url": base_url,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "baseline": {"ok": env_ok, "reason": env_reason},
        "auth": {k: v for k, v in auth_diag.items() if k != "token"},
        "perf": perf if perf is not None else {"enabled": False},
        "security": sec if sec is not None else {"enabled": False},
        "all_pass": all_pass,
        "summary": summary,
    }

    # ---- 部分重跑：未跑的那一侧沿用上次结果，不要把它洗掉 ----
    # 与 regression 的 rerun_one 同一原则：只跑一项时，另一项的既有结论应保留，
    # 否则「只跑安全」会把刚测出的性能数据静默抹掉，门禁结论也跟着失真。
    stale: List[str] = []
    if only and out_json and Path(out_json).is_file():
        try:
            prev = json.loads(Path(out_json).read_text(encoding="utf-8")) or {}
        except Exception:
            prev = {}
        for key in ("perf", "security"):
            if key in (only,):
                continue
            pv = prev.get(key)
            if isinstance(pv, dict) and pv.get("enabled"):
                out[key] = pv
                out[f"{key}_from_previous"] = prev.get("generated_at")
                stale.append(key)
        if stale:
            all_pass, summary = _summarize(out.get("perf"), out.get("security"))
            out["all_pass"], out["summary"] = all_pass, summary
            out["stale_sections"] = stale
            print(f"  [说明] 本次只跑了 {only}；{ '、'.join(stale) } 沿用上次结果"
                  f"（{prev.get('generated_at')}），门禁按两侧合并判定")

    if out_json:
        Path(out_json).parent.mkdir(parents=True, exist_ok=True)
        Path(out_json).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  结果已写出 {out_json}")
    print(f"\n  == 性能与安全冒烟：{'✅ 通过' if all_pass else '❌ 未通过'} ==\n  {summary}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="性能 + 安全 冒烟执行器（线程池，零新增依赖）")
    ap.add_argument("--project", required=True, help="project.yaml 路径")
    ap.add_argument("--regression", help="regression.yaml 路径（缺失时自动在同目录寻找）")
    ap.add_argument("--json", help="输出 JSON 结果路径")
    ap.add_argument("--only", choices=["perf", "security"], help="只跑其中一项")
    ap.add_argument("--users", type=int, help="并发数（覆盖 project.yaml 配置）")
    ap.add_argument("--iterations", type=int, help="每用户请求次数（覆盖配置）")
    args = ap.parse_args()
    res = run_all(Path(args.project),
                  Path(args.regression) if args.regression else None,
                  Path(args.json) if args.json else None,
                  only=args.only, users=args.users, iterations=args.iterations,
                  args=args)
    sys.exit(0 if res["all_pass"] else 1)


if __name__ == "__main__":
    main()
