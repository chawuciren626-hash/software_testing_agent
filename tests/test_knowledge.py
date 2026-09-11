"""长期记忆：项目知识库（extensions/memory/knowledge.py）的测试。

与 `lessons`（自动生成、短期）不同，`knowledge` 是人工维护的项目约定。
这里最容易被破坏的是**相对分数门槛**——没有它，只沾到一个无关共同词的段落
也会被凑进来，LLM 拿到无关上下文反而写歪用例。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "extensions" / "memory"))

import knowledge as kn  # noqa: E402
import project_manager as pm  # noqa: E402


MD = """# 项目知识（人工维护）

## 管理员登录
密码长度要求 8-20 位，用户名 4-16 位且只能字母数字下划线。
登录失败统一返回 code=500，HTTP 状态码恒为 200。

## 用户列表分页
列表接口默认 pageSize=10，最大 pageNum 不超过 100 页。超出范围返回空列表而非报错。

## 商品中心约定
商品上下架有延迟，最多 5 分钟生效。金额单位统一为分。
"""


# --------------------------------------------------------------------------- #
# 分段与分词
# --------------------------------------------------------------------------- #
def test_split_sections_by_h2():
    secs = kn.split_sections(MD)
    titles = [s["title"] for s in secs]
    assert titles == ["管理员登录", "用户列表分页", "商品中心约定"]
    assert "pageSize=10" in dict((s["title"], s["body"]) for s in secs)["用户列表分页"]


def test_split_sections_skips_explanation_only_parts():
    """文档标题下那段"写法建议"只是给编辑者看的引用说明，不该算成知识。

    否则界面上会显示"共 5 段"，而实际知识只有 4 段 —— 用户会以为有内容没被检索到。
    """
    md = "# 项目知识\n\n> 这里写给同事看的说明，不是知识内容。\n\n## 登录\n密码 8-20 位。\n"
    secs = kn.split_sections(md)
    assert [s["title"] for s in secs] == ["登录"]


def test_split_sections_handles_no_heading():
    secs = kn.split_sections("就这么一段纯文本，没有标题")
    assert len(secs) == 1 and secs[0]["title"] == ""
    assert "纯文本" in secs[0]["body"]


def test_terms_chinese_bigram_and_english_word():
    ts = kn.terms("登录 admin2 验证")
    assert "登录" in ts          # 中文 2-gram
    assert "admin2" in ts        # 英文/数字整体成词
    assert "的登录" not in ts     # 虚词造成的 gram 被过滤


def test_terms_filters_stopwords():
    """含虚词的 gram 会制造"看起来命中了"的假象，必须滤掉；实词要保留。"""
    ts = kn.terms("的场景")
    assert "场景" in ts                      # 实词保留
    assert "的的" not in ts and "的场" not in ts
    assert kn.terms("") == []


# --------------------------------------------------------------------------- #
# 检索
# --------------------------------------------------------------------------- #
def test_retrieve_picks_most_relevant_first():
    hits = kn.retrieve("管理员可以使用用户名密码登录系统，密码错误要有提示", MD)
    assert hits and hits[0]["title"] == "管理员登录"


def test_retrieve_title_match_is_weighted_higher():
    # 标题里带"分页"，正文里没有；同分时标题命中应排前
    hits = kn.retrieve("分页", MD)
    assert hits and hits[0]["title"] == "用户列表分页"


def test_relative_threshold_drops_noise():
    """回归：只沾到一个共同词的无关段落不能被凑进来凑满 top_k。

    需求讲"管理员登录"，讲分页的段落因为共有一个"用户"也被捎带上 ——
    这种噪音注入会让 LLM 写歪，比不注入更糟。
    """
    hits = kn.retrieve("管理员可以使用用户名密码登录系统，密码错误要有提示", MD)
    titles = [h["title"] for h in hits]
    assert titles == ["管理员登录"], f"混入了无关段落：{titles}"


def test_short_query_still_finds_something():
    """门槛不能高到让短需求什么都取不到。"""
    hits = kn.retrieve("登录", MD)
    assert [h["title"] for h in hits] == ["管理员登录"]


def test_unrelated_query_returns_nothing():
    assert kn.retrieve("导出年度财务报表 Excel", MD) == []


def test_empty_inputs_return_nothing():
    assert kn.retrieve("", MD) == []
    assert kn.retrieve("登录", "") == []
    assert kn.retrieve("登录", None) == []


def test_topk_limits_result_count():
    hits = kn.retrieve("用户 登录 商品 分页 密码 上下架", MD, top_k=1)
    assert len(hits) == 1


# --------------------------------------------------------------------------- #
# 注入
# --------------------------------------------------------------------------- #
def test_render_starts_with_h1_heading():
    """标题必须是 `#` 开头：规则版 parse_requirements 会跳过标题行，不污染需求解析。"""
    out = kn.render(kn.retrieve("登录", MD))
    assert out.startswith("# "), out[:20]
    assert "管理员登录" in out


def test_render_truncates_long_section():
    long_body = "## 长段落\n" + "这是一段很长的说明。" * 200
    out = kn.render(kn.retrieve("长段落", long_body))
    assert "已截断" in out


def test_to_inject_prompt_without_file(tmp_path):
    assert kn.to_inject_prompt(tmp_path, "登录") is None


def test_to_inject_prompt_without_query_does_not_dump_everything(tmp_path):
    """没有需求文本可参照时不猜：宁可不注入，也不把整篇塞进 prompt。"""
    pdir = tmp_path / "p"
    pdir.mkdir()
    (pdir / kn.KNOWLEDGE_FILE).write_text(MD, encoding="utf-8")
    assert kn.to_inject_prompt(pdir, "") is None


def test_to_inject_prompt_no_hit(tmp_path):
    pdir = tmp_path / "p"
    pdir.mkdir()
    (pdir / kn.KNOWLEDGE_FILE).write_text(MD, encoding="utf-8")
    assert kn.to_inject_prompt(pdir, "导出报表") is None


def test_to_inject_prompt_happy_path(tmp_path):
    pdir = tmp_path / "p"
    pdir.mkdir()
    (pdir / kn.KNOWLEDGE_FILE).write_text(MD, encoding="utf-8")
    out = kn.to_inject_prompt(pdir, "管理员登录")
    assert out and "密码长度要求" in out


# --------------------------------------------------------------------------- #
# 可解释性
# --------------------------------------------------------------------------- #
def test_explain_reports_hits_and_total(tmp_path):
    pdir = tmp_path / "p"
    pdir.mkdir()
    (pdir / kn.KNOWLEDGE_FILE).write_text(MD, encoding="utf-8")
    info = kn.explain(pdir, "管理员登录")
    assert info["has"] is True and info["total_sections"] == 3
    assert info["picked"] and info["picked"][0]["title"] == "管理员登录"
    assert info["picked"][0]["hits"], "要能看到命中了哪些词，否则无法解释为什么选中"


def test_explain_without_file(tmp_path):
    info = kn.explain(tmp_path, "登录")
    assert info["has"] is False and kn.KNOWLEDGE_FILE in info["reason"]


# --------------------------------------------------------------------------- #
# 模板
# --------------------------------------------------------------------------- #
def test_ensure_template_creates_but_never_overwrites(tmp_path):
    pdir = tmp_path / "p"
    pdir.mkdir()
    f = kn.ensure_template(pdir)
    assert f.is_file() and "项目知识" in f.read_text(encoding="utf-8")
    f.write_text("我写的宝贵内容", encoding="utf-8")
    assert kn.ensure_template(pdir) is None, "不能覆盖用户已写的内容"
    assert f.read_text(encoding="utf-8") == "我写的宝贵内容"


def test_create_project_generates_knowledge_template(tmp_path, monkeypatch, capsys):
    """新建项目就带上知识库模板 —— 没模板用户根本不知道该写什么。"""
    monkeypatch.setattr(pm, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr("builtins.input", lambda *_: "")
    # 用真实 parser 拿完整的默认参数，避免手搓 Args 少字段（cmd_create 会读很多）
    args = pm.build_parser().parse_args(["create", "--id", "demo", "--name", "演示"])
    pm.cmd_create(args)
    assert (tmp_path / "demo" / "knowledge.md").is_file()
    assert "knowledge.md" in capsys.readouterr().out
