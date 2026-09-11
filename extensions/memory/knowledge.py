"""长期记忆：项目知识库（人工维护）+ 检索注入。

与 `lessons.py` 的区别（两者互补，不要合并）：

|            | lessons.md                | knowledge.md                    |
|------------|---------------------------|---------------------------------|
| 来源       | 流水线自动聚类失败项生成    | **人工撰写 / 维护**              |
| 时效       | 短期（最近失败的反映）      | 长期（业务规则、领域约定）        |
| 内容       | "哪些场景容易红"           | "密码 8-20 位""列表默认分页 10"   |
| 更新频率   | 每次 run 重写              | 人工按需改                       |

为什么要它
----------
同一条需求"用户可查看订单列表"，在一个有"分页默认 20 条、最多查 90 天"约定的项目里，
和一个没有约定的项目里，该写的边界用例完全不同。这类信息不会出现在需求文本里，
只会存在于团队的脑子里——knowledge.md 就是把它落到磁盘上，
并在每次生成用例时**按相关性**捞出来一起交给 LLM。

为什么必须检索而不是全文塞进去
------------------------------
knowledge.md 会越写越长。全文注入的后果是：① 挤占需求本身的注意力
② prompt 变长带来成本与超时（agentic 模式串行 4 次调用，更敏感）。
所以只注入与**本次需求**相关的 Top-K 段。

⚠️ 检索能力的诚实边界（重要）
--------------------------
这里用的是 **关键词 / 子串匹配（中文 2-gram）**，**不是语义向量检索**。
它认得出"密码"和"密码"，认不出"登录凭证"和"用户名密码"是同一件事。
把它宣传成"智能语义匹配"会让人高估能力，进而把漏注入当成玄学问题排查。
不确定时宁可不注入——零命中就不注入，不拿无关的段落充数。
"""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

KNOWLEDGE_FILE = "knowledge.md"

# 每个注入段落的字符上限。超长截断并标注——静默截断会让引用者不知道内容被阉割过。
_SECTION_LIMIT = 900
_DEFAULT_TOPK = 3
# 段落分数若低于最高分的这个比例就被丢弃（防止"凑数注入"，见 retrieve 的说明）
_MIN_RATIO = 0.3

# 无信息量的虚词：参与匹配只会制造"看起来命中了"的假象。
# 注意**不要**收录"以/可/能"等字，它们出现在"可以""以上"这类有信息或含边界的词里。
_STOP = set("的了是着过吗呢吧啊在和跟与及或而就把被对于之其该此那这")

_WS_TITLE = re.compile(r"^#{1,6}\s*(.+?)\s*$")
_NONWORD = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")
_ALNUM = re.compile(r"[0-9A-Za-z]+")


# --------------------------------------------------------------------------- #
# 加载与分段
# --------------------------------------------------------------------------- #
def load_knowledge(pdir: Path) -> Optional[str]:
    kp = Path(pdir) / KNOWLEDGE_FILE
    return kp.read_text(encoding="utf-8") if kp.is_file() else None


def _strip_quotes(body: str) -> str:
    """去掉 Markdown 引用行（`> ...`）：那类内容通常只是给编辑者看的说明。"""
    return "\n".join(ln for ln in body.splitlines()
                     if not ln.strip().startswith(">"))


def split_sections(md: str) -> List[Dict[str, Any]]:
    """按 `## ...` 标题切段。没有二级标题时整篇算一段（标题取首个一级标题或留空）。"""
    lines = (md or "").splitlines()
    sections: List[Dict[str, Any]] = []
    title, buf = "", []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if not body:
            return
        # 只收**有实质正文**的段：
        # ① 文档的一级标题（"# 项目知识"）往往只有标题或一句说明，没有知识内容；
        # ② 纯引用块（"> ..."）是给编辑者看的写法说明，不是知识。
        # 算进来会让"共 N 段知识"虚高，用户会以为有内容没被检索到。
        if not _strip_quotes(body).strip():
            return
        sections.append({"title": title, "body": body})

    for ln in lines:
        m = _WS_TITLE.match(ln)
        if m:
            if m.group(0).startswith("## ") or m.group(0).startswith("# "):
                flush()
                title, buf = m.group(1), []
                continue
        buf.append(ln)
    flush()
    return [s for s in sections if (s["body"] or s["title"])]


# --------------------------------------------------------------------------- #
# 检索
# --------------------------------------------------------------------------- #
def terms(text: str) -> List[str]:
    """把文本切成检索词：英文/数字整体成词 + 中文 2-gram。

    中文不做分词（无第三方依赖），用 2-gram 近似——"密码长度"会切成
    "密码/码长/长度"，查询里出现"密码"即可命中，够用且完全确定性。
    """
    if not text:
        return []
    cleaned = _NONWORD.sub(" ", text)
    out: List[str] = []
    for word in _ALNUM.findall(cleaned):
        if len(word) >= 2:
            out.append(word.lower())
    han = re.sub(r"[0-9A-Za-z ]+", "", cleaned).replace(" ", "")
    for i in range(len(han) - 1):
        gram = han[i:i + 2]
        if gram[0] in _STOP or gram[1] in _STOP:
            continue
        out.append(gram)
    return out


def retrieve(query: str, md: Optional[str],
             top_k: int = _DEFAULT_TOPK,
             min_ratio: float = _MIN_RATIO) -> List[Dict[str, Any]]:
    """按关键词重叠度挑出最相关的若干段落。

    打分 = 命中的查询词个数 + 2×标题命中数。用**去重后的词数**而非出现次数计分，
    否则长段落会靠重复词刷分。

    ⚠️ 相对分数门槛（关键）：只保留不低于最高分 `min_ratio` 的段落。
    没有这道门槛时，"顺便命中一个词"的无关段落也会被凑进来凑满 top_k ——
    例如需求讲的是"管理员登录"，讲分页的段落会因共有一个"用户"被带上，
    LLM 拿到无关上下文反而写歪。**宁可少给，不能给错。**
    """
    if not md or not md.strip():
        return []
    q_terms = set(terms(query or ""))
    if not q_terms:
        return []

    scored: List[Dict[str, Any]] = []
    for sec in split_sections(md):
        body_terms = set(terms(sec["body"]))
        title_terms = set(terms(sec["title"]))
        hit = q_terms & (body_terms | title_terms)
        if not hit:
            continue
        title_hit = q_terms & title_terms
        scored.append({
            "title": sec["title"],
            "body": sec["body"],
            "score": len(hit) + 2 * len(title_hit),
            "hits": sorted(hit)[:12],        # 便于 UI 解释"为什么选中这段"
            "title_hits": sorted(title_hit),
        })
    if not scored:
        return []

    best = max(s["score"] for s in scored)
    floor = max(2, math.ceil(best * min_ratio))
    kept = [s for s in scored if s["score"] >= floor]
    kept.sort(key=lambda h: (-h["score"], h["title"]))
    return kept[:max(1, top_k)]


def render(hits: Sequence[Dict[str, Any]]) -> str:
    """渲染成可拼进 prompt 的 Markdown 片段（标题以 # 开头，规则版解析时会跳过）。"""
    if not hits:
        return ""
    parts: List[str] = []
    for h in hits:
        head = f"## {h['title']}" if h["title"] else "## （无标题）"
        body = h["body"]
        if len(body) > _SECTION_LIMIT:
            body = body[:_SECTION_LIMIT].rstrip() + "\n…（该段过长，已截断）"
        parts.append(f"{head}\n{body}")
    return "# 项目知识（人工维护 · 本次需求相关片段）\n\n" + "\n\n".join(parts)


def to_inject_prompt(pdir: Path, query: str,
                     top_k: int = _DEFAULT_TOPK) -> Optional[str]:
    """取项目知识里与 query 相关的片段；无内容或无命中返回 None（不注入）。"""
    md = load_knowledge(pdir)
    if not md or not md.strip():
        return None
    if not (query or "").strip():
        # 没有需求文本可参照时不猜：宁可不注入，也不把整篇塞进去
        return None
    return render(retrieve(query, md, top_k=top_k)) or None


def explain(pdir: Path, query: str, top_k: int = _DEFAULT_TOPK) -> Dict[str, Any]:
    """返回可解释的检索结果（含分数与命中词），供 CLI / 控制台展示。

    检索这件事如果不解释，用户就只能"信或不信"；把命中词摆出来，
    用户立刻能判断"哦它认的是这个词，难怪漏了那段"。
    """
    md = load_knowledge(pdir)
    if not md or not md.strip():
        return {"has": False, "reason": f"没有 {KNOWLEDGE_FILE}", "picked": [],
                "total_sections": 0}
    sections = split_sections(md)
    picked = retrieve(query or "", md, top_k=top_k)
    return {
        "has": True,
        "reason": "",
        "total_sections": len(sections),
        "query_terms": sorted(set(terms(query or ""))),
        "picked": [{"title": p["title"], "score": p["score"],
                    "hits": p["hits"], "title_hits": p["title_hits"],
                    "body": p["body"]} for p in picked],
    }


TEMPLATE = """# 项目知识（人工维护）

> 这里写给 **LLM 和未来的同事** 看的项目特有约定 —— 需求文档里通常不会写、
> 但不知道就会写出错误用例的那些东西。每次生成用例时会自动挑选相关片段注入。
>
> 写法建议：一个二级标题一段主题，标题写清核心词（会参与检索匹配），
> 正文给具体取值。没有的段落删掉即可。

## 业务取值边界
<!-- 例：密码长度 8-20 位；用户名 4-16 位且只能字母数字下划线 -->

## 默认约定
<!-- 例：列表接口默认 pageSize=10，最大 100；金额单位统一为分 -->

## 环境与依赖
<!-- 例：登录态有效期 30 分钟；下单依赖库存服务，库存服务挂了会返回 503 -->

## 历史踩坑
<!-- 例：/admin/list 不带 token 时 HTTP 仍返回 200，成败要看响应体的 code 字段 -->
"""


def ensure_template(pdir: Path) -> Optional[Path]:
    """项目创建时生成一份带注释的模板（不存在才写，绝不覆盖用户内容）。"""
    kp = Path(pdir) / KNOWLEDGE_FILE
    if kp.is_file():
        return None
    kp.write_text(TEMPLATE, encoding="utf-8")
    return kp


def main() -> None:  # pragma: no cover - CLI 薄封装
    ap = argparse.ArgumentParser(description="项目知识库检索（长期记忆）")
    ap.add_argument("project", help="项目目录，如 projects/mall-admin")
    ap.add_argument("--query", "-q", default="", help="按该需求文本检索相关片段")
    ap.add_argument("--topk", type=int, default=_DEFAULT_TOPK)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--init", action="store_true", help="生成模板文件（已存在则不覆盖）")
    args = ap.parse_args()

    pdir = Path(args.project)
    if args.init:
        f = ensure_template(pdir)
        print("已生成：" + f if f else "已存在，未覆盖")
        return
    if args.json:
        print(json.dumps(explain(pdir, args.query, top_k=args.topk),
                         ensure_ascii=False, indent=2))
        return
    info = explain(pdir, args.query, top_k=args.topk)
    if not info["has"]:
        print(info["reason"])
        return
    picked = info["picked"]
    print(f"共 {info['total_sections']} 段知识；本次命中 {len(picked)} 段：\n")
    for p in picked:
        print(f"- {p['title'] or '（无标题）'}  score={p['score']}  命中词："
              f"{'、'.join(p['hits']) or '—'}")
    if not picked:
        print("没有相关片段（不会注入任何内容）")


if __name__ == "__main__":  # pragma: no cover
    main()
