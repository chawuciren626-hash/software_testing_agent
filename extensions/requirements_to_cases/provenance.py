"""生成溯源（provenance）：给每一次「需求→用例」的产物盖上来源与置信度标记。

为什么需要它
------------
用例文件（cases.md）经常被单独发出去评审、贴进缺陷单、发给外包执行。
一旦它离开 ``artifacts/``，``run_meta.json`` 里的 ``mode/use_llm`` 就跟它脱节了 ——
读的人看到一张用例表，完全无从判断：

- 这是 LLM 写的，还是规则版模板套出来的？
- 如果是规则版，**是因为没配 key，还是 LLM 调用失败降级了**？
- 用了哪个模型？注入了哪些上下文（情景记忆 / 项目知识）？

**「没配 key 走规则版」和「LLM 超时降级成规则版」在产物上长得一模一样，
但前者是预期行为，后者意味着这次生成的质量低于预期、值得查。**
产物自己说不清这件事，就只能去翻日志 —— 而日志早被下一次 run 冲掉了。

所以：**标记必须写在产物里**（stamp），不能只存在 run_meta 里。
本模块是唯一的写入与回读入口，避免出现第二套口径。

置信度怎么给（诚实口径）
------------------------
**不编造百分比。** 分两层，互不冒充：

- **定性**：每种生成模式给出固定的「可信度 + 必须人工做什么」，见下表。
- **定量**：不另设评分，只**引用**同批产出的结构质量分（``case_quality``，实测值）。
  质量分算不出时记为 ``None`` 并显示"未计分"，不拿别的数凑。

============  ==========  ====================================================
模式           可信度       必须人工做什么
============  ==========  ====================================================
rule           低           结构确定、内容泛化：步骤/预期是模板占位，必须逐条细化
llm            中           内容具体：可能引入需求之外的假设，需核对业务口径
llm-agentic    中高         同上，但多一轮自评审，覆盖度实测更高（见 README 评测）
（任一模式）   低（降级）    LLM 没干活，产物等同规则版，**要查原因**
============  ==========  ====================================================

⚠️ 关于「中高」的依据：取自本项目 ``tests/eval/quality_bench.py`` 的一次实测
（qwen-max、repeat=3：多步 96.0 > 单步 94.3 >> 规则版 79.7）。
**它是参考，不是承诺** —— 换模型、换需求集都可能变，且 judge 判分本身有方差。
"""

from __future__ import annotations

import datetime
import json
import re
from typing import Any, Dict, List, Optional

SCHEMA_V = 1
MARK = "STA-PROVENANCE"

# 机器可读标记：藏在 HTML 注释里 —— 渲染时不可见，但原始文件里可回读。
_MARK_RE = re.compile(r"<!--\s*" + MARK + r"\s+v(\d+)\s*(\{.*?\})\s*-->", re.S)

MODE_RULE = "rule"
MODE_LLM = "llm"
MODE_AGENTIC = "llm-agentic"

_MODE_TABLE: Dict[str, Dict[str, str]] = {
    MODE_RULE: {
        "label": "规则版",
        "confidence": "低",
        "caveat": "结构确定、内容泛化：步骤/预期是模板占位，必须逐条人工细化",
    },
    MODE_LLM: {
        "label": "LLM 增强",
        "confidence": "中",
        "caveat": "内容具体可用，但可能引入需求之外的假设，需核对业务口径",
    },
    MODE_AGENTIC: {
        "label": "LLM 多步自审",
        "confidence": "中高",
        "caveat": "多一轮自评审，覆盖度实测更高；仍需核对业务口径",
    },
}

DEGRADED_CONFIDENCE = "低（降级）"


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def mode_of(use_llm: bool, agentic: bool = False) -> str:
    """由调用参数推导模式标识。降级与否是**另一维**，不并进模式里。"""
    if not use_llm:
        return MODE_RULE
    return MODE_AGENTIC if agentic else MODE_LLM


def build(*, mode: str,
          provider: Optional[str] = None,
          model: Optional[str] = None,
          degraded: bool = False,
          degrade_reason: str = "",
          injected: Optional[Dict[str, Any]] = None,
          source: str = "",
          case_count: Optional[int] = None,
          req_count: Optional[int] = None,
          quality: Optional[float] = None,
          ts: Optional[str] = None) -> Dict[str, Any]:
    """组装一份溯源信息。未知的一律留空/None —— **不猜、不补默认值充数**。"""
    t = _MODE_TABLE.get(mode, _MODE_TABLE[MODE_RULE])
    # 降级原因来自异常文本，是唯一的自由文本字段。它若含 "-->" 会提前闭合
    # HTML 注释，导致整个标记被截断、回读失败 —— 宁可改字，也不能让标记丢。
    reason = (degrade_reason or "").strip().replace("\n", " ")[:300].replace("-->", "->")
    return {
        "v": SCHEMA_V,
        "mode": mode,
        "mode_label": t["label"],
        "confidence": DEGRADED_CONFIDENCE if degraded else t["confidence"],
        "caveat": t["caveat"],
        "provider": provider or "",
        "model": model or "",
        "degraded": bool(degraded),
        "degrade_reason": reason,
        "injected": dict(injected or {}),
        "source": source or "",
        "case_count": case_count,
        "req_count": req_count,
        "quality": quality,
        "ts": ts or _now(),
    }


# ---------------------------------------------------------------- 文案

def describe(meta: Dict[str, Any]) -> str:
    """一行人类可读的「怎么生成的」。例：``LLM 增强（qwen-max · openai 兼容）``。"""
    m = meta or {}
    label = m.get("mode_label") or _MODE_TABLE[MODE_RULE]["label"]
    parts = [label]
    if m.get("model"):
        prov = m.get("provider")
        parts.append(f"{m['model']} · {prov} 兼容" if prov else str(m["model"]))
    if m.get("degraded"):
        parts.append("⚠️ 降级")
    return "（".join(parts) + ("）" if len(parts) > 1 else "")


def _inject_text(inj: Dict[str, Any]) -> str:
    names = {"lessons": "情景记忆", "knowledge": "项目知识"}
    out = []
    for k, v in (inj or {}).items():
        if v:
            out.append(f"{names.get(k, k)} {v} {'条' if k == 'lessons' else '段'}")
    return " · ".join(out)


def render_text(meta: Optional[Dict[str, Any]]) -> str:
    """给 CLI / 通知用的多行摘要。"""
    if not meta:
        return "生成溯源：未知（旧版产物，未带标记）"
    m = meta
    lines = [f"生成方式：{describe(m)}", f"可信度：{m.get('confidence', '未知')}"]
    if m.get("injected"):
        lines.append("上下文注入：" + _inject_text(m["injected"]))
    if m.get("quality") is not None:
        lines.append(f"结构质量分：{m['quality']}/100")
    if m.get("degraded"):
        lines.append(f"⚠️ 降级原因：{m.get('degrade_reason') or '未记录'}")
    return "\n".join(lines)


_CONF_COLOR = {"低（降级）": "#c62828", "低": "#ef6c00", "中": "#1565c0", "中高": "#2e7d32"}
_CONF_ICON = {"低（降级）": "⚠️", "低": "🔸", "中": "🔷", "中高": "✅"}


def badge(meta: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """给控制台 / 报告用的徽章（不含 HTML，由调用方决定怎么渲染）。"""
    if not meta:
        return {"label": "未知来源", "color": "#757575", "icon": "❔",
                "title": "这份用例早于溯源功能生成，来源未知"}
    conf = str(meta.get("confidence") or "")
    return {
        "label": describe(meta),
        "color": _CONF_COLOR.get(conf, "#757575"),
        "icon": _CONF_ICON.get(conf, "🔹"),
        "title": f"可信度：{conf} —— {meta.get('caveat', '')}".strip(),
    }


# ---------------------------------------------------------------- 写入（stamp）

def render_header(meta: Dict[str, Any]) -> List[str]:
    """生成 Markdown 头部若干行。**顺序有讲究**：``_parse_cases`` 只看前 8 行
    里的 ``- 来源 / 生成时间 / 用例数``，所以这几项必须靠前。"""
    m = dict(meta or {})
    lines = ["# 测试用例（由需求生成）"]
    if m.get("source"):
        lines.append(f"- 来源：{m['source']}")
    lines.append(f"- 生成时间：{m.get('ts') or _now()}")
    if m.get("case_count") is not None:
        lines.append(f"- 用例数：{m['case_count']}")
    lines.append(f"- 生成方式：{describe(m)} · 可信度：{m.get('confidence', '未知')}")
    if m.get("injected"):
        txt = _inject_text(m["injected"])
        if txt:
            lines.append(f"- 上下文注入：{txt}")
    if m.get("quality") is not None:
        lines.append(f"- 结构质量分：{m['quality']}/100")
    if m.get("degraded"):
        reason = m.get("degrade_reason") or "原因未记录"
        lines.append(f"- ⚠️ 降级产出：{reason} —— 已退回规则版，"
                     f"步骤/预期为模板占位，必须人工细化")
    if m.get("caveat"):
        lines.append(f"> 可信度说明：{m.get('confidence', '')} —— {m['caveat']}")
    payload = json.dumps(m, ensure_ascii=False, sort_keys=True)
    lines.append(f"<!-- {MARK} v{SCHEMA_V} {payload} -->")
    return lines


def _is_header_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return True     # 头部块内的空行一并吃掉
    if s.startswith("# "):
        return True
    if _MARK_RE.search(s):
        return True
    for prefix in ("- 来源：", "- 生成时间：", "- 用例数：", "- 生成方式：",
                   "- 上下文注入：", "- 结构质量分：", "- ⚠️ 降级产出："):
        if s.startswith(prefix):
            return True
    return s.startswith("> 可信度说明：")


def strip_header(md: str) -> str:
    """去掉（可能存在的）旧头部块，保证重复 stamp 不会越叠越高。"""
    lines = (md or "").splitlines()
    i = 0
    while i < len(lines) and _is_header_line(lines[i]):
        i += 1
    return "\n".join(lines[i:])


def stamp(md: str, meta: Dict[str, Any]) -> str:
    """把溯源头部盖到用例 Markdown 上。幂等：已盖过会先剥掉再重盖。"""
    body = strip_header(md)
    head = "\n".join(render_header(meta))
    return head + "\n\n" + body.lstrip("\n")


def attach_quality(md: str, score: Optional[float]) -> str:
    """生成后补记结构质量分。没有标记的原样返回 —— **不给旧产物硬盖章**。"""
    meta = parse(md)
    if meta is None:
        return md
    meta["quality"] = score
    return stamp(md, meta)


# ---------------------------------------------------------------- 回读（parse）

def parse(md: str) -> Optional[Dict[str, Any]]:
    """从用例 Markdown 回读溯源信息；没有标记返回 ``None``。

    两档读取：
    1. HTML 注释里的 JSON（主要途径，字段完整）；
    2. 注释被渲染器/复制粘贴吃掉时，从 ``- 生成方式：`` 等文本行回读，
       此时 ``partial=True`` —— **降级回读要说清自己不完整**，
       否则调用方会拿着残缺数据当完整数据用。
    """
    if not md:
        return None
    m = _MARK_RE.search(md)
    if m:
        try:
            data = json.loads(m.group(2))
            if isinstance(data, dict):
                data["v"] = int(m.group(1))
                return data
        except (ValueError, TypeError):
            pass
    return _parse_plain(md)


def _parse_plain(md: str) -> Optional[Dict[str, Any]]:
    found: Dict[str, Any] = {}
    for line in (md or "").splitlines()[:12]:
        s = line.strip()
        if s.startswith("- 生成方式："):
            found["mode_label"] = s[len("- 生成方式："):].split("·")[0].strip()
            if "⚠️ 降级" in s:
                found["degraded"] = True
                found["confidence"] = DEGRADED_CONFIDENCE
            if "可信度：" in s:
                found["confidence"] = s.split("可信度：")[-1].strip()
        elif s.startswith("- 用例数："):
            try:
                found["case_count"] = int(s[len("- 用例数："):].strip())
            except ValueError:
                pass
        elif s.startswith("- 上下文注入："):
            found["_inject"] = s[len("- 上下文注入："):].strip()
        elif s.startswith("- 结构质量分："):
            try:
                found["quality"] = float(s[len("- 结构质量分："):].split("/")[0].strip())
            except ValueError:
                pass
        elif s.startswith("- ⚠️ 降级产出："):
            found["degraded"] = True
            found["degrade_reason"] = s[len("- ⚠️ 降级产出："):].split("——")[0].strip()
        elif s.startswith("- 来源："):
            found["source"] = s[len("- 来源："):].strip()
        elif s.startswith("- 生成时间："):
            found["ts"] = s[len("- 生成时间："):].strip()
    if not found:
        return None
    found["partial"] = True
    found.setdefault("confidence", "未知")
    found.setdefault("caveat", "")
    found.setdefault("mode", "")
    return found


def is_degraded(md: str) -> bool:
    """只关心一件事：这批用例是不是 LLM 没干活的降级产物。"""
    meta = parse(md)
    return bool(meta and meta.get("degraded"))


def main() -> None:  # pragma: no cover - 手工排查用
    import argparse
    ap = argparse.ArgumentParser(description="查看/校验 cases.md 的生成溯源标记")
    ap.add_argument("cases", help="cases.md 路径")
    args = ap.parse_args()
    with open(args.cases, encoding="utf-8") as f:
        md = f.read()
    meta = parse(md)
    if meta is None:
        print("未发现溯源标记（旧版产物或文件被改过）")
        return
    print(render_text(meta))
    if meta.get("partial"):
        print("\n(残缺回读：机器可读标记已丢失，以上信息由文本行还原)")


if __name__ == "__main__":  # pragma: no cover
    main()
