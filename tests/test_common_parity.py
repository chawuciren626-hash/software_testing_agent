"""防漂移：跨扩展共享的「唯一实现层」必须真的唯一。

背景（`docs/HARNESS_ARCHITECTURE_REVIEW.md` §4.1 / §7 序 2）
----------------------------------------------------------
同一件事曾在多个扩展里各自复刻：`_load_yaml` ×3、`_resolve_auth` ×3、
`load_dotenv` ×2（还有一处兜底副本）、门禁谓词 `failed==0 and passed>0` 多处、
cases.md 行解析 ×3。复刻不会立刻报错，但任一处改了规则、另一处没跟上就
**静默不一致** —— 表现成最熟悉的那种"改了没效果"，而且不会有任何测试变红。

本测试就是那条"会变红"的线，守三件事：
1. **身份**：各调用方拿到的是 `extensions/common` 的**同一个函数对象**（`is` 判定），
   而不是"看起来一样"的第二份实现；
2. **唯一性**：AST 扫描 `extensions/`，这些函数的 `def` 定义数必须各为 1；
3. **行为**：一批输入逐字段比对"调用方输出"与 `common` 输出相等。

任何一处被重新复制出去，这三类断言立刻变红。
"""
from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT
EXT = ROOT / "extensions"

# 自足地准备导入路径（不依赖其他测试文件是否先跑过）。
for _sub in ("", "extensions", "extensions/regression", "extensions/perf_security",
             "extensions/web_testing", "extensions/requirements_to_cases",
             "extensions/memory", "extensions/reporting"):
    _p = str(ROOT / _sub) if _sub else str(ROOT)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import common.auth as ca          # noqa: E402
import common.cases as cc         # noqa: E402
import common.data as cdata       # noqa: E402
import common.gates as cg         # noqa: E402
import common.yamlio as cy        # noqa: E402


def _import(name: str):
    return importlib.import_module(name)


# --------------------------------------------------------------------------- #
# 1) AST 唯一性
# --------------------------------------------------------------------------- #
def _def_count(func_names):
    hits = []
    for p in sorted(EXT.rglob("*.py")):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        except SyntaxError as e:      # 不该发生；明确报出好过静默跳过
            raise AssertionError(f"无法解析 {p}: {e}")
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name in func_names:
                hits.append(f"{p.relative_to(REPO)}:{node.lineno} def {node.name}")
    return hits


@pytest.mark.parametrize("names,label", [
    ({"resolve_auth", "_resolve_auth"}, "resolve_auth"),
    ({"load_yaml", "_load_yaml"}, "load_yaml"),
    ({"load_dotenv", "_load_dotenv", "_fallback_load_dotenv"}, "load_dotenv"),
    ({"parse_rows", "_parse_rows"}, "cases.parse_rows"),
])
def test_shared_functions_have_single_definition(names, label):
    hits = _def_count(names)
    assert len(hits) == 1, (
        f"{label} 应只有 1 处 def 定义（extensions/common/），实际 {len(hits)} 处：{hits}"
    )


# --------------------------------------------------------------------------- #
# 2) 身份同一（拿到的是同一个函数对象）
# --------------------------------------------------------------------------- #
def test_executors_alias_the_shared_functions():
    rr = _import("run_regression")
    rw = _import("run_web")
    ps = _import("run_perf_security")

    for mod in (rr, rw, ps):
        assert mod._resolve_auth is ca.resolve_auth, f"{mod.__name__}._resolve_auth 不是共享实现"
        assert mod._load_yaml is cy.load_yaml, f"{mod.__name__}._load_yaml 不是共享实现"
    # load_dotenv 的名字各模块不一（run_regression 用公开名），但必须是同一个对象
    assert rr.load_dotenv is ca.load_dotenv
    assert rw._load_dotenv is ca.load_dotenv
    assert ps._load_dotenv is ca.load_dotenv
    assert rr._substitute is cdata.substitute and rw._substitute is cdata.substitute
    assert rr._dig is cdata.dig and ps._dig is cdata.dig
    assert rr._all_pass is cg.all_pass and rw._all_pass is cg.all_pass


def test_cases_consumers_alias_shared_parser():
    assert _import("focus")._parse_rows is cc.parse_rows
    assert _import("case_quality").parse_rows is cc.parse_rows


# --------------------------------------------------------------------------- #
# 3) 行为逐字段相等
# --------------------------------------------------------------------------- #
_AUTH_BATTERY = [
    {},
    {"name": "只有名字，没有 env"},
    {"env": {}},
    {"env": {"auth": {}}},
    {"env": {"auth": {"type": "form", "login_url": "/admin/login",
                      "username_env": "STA_T_USER", "password_env": "STA_T_PASS",
                      "token_env": "STA_T_TOKEN", "token_field": "access_token"}}},
    {"env": {"auth": {"username_env": "STA_T_MISSING_XYZ",
                      "password_env": "STA_T_MISSING_ABC"}}},
]


@pytest.fixture()
def _auth_env(monkeypatch):
    monkeypatch.setenv("STA_T_USER", "u-123")
    monkeypatch.setenv("STA_T_PASS", "p-456")
    monkeypatch.setenv("STA_T_TOKEN", "t-789")


@pytest.mark.parametrize("project", _AUTH_BATTERY)
def test_resolve_auth_parity(project, _auth_env):
    expected = ca.resolve_auth(project)
    assert set(expected) == {"type", "login_url", "token_field", "username", "password", "token"}
    for mod in (_import("run_regression"), _import("run_web"), _import("run_perf_security")):
        assert mod._resolve_auth(project) == expected, f"{mod.__name__} 输出与共享实现不一致"


def test_resolve_auth_reads_env_and_defaults(monkeypatch):
    monkeypatch.setenv("STA_T_USER", "alice")
    monkeypatch.setenv("STA_T_PASS", "s3cret")
    got = ca.resolve_auth({"env": {"auth": {"username_env": "STA_T_USER",
                                            "password_env": "STA_T_PASS"}}})
    assert got["username"] == "alice" and got["password"] == "s3cret"
    assert got["type"] == "none" and got["token_field"] == "token" and got["token"] == ""
    # 未声明 env 名 -> 空串（不是 None，更不是报错）
    assert ca.resolve_auth({"env": {"auth": {"username_env": "STA_T_MISSING_XYZ"}}})["username"] == ""


def test_load_yaml_parity(tmp_path):
    f = tmp_path / "x.yaml"
    f.write_text("a: 1\nb:\n  - x\n  - y\n", encoding="utf-8")
    expected = {"a": 1, "b": ["x", "y"]}
    for fn in (cy.load_yaml, _import("run_regression")._load_yaml,
               _import("run_web")._load_yaml, _import("run_perf_security")._load_yaml):
        assert fn(f) == expected
    empty = tmp_path / "e.yaml"
    empty.write_text("", encoding="utf-8")
    assert cy.load_yaml(empty) == {}


def test_substitute_and_dig_parity():
    auth = {"username": "u", "password": "p"}
    body = {"a": "{{username}}", "b": ["x", "{{password}}"], "c": 1}
    expected = {"a": "u", "b": ["x", "p"], "c": 1}
    assert cdata.substitute(body, auth) == expected
    assert _import("run_regression")._substitute(body, auth) == expected
    assert cdata.dig({"data": {"token": "t"}}, "data.token") == "t"
    assert cdata.dig({"data": {}}, "data.token") is None
    assert cdata.dig([], "data") is None


# --------------------------------------------------------------------------- #
# 4) 门禁谓词（全 SKIP 不判绿）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("failed,passed,expected", [
    (0, 3, True),      # 正常通过
    (1, 3, False),     # 有失败
    (0, 0, False),     # 全 SKIP —— 关键：不判绿
    (2, 0, False),     # 全失败
])
def test_gate_predicate_truth_table(failed, passed, expected):
    assert cg.all_pass(failed, passed) is expected


# --------------------------------------------------------------------------- #
# 5) cases.md 行解析（把"统一后的语义"钉住）
# --------------------------------------------------------------------------- #
def test_cases_parse_rows_pads_short_rows():
    md = ("| id | 标题 | 步骤 |\n|---|---|---|\n"
          "| A-1 | 登录 | 1. 登录 |\n"
          "| A-2 | 退出 |\n")           # 第二行少一列 -> 右侧补空串
    rows = cc.parse_rows(md)
    assert len(rows) == 2
    assert rows[0] == {"id": "A-1", "标题": "登录", "步骤": "1. 登录"}
    assert rows[1]["id"] == "A-2" and rows[1]["步骤"] == ""


def test_cases_parse_rows_stops_at_table_end():
    md = ("| id | 标题 |\n|---|---|\n| A-1 | 登录 |\n"
          "\n## 缺口小节\n\n| 别的 | 表 |\n|---|---|\n| 不应 | 计入 |\n")
    rows = cc.parse_rows(md)
    assert [r["id"] for r in rows] == ["A-1"]      # 后文另一张表不被吞入


def test_cases_parse_rows_empty():
    assert cc.parse_rows("") == []
    assert cc.parse_rows("# 无表格\n正文\n") == []


def test_cases_parity_across_consumers():
    md = "| id | 标题 |\n|---|---|\n| A-1 | 登录 |\n"
    assert (_import("focus")._parse_rows(md)
            == _import("case_quality").parse_rows(md)
            == cc.parse_rows(md))
