"""需求/PR → 结构化测试用例生成器（规则版为主，可选 LLM 增强）。

设计：
- 纯标准库实现规则版，无需 LLM 即可跑（适合 CI / 离线）。
- 解析需求文本中的条目（编号/项目符号列表），为每条需求生成
  功能 / 边界 / 异常 三类用例，输出符合 agent-skills/requirements-to-cases 字段规范的 Markdown。
- 可选 LLM 增强：加 --llm 可改用 LLM 做更智能的生成；通过 LLM_PROVIDER 选择厂商，
  任何失败（缺 key / 限流 / 网络）都会自动降级回规则版，绝不中断流水线。
  * 默认 provider=openai：走 OpenAI 兼容协议（DeepSeek / 通义千问 / 智谱 GLM / Kimi / 本地 Ollama 等），
    配置 LLM_API_KEY + LLM_BASE_URL + LLM_MODEL。
  * provider=gemini：走 Google Generative Language API，配置 GOOGLE_API_KEY（+可选 GEMINI_API_BASE / GEMINI_MODEL）。

用法：
    python generate_cases.py --input sample_requirements.md --output cases.md
    python generate_cases.py --input pr_description.txt            # 输出到 stdout
    python generate_cases.py --input req.md --llm                  # 启用 LLM 增强（默认 openai provider）
    LLM_PROVIDER=gemini GOOGLE_API_KEY=xxx python generate_cases.py --input req.md --llm
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
import time
from typing import Dict, List, Optional

import requests  # Gemini REST 调用零额外依赖


def parse_requirements(text: str) -> List[str]:
    """从需求文本中抽取条目（编号 1. / 1) 或 - / * 开头的行）。"""
    items: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):        # 跳过 Markdown 标题
            continue
        m = re.match(r"^(\d+[.)]|[+\-*])\s+(.*)", line)
        if m:
            items.append(m.group(2).strip())
        elif len(items) == 0 and len(line) > 6:
            # 非列表但像一句需求描述，作为单条兜底
            items.append(line)
    return [i for i in items if i]


def _case(rid: str, title: str, rtype: str, priority: str, pre: str, steps: str, expected: str) -> Dict:
    return {
        "id": rid,
        "title": title,
        "module": "待定",
        "type": rtype,
        "priority": priority,
        "preconditions": pre,
        "steps": steps,
        "expected": expected,
        "automated": "可（pytest/Playwright）",
    }


def gen_cases(items: List[str]) -> List[Dict]:
    cases: List[str] = []  # type: ignore
    out: List[Dict] = []
    for i, req in enumerate(items, 1):
        rid = f"REQ-{i:03d}"
        base = req.rstrip("。. ")
        out.append(_case(f"{rid}-F", f"{base}（功能）", "功能", "P1",
                         "系统已部署且可访问", f"1. 按需求执行：{base}", "功能按预期正常完成，无报错"))
        out.append(_case(f"{rid}-B", f"{base}（边界）", "边界", "P2",
                         "准备边界输入数据", f"1. 使用边界值（最小/最大/临界）执行：{base}", "边界条件下系统处理正确，无越界/崩溃"))
        out.append(_case(f"{rid}-N", f"{base}（异常）", "异常", "P1",
                         "准备异常/非法输入", f"1. 输入非法/缺失数据执行：{base}", "系统给出明确错误提示，不发生脏数据/异常中断"))
    return out


def to_markdown(cases: List[Dict], source: str) -> str:
    lines = [
        f"# 测试用例（由需求生成）",
        f"- 来源：{source}",
        f"- 生成时间：{datetime.datetime.now():%Y-%m-%d %H:%M}",
        f"- 用例数：{len(cases)}",
        "",
        "| id | 标题 | 模块 | 类型 | 优先级 | 前置 | 步骤 | 预期 | 可自动化 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for c in cases:
        lines.append(
            f"| {c['id']} | {c['title']} | {c['module']} | {c['type']} | {c['priority']} "
            f"| {c['preconditions']} | {c['steps']} | {c['expected']} | {c['automated']} |"
        )
    return "\n".join(lines) + "\n"


class LLMError(Exception):
    """LLM 调用失败（缺 key / 限流 / 网络 / 空响应等）。调用方应降级到规则版。"""


class GeminiError(LLMError):
    """Gemini 调用失败；向后兼容别名（LLMError 的子类）。"""


# ---- Gemini 配置（provider=gemini 时使用）----
# 免费套餐默认模型：gemini-2.5-flash（质量与免费额度的最佳平衡；可用 GEMINI_MODEL 覆盖）
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

# ---- OpenAI 兼容配置（provider=openai 时使用，默认）----
# 覆盖绝大多数国产/开源模型：DeepSeek / 通义千问 Qwen / 智谱 GLM / Kimi / MiniMax / 本地 Ollama 等，
# 它们都兼容 /v1/chat/completions。可用 LLM_MODEL 指定具体模型名。
DEFAULT_OPENAI_MODEL = "deepseek-chat"
# 本地 Ollama 默认 base（无需 key，可经 LLM_BASE_URL 覆盖）
LOCAL_BASE_HINT = ("localhost", "127.0.0.1")

# 要求 LLM 输出与规则版完全一致的表头，保证下游 _parse_cases / 报告渲染直接复用
GEMINI_PROMPT = """你是一名资深测试工程师。请把下面的需求/PR 描述转换为结构化测试用例，
严格按下面的 Markdown 表格格式输出，**只输出表格本身，不要任何额外说明文字**：

| id | 标题 | 模块 | 类型 | 优先级 | 前置 | 步骤 | 预期 | 可自动化 |
|---|---|---|---|---|---|---|---|---|

要求：
- 为每条需求生成 功能 / 边界 / 异常 三类用例，id 形如 REQ-001-F / REQ-001-B / REQ-001-N。
- 类型只能取：功能 / 边界 / 异常；优先级取 P1 或 P2。
- 可自动化：可（pytest/Playwright）或 暂不可。
- 步骤用 "1. ... 2. ..." 编号；预期描述可观测的结果。
- 表头字段顺序与上面完全一致，且“标题/类型/优先级/可自动化”等关键列必须保留。
- 若需求含接口信息，可在步骤中写入具体请求方法、路径与断言要点。

需求：
{text}"""


def gemini_generate(text: str, api_key: Optional[str] = None,
                    model: Optional[str] = None, timeout: int = 60) -> str:
    """调用 Gemini REST API 生成用例 Markdown（零额外依赖，仅用 requests）。

    针对免费套餐做了：指数退避重试（应对 429 限流）、5xx 重试、空响应保护。
    任何不可恢复错误都抛 GeminiError，由调用方降级到规则版。
    """
    api_key = api_key or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise GeminiError("缺少 GOOGLE_API_KEY（免费套餐也可用 AI Studio key）")
    model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
    # 标准端点为 Google Generative Language API；若 key 来自代理网关/受限项目，
    # 可用 GEMINI_API_BASE 指向其网关（结尾不带斜杠）。
    base = os.environ.get("GEMINI_API_BASE", "https://generativelanguage.googleapis.com").rstrip("/")
    url = f"{base}/v1beta/models/{model}:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": GEMINI_PROMPT.format(text=text)}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8192},
    }

    last_err = "未知错误"
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=timeout)
        except requests.RequestException as e:
            last_err = f"网络异常: {e}"
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429:
            # 免费套餐常见：退避后重试；优先读 Retry-After 头
            retry_after = r.headers.get("retry-after")
            try:
                wait = int(retry_after) if retry_after else 2 ** attempt + 1
            except ValueError:
                wait = 2 ** attempt + 1
            last_err = f"429 限流 (第{attempt + 1}次)，{wait}s 后重试"
            time.sleep(min(wait, 30))
            continue
        if r.status_code >= 500:
            last_err = f"{r.status_code} 服务端错误"
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 400:
            # 常见：key 无效 / 模型名不支持 / 内容被拦截
            detail = ""
            try:
                detail = r.json().get("error", {}).get("message", "")
            except Exception:
                pass
            raise GeminiError(f"400 请求被拒：{detail or r.text[:200]}")
        if r.status_code == 403:
            raise GeminiError("403 无权限（key 无效或未开通 Generative Language API）")
        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            last_err = str(e)
            time.sleep(2 ** attempt)
            continue
        try:
            data = r.json()
        except ValueError:
            raise GeminiError("返回非 JSON")
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        out = "".join(p.get("text", "") for p in parts).strip()
        if not out:
            raise GeminiError("空响应（可能被安全策略拦截）")
        return out
    raise GeminiError(f"重试后仍失败：{last_err}")


def openai_compatible_generate(text: str, api_key: Optional[str] = None,
                               base_url: Optional[str] = None,
                               model: Optional[str] = None,
                               timeout: int = 60) -> str:
    """调用 OpenAI 兼容 Chat Completions 接口生成用例 Markdown（零额外依赖，仅用 requests）。

    适用于 DeepSeek / 通义千问 / 智谱 GLM / Kimi / MiniMax / 本地 Ollama 等所有兼容
    /v1/chat/completions 的厂商。针对限流(429)/5xx 做指数退避重试；任何不可恢复错误
    都抛 LLMError，由调用方降级到规则版。本地 Ollama（base 含 localhost/127.0.0.1）
    可不配 key。
    """
    api_key = api_key or os.environ.get("LLM_API_KEY")
    model = model or os.environ.get("LLM_MODEL") or DEFAULT_OPENAI_MODEL
    base = (base_url or os.environ.get("LLM_BASE_URL") or os.environ.get("LLM_API_BASE")
            or "https://api.openai.com/v1").rstrip("/")
    url = f"{base}/chat/completions"
    is_local = any(h in base for h in LOCAL_BASE_HINT)
    if not api_key and not is_local:
        raise LLMError("缺少 LLM_API_KEY（OpenAI 兼容平台 key；本地 Ollama 可省略）")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是一名资深软件测试工程师，请严格按用户要求的表格格式输出测试用例。"},
            {"role": "user", "content": GEMINI_PROMPT.format(text=text)},
        ],
        "temperature": 0.2,
    }

    last_err = "未知错误"
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            last_err = f"网络异常: {e}"
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429:
            retry_after = r.headers.get("retry-after")
            try:
                wait = int(retry_after) if retry_after else 2 ** attempt + 1
            except ValueError:
                wait = 2 ** attempt + 1
            last_err = f"429 限流 (第{attempt + 1}次)，{wait}s 后重试"
            time.sleep(min(wait, 30))
            continue
        if r.status_code >= 500:
            last_err = f"{r.status_code} 服务端错误"
            time.sleep(2 ** attempt)
            continue
        if r.status_code in (401, 403):
            raise LLMError(f"{r.status_code} 认证/权限失败（检查 LLM_API_KEY 或账号权限）")
        if r.status_code == 404:
            raise LLMError(f"404 接口/模型不存在（检查 LLM_BASE_URL 与 LLM_MODEL）：{r.text[:200]}")
        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            last_err = str(e)
            time.sleep(2 ** attempt)
            continue
        try:
            data = r.json()
        except ValueError:
            raise LLMError("返回非 JSON")
        out = (data.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
        if not out:
            raise LLMError("空响应（可能被内容策略拦截）")
        return out
    raise LLMError(f"重试后仍失败：{last_err}")


def llm_configured() -> bool:
    """当前 provider 是否已具备调用条件（用于 Web 控制台显示 LLM 可用性）。"""
    provider = os.environ.get("LLM_PROVIDER", "openai").lower()
    if provider == "gemini":
        return bool(os.environ.get("GOOGLE_API_KEY"))
    if os.environ.get("LLM_API_KEY"):
        return True
    base = os.environ.get("LLM_BASE_URL") or os.environ.get("LLM_API_BASE") or ""
    return any(h in base for h in LOCAL_BASE_HINT)


def llm_generate(text: str, provider: Optional[str] = None,
                 api_key: Optional[str] = None, base_url: Optional[str] = None,
                 model: Optional[str] = None, timeout: int = 60) -> str:
    """统一 LLM 入口：按 provider 分发（默认 openai 兼容，可选 gemini）。

    仅负责调用，不处理降级；降级由 generate_from_text 统一兜底。
    """
    provider = (provider or os.environ.get("LLM_PROVIDER", "openai")).lower()
    if provider == "gemini":
        return gemini_generate(text, api_key=api_key, model=model, timeout=timeout)
    return openai_compatible_generate(text, api_key=api_key, base_url=base_url,
                                      model=model, timeout=timeout)


def generate_from_text(text: str, use_llm: bool = False,
                       provider: Optional[str] = None,
                       api_key: Optional[str] = None,
                       base_url: Optional[str] = None,
                       model: Optional[str] = None,
                       source: str = "需求文本",
                       extra_context: Optional[str] = None) -> str:
    """需求文本 -> 用例 Markdown。

    use_llm=True 时优先走 LLM（provider 由 LLM_PROVIDER 决定，默认 openai 兼容）；
    任何失败（缺 key / 限流 / 异常）都自动降级到零依赖规则版并打日志，绝不中断流水线。

    extra_context: 情景记忆注入（历史易错点重点覆盖清单）。LLM 版直接拼进 prompt 让其
    推理加强覆盖；规则版只在用例表格后追加建议段（规则版无法自动推理，需人工/LLM 版增强），
    不会污染需求解析（parse_requirements 跳过标题行）。无 extra_context 时行为与原来一致。
    """
    if use_llm:
        try:
            prompt = text
            if extra_context:
                prompt = text + "\n\n# 历史易错点（重点覆盖）\n" + extra_context
            return llm_generate(prompt, provider=provider, api_key=api_key,
                                base_url=base_url, model=model)
        except LLMError as e:
            print(f"  [需求->用例] LLM 增强失败，已自动降级为规则版：{e}")
    items = parse_requirements(text)  # 规则版只用原始需求，extra_context 不进解析
    if not items:
        return "# 未解析到需求条目。请使用编号/项目符号列表书写需求。\n"
    md = to_markdown(gen_cases(items), source)
    if extra_context:
        md += (
            "\n\n## 历史易错点重点覆盖建议\n"
            "> 以下为历史回归失败根因，建议补充覆盖（规则版无法自动推理，"
            "需人工或 LLM 版增强）：\n\n" + extra_context + "\n"
        )
    return md


def main() -> None:
    ap = argparse.ArgumentParser(description="需求 → 测试用例生成器")
    ap.add_argument("--input", "-i", help="需求文件路径（缺省读 stdin）")
    ap.add_argument("--output", "-o", help="输出 Markdown 路径（缺省 stdout）")
    ap.add_argument("--llm", action="store_true",
                    help="启用 LLM 增强生成（默认 openai 兼容 provider；失败自动降级规则版）")
    ap.add_argument("--provider", choices=["openai", "gemini"], default=None,
                    help="LLM 厂商（覆盖 LLM_PROVIDER 环境变量；默认 openai）")
    args = ap.parse_args()

    if args.input:
        with open(args.input, encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()

    result = generate_from_text(text, use_llm=args.llm, provider=args.provider,
                                source=args.input or "stdin")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(result)
        print(f"已写出 {args.output}")
    else:
        print(result)


if __name__ == "__main__":
    main()
