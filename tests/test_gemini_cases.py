"""P1: Gemini 用例增强（含免费套餐 429 退避与规则版降级）单测。

不依赖真实网络/真实 key：一律用 FakeRequests 模拟生成语言 API 的响应。
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "extensions" / "requirements_to_cases"))
import generate_cases as gc  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            from requests import HTTPError
            raise HTTPError(f"{self.status_code}")

    def __repr__(self):
        return f"<FakeResponse {self.status_code}>"


GOOD_JSON = {
    "candidates": [{"content": {"parts": [{"text": "| id | 标题 | 模块 | 类型 | 优先级 | 前置 | 步骤 | 预期 | 可自动化 |\n|---|---|---|---|---|---|---|---|---|\n| REQ-001-F | 登录（功能） | 认证 | 功能 | P1 | 已部署 | 1. 输入正确账号 | 登录成功 | 可（pytest/Playwright） |"}]}}]}

# OpenAI 兼容协议（/v1/chat/completions）的成功响应，与 GOOD_JSON 等价的用例内容
OPENAI_GOOD_JSON = {
    "choices": [{"message": {"content": "| id | 标题 | 模块 | 类型 | 优先级 | 前置 | 步骤 | 预期 | 可自动化 |\n|---|---|---|---|---|---|---|---|---|\n| REQ-001-F | 登录（功能） | 认证 | 功能 | P1 | 已部署 | 1. 输入正确账号 | 登录成功 | 可（pytest/Playwright） |"}}]}


def make_fake(sequence):
    """sequence: 可被 next() 的响应迭代器，模拟多次 post 的返回。"""
    class FakeRequests:
        def __init__(self, seq):
            self._seq = iter(seq)
            self.calls = 0
        def post(self, *a, **k):
            self.calls += 1
            try:
                return next(self._seq)
            except StopIteration:
                return FakeResponse(200, GOOD_JSON)
    return FakeRequests(sequence)


# ---------------------------------------------------------------------------
# gemini_generate 单测
# ---------------------------------------------------------------------------
def test_gemini_generate_success(monkeypatch):
    monkeypatch.setattr(gc, "requests", make_fake([FakeResponse(200, GOOD_JSON)]))
    out = gc.gemini_generate("用户可登录系统", api_key="dummy")
    assert "REQ-001-F" in out
    assert "登录" in out


def test_gemini_generate_429_then_success(monkeypatch):
    # 免费套餐典型：首次 429 带 retry-after，退避后第二次成功
    seq = [FakeResponse(429, headers={"retry-after": "0"}), FakeResponse(200, GOOD_JSON)]
    monkeypatch.setattr(gc, "requests", make_fake(seq))
    out = gc.gemini_generate("用户可登录系统", api_key="dummy")
    assert "REQ-001-F" in out


def test_gemini_generate_403_raises(monkeypatch):
    monkeypatch.setattr(gc, "requests", make_fake([FakeResponse(403, text="forbidden")]))
    with pytest.raises(gc.GeminiError):
        gc.gemini_generate("x", api_key="dummy")


def test_gemini_generate_400_raises(monkeypatch):
    monkeypatch.setattr(gc, "requests", make_fake([FakeResponse(400, text="bad key")]))
    with pytest.raises(gc.GeminiError):
        gc.gemini_generate("x", api_key="dummy")


def test_gemini_generate_persistent_429_raises(monkeypatch):
    # 连续 3 次 429 后放弃
    monkeypatch.setattr(gc, "requests", make_fake([FakeResponse(429, headers={"retry-after": "0"}) for _ in range(3)]))
    with pytest.raises(gc.GeminiError):
        gc.gemini_generate("x", api_key="dummy")


def test_gemini_generate_no_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(gc.GeminiError):
        gc.gemini_generate("x", api_key=None)


def test_gemini_generate_custom_base(monkeypatch):
    captured = {}
    class FakeReq:
        def post(self, url, **k):
            captured["url"] = url
            return FakeResponse(200, GOOD_JSON)
    monkeypatch.setattr(gc, "requests", FakeReq())
    monkeypatch.setenv("GEMINI_API_BASE", "https://proxy.example.com/")
    gc.gemini_generate("x", api_key="dummy")
    assert captured["url"].startswith("https://proxy.example.com/v1beta/models/")
    assert "key=dummy" in captured["url"]


# ---------------------------------------------------------------------------
# generate_from_text 调度 + 降级单测
# ---------------------------------------------------------------------------
def test_generate_from_text_rule_when_no_llm():
    out = gc.generate_from_text("1. 用户可登录\n2. 用户可登出", use_llm=False)
    assert "| REQ-001-F |" in out
    assert "| REQ-001-B |" in out
    assert "| REQ-001-N |" in out


def test_generate_from_text_llm_success(monkeypatch):
    # 默认 provider=openai，走 OpenAI 兼容格式
    monkeypatch.setattr(gc, "requests", make_fake([FakeResponse(200, OPENAI_GOOD_JSON)]))
    out = gc.generate_from_text("用户可登录系统", use_llm=True, api_key="dummy")
    assert "REQ-001-F" in out
    assert "登录" in out


def test_generate_from_text_fallback_on_403(monkeypatch, capsys):
    # 开启 LLM 但 Gemini 报 403 -> 自动降级到规则版，不抛异常
    monkeypatch.setattr(gc, "requests", make_fake([FakeResponse(403, text="forbidden")]))
    out = gc.generate_from_text("1. 用户可登录", use_llm=True, api_key="dummy")
    assert "| REQ-001-F |" in out  # 规则版产物
    captured = capsys.readouterr().out
    assert "降级" in captured  # 打印了降级日志


def test_generate_from_text_fallback_on_429(monkeypatch, capsys):
    monkeypatch.setattr(gc, "requests", make_fake([FakeResponse(429, headers={"retry-after": "0"}) for _ in range(3)]))
    out = gc.generate_from_text("1. 用户可登录", use_llm=True, api_key="dummy")
    assert "| REQ-001-F |" in out
    assert "降级" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 集成：真实 key 缺失时即便 use_llm=True 也走规则版
# ---------------------------------------------------------------------------
def test_generate_from_text_llm_without_key_falls_back(monkeypatch, capsys):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    out = gc.generate_from_text("1. 用户可登录", use_llm=True, api_key=None)
    assert "| REQ-001-F |" in out


# ---------------------------------------------------------------------------
# openai_compatible_generate 单测（默认 provider）
# ---------------------------------------------------------------------------
def test_openai_generate_success(monkeypatch):
    monkeypatch.setattr(gc, "requests", make_fake([FakeResponse(200, OPENAI_GOOD_JSON)]))
    out = gc.openai_compatible_generate("用户可登录系统", api_key="dummy")
    assert "REQ-001-F" in out
    assert "登录" in out


def test_openai_generate_uses_base_url_and_key(monkeypatch):
    captured = {}
    class FakeReq:
        def post(self, url, **k):
            captured["url"] = url
            captured["auth"] = k["headers"].get("Authorization")
            return FakeResponse(200, OPENAI_GOOD_JSON)
    monkeypatch.setattr(gc, "requests", FakeReq())
    monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
    out = gc.openai_compatible_generate("x", api_key="sk-dummy")
    assert captured["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert captured["auth"] == "Bearer sk-dummy"
    assert "REQ-001-F" in out


def test_openai_generate_401_raises(monkeypatch):
    monkeypatch.setattr(gc, "requests", make_fake([FakeResponse(401, text="unauthorized")]))
    with pytest.raises(gc.LLMError):
        gc.openai_compatible_generate("x", api_key="dummy")


def test_openai_generate_404_raises(monkeypatch):
    monkeypatch.setattr(gc, "requests", make_fake([FakeResponse(404, text="not found")]))
    with pytest.raises(gc.LLMError):
        gc.openai_compatible_generate("x", api_key="dummy")


def test_openai_generate_429_then_success(monkeypatch):
    seq = [FakeResponse(429, headers={"retry-after": "0"}), FakeResponse(200, OPENAI_GOOD_JSON)]
    monkeypatch.setattr(gc, "requests", make_fake(seq))
    out = gc.openai_compatible_generate("x", api_key="dummy")
    assert "REQ-001-F" in out


def test_openai_generate_no_key_raises(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_BASE", raising=False)
    with pytest.raises(gc.LLMError):
        gc.openai_compatible_generate("x", api_key=None)


def test_openai_generate_local_ollama_no_key(monkeypatch):
    captured = {}
    class FakeReq:
        def post(self, url, **k):
            captured["url"] = url
            captured["has_auth"] = "Authorization" in k.get("headers", {})
            return FakeResponse(200, OPENAI_GOOD_JSON)
    monkeypatch.setattr(gc, "requests", FakeReq())
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    out = gc.openai_compatible_generate("x")  # 无 key
    assert captured["url"].startswith("http://localhost:11434/v1/chat/completions")
    assert captured["has_auth"] is False
    assert "REQ-001-F" in out


# ---------------------------------------------------------------------------
# llm_configured / llm_generate 分发单测
# ---------------------------------------------------------------------------
def test_llm_configured_openai_with_key(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", "sk-dummy")
    assert gc.llm_configured() is True


def test_llm_configured_gemini_with_key(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GOOGLE_API_KEY", "xxx")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert gc.llm_configured() is True


def test_llm_configured_openai_no_key_no_local(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_BASE", raising=False)
    assert gc.llm_configured() is False


def test_llm_generate_dispatches_to_openai(monkeypatch):
    called = {"openai": False}
    def fake_openai(text, **k):
        called["openai"] = True
        return "| REQ-001-F | ok |"
    monkeypatch.setattr(gc, "openai_compatible_generate", fake_openai)
    out = gc.llm_generate("x", provider="openai", api_key="dummy")
    assert called["openai"] is True and "REQ-001-F" in out


def test_llm_generate_dispatches_to_gemini(monkeypatch):
    called = {"gemini": False}
    def fake_gemini(text, **k):
        called["gemini"] = True
        return "| REQ-001-F | ok |"
    monkeypatch.setattr(gc, "gemini_generate", fake_gemini)
    out = gc.llm_generate("x", provider="gemini", api_key="dummy")
    assert called["gemini"] is True and "REQ-001-F" in out
