"""核心业务回归执行器。

读取项目的 regression.yaml（核心业务场景声明）与 project.yaml（环境/认证），
对被测项目环境发起真实请求，校验状态码与响应体，输出结构化结果（JSON + 控制台摘要）。

认证（密钥分离）：project.yaml 仅声明环境变量名（如 MALL_ADMIN_USER），
真实口令 / token 取自 .env（不入库）。

用法：
    python run_regression.py --project projects/mall-admin/project.yaml \
                             --regression projects/mall-admin/regression.yaml \
                             --json projects/mall-admin/artifacts/regression.json
也可被 project_manager 作为模块导入：run_regression(project_path, regression_path, out_json)。

支持的回归项 type：
  api_smoke      声明式 HTTP 检查（method / path / expect_status）
  pytest_marker  复用已有 pytest 用例，按 marker 筛选（target + marker，如 -m smoke）
  pytest_node    复用已有 pytest 用例，指定 node id（node: path::test_name）
后两者把"已经写好的自动化用例"直接变成核心回归项，避免重复声明、保证回归与用例同源。

api_smoke 的断言能力（除 expect_status 外）：
  expect_json     校验响应体字段，支持点路径（如 data.token）；
                  取值 "__not_null__" 表示该字段必须非空。
  expect_contains 校验响应体文本须包含某子串。
⚠ 很多后端（如 mall-admin）HTTP 状态码恒为 200、成败写在 body 的业务码里，
  只断言状态码会产生"永远通过"的假绿，务必配合 expect_json 使用。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None
try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


def _load_yaml(path: Path) -> Dict[str, Any]:
    if yaml is None:
        raise RuntimeError("需要 PyYAML，请先安装依赖：pip install pyyaml")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_dotenv() -> Optional[Path]:
    """载入仓库根 .env（密钥分离：只有此文件里有真实口令，且已被 gitignore）。

    ⚠ 为什么必须显式提供：本模块既被 project_manager 调用（它启动时已载入 .env），
    也会被**独立运行**（`python run_regression.py --project ...`）或从别的扩展调用。
    后两种情况下如果没人载入 .env，`_resolve_auth` 取到的是空口令，
    登录必然失败 → 受保护接口全部 401 → 表现为"回归大面积失败"，
    极难排查（实际只是凭据没加载）。因此独立入口必须先调用本函数。
    用 setdefault 语义：不覆盖已存在的环境变量，CI 里显式注入的值优先。
    """
    roots = []
    if os.environ.get("STA_ROOT"):
        roots.append(Path(os.environ["STA_ROOT"]))
    roots.append(Path(__file__).resolve().parents[2])   # extensions/regression/x.py -> 仓库根
    for root in roots:
        p = root / ".env"
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        return p
    return None


def _resolve_auth(project: Dict[str, Any]) -> Dict[str, Any]:
    """从 project.yaml 解析认证上下文；口令/token 取自环境变量（密钥分离）。"""
    env = project.get("env", {}) or {}
    auth = env.get("auth", {}) or {}
    return {
        "type": auth.get("type", "none"),
        "login_url": auth.get("login_url", ""),
        "token_field": auth.get("token_field", "token"),
        "username": os.getenv(auth["username_env"], "") if auth.get("username_env") else "",
        "password": os.getenv(auth["password_env"], "") if auth.get("password_env") else "",
        "token": os.getenv(auth["token_env"], "") if auth.get("token_env") else "",
    }


def _login(base_url: str, auth: Dict[str, Any]) -> Optional[str]:
    """form 登录，成功返回 token，失败返回 None。"""
    if not auth.get("login_url"):
        return None
    url = base_url.rstrip("/") + auth["login_url"]
    try:
        r = requests.post(
            url,
            json={"username": auth["username"], "password": auth["password"]},
            timeout=10,
        )
    except Exception as e:  # pragma: no cover
        print(f"  [login] 请求失败：{e}", file=sys.stderr)
        return None
    if r.status_code >= 400:
        print(f"  [login] 状态码 {r.status_code}：{r.text[:200]}", file=sys.stderr)
        return None
    try:
        data = r.json()
    except Exception:
        return None
    token = data.get(auth["token_field"]) or (data.get("data") or {}).get(auth["token_field"])
    return token


def _substitute(body: Any, auth: Dict[str, Any]) -> Any:
    """把 {{username}} / {{password}} 占位替换为凭据。"""
    if isinstance(body, str):
        return body.replace("{{username}}", auth["username"]).replace("{{password}}", auth["password"])
    if isinstance(body, dict):
        return {k: _substitute(v, auth) for k, v in body.items()}
    if isinstance(body, list):
        return [_substitute(v, auth) for v in body]
    return body


def _dig(data: Any, dotted: str) -> Any:
    """按点路径取嵌套字段，如 data.token -> json['data']['token']。"""
    cur = data
    for part in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


PYTEST_SUMMARY_RE = re.compile(r"(\d+)\s+(passed|failed|error|skipped)")


def _run_pytest(args: List[str], cwd: Path, base_url: str) -> Dict[str, Any]:
    """以本项目测试环境运行 pytest，返回统计结果。

    复用已有自动化用例（默认 extensions/api_testing）作为回归项，
    通过 BASE_URL 指向本项目环境；环境不可达时用例会自行 skip（不误判为失败）。
    """
    env = os.environ.copy()
    env["BASE_URL"] = base_url
    # 打包成 exe 时 sys.executable 指向 exe，需回落到真实 python（见 project_manager.python_exe）
    try:
        import project_manager as _pm
        py = _pm.python_exe()
    except Exception:
        py = sys.executable
    proc = subprocess.run(
        [py, "-m", "pytest", *args],
        cwd=str(cwd), env=env, capture_output=True, text=True,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    counts: Dict[str, int] = {}
    for n, kind in PYTEST_SUMMARY_RE.findall(out):
        counts[kind] = counts.get(kind, 0) + int(n)
    return {"returncode": proc.returncode, "counts": counts, "tail": out[-1500:]}


def run_regression(
    project_path: Path,
    regression_path: Path,
    out_json: Optional[Path] = None,
    repo_root: Optional[Path] = None,
    only: Optional[str] = None,
) -> Dict[str, Any]:
    """执行核心业务回归。

    only: 只跑指定名称的场景（用于「单场景重跑」）。
          ⚠ 注意：only 模式下若直接传 out_json，写出的 JSON 只会含这一个场景，
          会把完整报告"洗掉"。需要保留完整结果请改用 rerun_one()（合并写入）。
    """
    if requests is None:
        raise RuntimeError("需要 requests，请先安装依赖")
    project = _load_yaml(project_path)
    reg = _load_yaml(regression_path)
    base_url = (project.get("env", {}) or {}).get("base_url", "")
    auth = _resolve_auth(project)

    bearer: Optional[str] = None
    if auth["type"] == "form":
        tok = _login(base_url, auth)
        if tok:
            bearer = tok
            print(f"  [auth] 登录成功，已获取 token（{auth['token_field']}）")
        else:
            print("  [auth] 登录失败，仅执行无需认证的用例", file=sys.stderr)
    elif auth["type"] == "bearer":
        bearer = auth["token"] or None

    cwd = Path(repo_root) if repo_root else Path.cwd()
    items = reg.get("core_business", []) or []
    if only:
        items = [it for it in items if str(it.get("name", "")) == only]
        if not items:
            msg = f"未找到名为 {only!r} 的回归场景，请检查 regression.yaml 中 core_business[].name"
            print(f"  [错误] {msg}", file=sys.stderr)
            return {"project_id": project.get("project_id"), "base_url": base_url,
                    "total": 0, "passed": 0, "failed": 0, "skipped": 0,
                    "all_pass": False, "results": [], "error": msg}
    results: List[Dict[str, Any]] = []
    for it in items:
        name = it.get("name", "?")
        itype = it.get("type", "api_smoke")

        # ---- 复用已有 pytest 用例（与用例同源，推荐）----
        if itype in ("pytest_marker", "pytest_node"):
            target = it.get("target", "extensions/api_testing")
            marker = it.get("marker")
            node = it.get("node")
            if node:
                pargs = [node, "-q"]
                desc = node
            else:
                pargs = [target, "-q"]
                if marker:
                    pargs += ["-m", marker]
                desc = target + (f" -m {marker}" if marker else "")
            pr = _run_pytest(pargs, cwd, base_url)
            c = pr["counts"]
            n_passed = c.get("passed", 0)
            n_failed = c.get("failed", 0) + c.get("error", 0)
            n_skipped = c.get("skipped", 0)
            if n_failed > 0:
                result = "FAIL"
            elif n_passed > 0:
                result = "PASS"
            else:
                result = "SKIP"      # 环境不可达导致全部跳过：不算失败，但会告警
            results.append({
                "name": name, "type": itype, "method": "pytest", "url": desc,
                "expect": "全部通过",
                "status_code": f"passed={n_passed}, failed={n_failed}, skipped={n_skipped}",
                "result": result, "snippet": pr["tail"],
            })
            print(f"  [{result}] {name} [pytest {desc}] -> "
                  f"{n_passed} passed / {n_failed} failed / {n_skipped} skipped")
            continue

        # ---- 声明式 HTTP 检查 ----
        method = (it.get("method") or "GET").upper()
        path = it.get("path", "/")
        url = base_url.rstrip("/") + path
        expect = it.get("expect_status", 200)
        need_auth = it.get("auth") == "required"
        body = _substitute(it.get("body"), auth)
        headers = {"Content-Type": "application/json"}
        if need_auth and bearer:
            headers["Authorization"] = f"Bearer {bearer}"

        rec: Dict[str, Any] = {"name": name, "type": itype,
                               "method": method, "url": url, "expect": expect}
        try:
            if method == "GET":
                resp = requests.get(url, params=body, headers=headers, timeout=10)
            else:
                resp = requests.request(method, url, json=body, headers=headers, timeout=10)
        except Exception as e:  # 连不上（非 HTTP 响应）：按"环境不可达"跳过并告警
            rec.update(status_code="UNREACHABLE", result="SKIP", snippet=str(e)[:200])
            results.append(rec)
            print(f"  [SKIP] {name} [{method} {path}] -> 环境不可达")
            continue

        # 断言 = 状态码 + 响应体。
        # 关键：不少后端（如 mall-admin）HTTP 状态码恒为 200，成败写在响应体的业务码里，
        # 只看状态码会产生"永远通过"的假绿，因此必须支持 expect_json / expect_contains。
        fails: List[str] = []
        if resp.status_code != expect:
            fails.append(f"状态码 {resp.status_code} != {expect}")

        expect_json: Dict[str, Any] = it.get("expect_json") or {}
        if expect_json:
            try:
                payload = resp.json()
            except Exception:
                payload = None
                fails.append("响应体不是合法 JSON")
            if payload is not None:
                for key, want in expect_json.items():
                    got = _dig(payload, key)
                    if str(want) == "__not_null__":
                        if got in (None, "", [], {}):
                            fails.append(f"{key} 为空（期望非空）")
                    elif not (got == want or str(got) == str(want)):
                        fails.append(f"{key}={got!r}（期望 {want!r}）")

        contains = it.get("expect_contains")
        if contains and contains not in resp.text:
            fails.append(f"响应体缺少 {contains!r}")

        exp_desc = str(expect) + (f" + {expect_json}" if expect_json else "")
        rec.update(status_code=resp.status_code,
                   result="PASS" if not fails else "FAIL",
                   expect=exp_desc, detail="；".join(fails),
                   snippet=resp.text[:200])
        results.append(rec)
        line = f"  [{rec['result']}] {name} [{method} {path}] -> {rec['status_code']} (期望 {exp_desc})"
        if fails:
            line += f"\n        失败原因：{rec['detail']}"
        print(line)

    passed = sum(1 for r in results if r["result"] == "PASS")
    failed = sum(1 for r in results if r["result"] == "FAIL")
    skipped = sum(1 for r in results if r["result"] == "SKIP")
    summary = {
        "project_id": project.get("project_id"),
        "base_url": base_url,
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        # 回归门禁：无 FAIL 且【确有实际执行】才算通过。
        # 全 SKIP（环境不可达）不判绿，避免 CI 给出虚假的安全信号。
        "all_pass": failed == 0 and passed > 0,
        "results": results,
    }
    if out_json:
        out_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  结果已写出 {out_json}")
    if passed == 0:
        print("  ⚠ 本次回归没有任何实际执行的通过项"
              "（环境不可达 / 未声明回归项），门禁按未通过处理。", file=sys.stderr)
    return summary


def rerun_one(
    project_path: Path,
    regression_path: Path,
    out_json: Path,
    name: str,
    repo_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """只重跑一个场景，并把结果**合并回**既有 regression.json。

    为什么不直接 run_regression(only=name, out_json=...)：
      那样写出的 JSON 只含这一个场景，会把其它场景的执行记录全部冲掉，
      报告中心/看板会瞬间从"完整回归结果"退化成"一条记录"，门禁结论也失真。
    这里改为：执行该场景 → 在既有 results 中按 name 定位并替换（没有则追加）
    → 重新统计 total/passed/failed/skipped 与门禁 all_pass → 写回。
    """
    fresh = run_regression(project_path, regression_path, None,
                           repo_root=repo_root, only=name)
    if fresh.get("error") or not fresh.get("results"):
        print(f"  [重跑] 失败：{fresh.get('error') or '未获得执行结果'}", file=sys.stderr)
        return fresh

    new_rec = dict(fresh["results"][0])
    new_rec["rerun_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    merged: Dict[str, Any] = {}
    if Path(out_json).is_file():
        try:
            merged = json.loads(Path(out_json).read_text(encoding="utf-8")) or {}
        except Exception:
            merged = {}
    merged.setdefault("project_id", fresh.get("project_id"))
    merged["base_url"] = fresh.get("base_url")

    results: List[Dict[str, Any]] = list(merged.get("results") or [])
    idx = next((i for i, r in enumerate(results)
                if str(r.get("name", "")) == name), None)
    if idx is None:
        results.append(new_rec)
        action = "新增"
    else:
        results[idx] = new_rec
        action = "更新"

    passed = sum(1 for r in results if r.get("result") == "PASS")
    failed = sum(1 for r in results if r.get("result") == "FAIL")
    skipped = sum(1 for r in results if r.get("result") == "SKIP")
    merged["results"] = results
    merged["total"], merged["passed"] = len(results), passed
    merged["failed"], merged["skipped"] = failed, skipped
    merged["all_pass"] = failed == 0 and passed > 0   # 全 SKIP 不判绿（防假绿）
    merged["last_rerun"] = {"name": name, "result": new_rec.get("result"),
                            "at": new_rec["rerun_at"]}

    Path(out_json).write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"  [重跑] {action}场景「{name}」-> {new_rec.get('result')}；"
          f"汇总 通过 {passed}/{len(results)}（失败 {failed}，跳过 {skipped}）"
          f"{' ✅' if merged['all_pass'] else ' ❌'}")
    if new_rec.get("detail"):
        print(f"         说明：{new_rec['detail']}")
    return merged


def main() -> None:
    ap = argparse.ArgumentParser(description="核心业务回归执行器")
    ap.add_argument("--project", required=True, help="project.yaml 路径")
    ap.add_argument("--regression", required=True, help="regression.yaml 路径")
    ap.add_argument("--json", help="输出 JSON 结果路径")
    ap.add_argument("--only", help="只跑指定名称的场景；配合 --json 时会合并回既有结果（不覆盖完整报告）")
    args = ap.parse_args()
    load_dotenv()   # 独立运行时必须自行载入 .env，否则凭据为空导致大面积假失败
    if args.only and args.json:
        summary = rerun_one(Path(args.project), Path(args.regression),
                            Path(args.json), args.only)
        sys.exit(0 if summary.get("all_pass") else 1)
    summary = run_regression(
        Path(args.project), Path(args.regression),
        Path(args.json) if args.json else None,
        only=args.only,
    )
    sys.exit(0 if summary["all_pass"] else 1)


if __name__ == "__main__":
    main()
