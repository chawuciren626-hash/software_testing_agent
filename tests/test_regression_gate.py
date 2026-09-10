"""核心回归门禁单测：防假绿（状态码200但业务失败）、环境不可达不判绿、合并重跑。

通过 monkeypatch requests 模拟被测服务，不发起真实网络请求。
"""
import json
from pathlib import Path

import pytest

import run_regression as rr


class FakeResp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload, ensure_ascii=False)

    def json(self):
        return self._payload


class FakeReq:
    def __init__(self, resp):
        self.resp = resp

    def get(self, *a, **k):
        return self.resp

    def request(self, *a, **k):
        return self.resp

    def post(self, *a, **k):
        return self.resp


@pytest.fixture
def project_file(tmp_path):
    p = tmp_path / "project.yaml"
    p.write_text("project_id: t\nenv:\n  base_url: http://fake.local\n", encoding="utf-8")
    return p


@pytest.fixture
def regression_file(tmp_path):
    p = tmp_path / "regression.yaml"
    p.write_text(
        "core_business:\n"
        "  - name: 登录\n"
        "    type: api_smoke\n"
        "    method: POST\n"
        "    path: /admin/login\n"
        "    expect_status: 200\n"
        "    expect_json:\n"
        "      code: 200\n",
        encoding="utf-8",
    )
    return p


def _run(project_file, regression_file, fake, tmp_path, monkeypatch, only=None):
    monkeypatch.setattr(rr, "requests", FakeReq(fake))
    out = tmp_path / "reg.json"
    return rr.run_regression(project_file, regression_file, out, repo_root=tmp_path, only=only)


def test_pass_when_status_and_body_ok(project_file, regression_file, tmp_path, monkeypatch):
    s = _run(project_file, regression_file, FakeResp(200, {"code": 200}), tmp_path, monkeypatch)
    assert s["all_pass"] is True
    assert s["results"][0]["result"] == "PASS"


def test_false_green_blocked(project_file, regression_file, tmp_path, monkeypatch):
    """状态码恒为200、但业务码=500：必须判 FAIL，不能假绿。"""
    s = _run(project_file, regression_file, FakeResp(200, {"code": 500}), tmp_path, monkeypatch)
    assert s["all_pass"] is False
    assert s["results"][0]["result"] == "FAIL"
    assert "code" in (s["results"][0].get("detail") or "")


def test_unreachable_not_green(project_file, regression_file, tmp_path, monkeypatch):
    """环境不可达（全部 SKIP）：不能判绿。"""
    class Boom:
        def get(self, *a, **k): raise ConnectionError("unreachable")
        def request(self, *a, **k): raise ConnectionError("unreachable")
        def post(self, *a, **k): raise ConnectionError("unreachable")
    monkeypatch.setattr(rr, "requests", Boom())
    s = rr.run_regression(project_file, regression_file, tmp_path / "reg.json",
                          repo_root=tmp_path)
    assert s["all_pass"] is False
    assert s["results"][0]["result"] == "SKIP"


def test_rerun_one_merges(tmp_path, monkeypatch):
    """rerun_one 应合并回既有结果，而非覆盖完整报告。"""
    # 既有报告：2 个场景（一成一败）
    prior = {
        "project_id": "t", "base_url": "http://fake.local",
        "total": 2, "passed": 1, "failed": 1, "skipped": 0, "all_pass": False,
        "results": [
            {"name": "登录", "result": "FAIL", "detail": "old"},
            {"name": "列表", "result": "PASS"},
        ],
    }
    out = tmp_path / "reg.json"
    out.write_text(json.dumps(prior, ensure_ascii=False), encoding="utf-8")

    # 让 run_regression（rerun_one 内部调用）返回"登录"重跑为 PASS
    def fake_run(*a, **k):
        return {"project_id": "t", "base_url": "http://fake.local",
                "total": 1, "passed": 1, "failed": 0, "skipped": 0,
                "all_pass": True,
                "results": [{"name": "登录", "result": "PASS", "detail": "ok"}]}
    monkeypatch.setattr(rr, "run_regression", fake_run)

    merged = rr.rerun_one(tmp_path / "project.yaml", tmp_path / "regression.yaml",
                          out, "登录", repo_root=tmp_path)
    # 总数应为 2（保留了"列表"），登录被更新为 PASS
    assert merged["total"] == 2
    names = {r["name"]: r["result"] for r in merged["results"]}
    assert names["登录"] == "PASS"
    assert names["列表"] == "PASS"
    # 全通过 -> 门禁转绿
    assert merged["all_pass"] is True
