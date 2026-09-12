"""技能自动发现与版本目录（O1 基础件）单测。"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "extensions" / "skills"))

import registry as r  # noqa: E402

SKILLS_DIR = ROOT / "agent-skills"
CATALOG = SKILLS_DIR / "skills_catalog.json"
EXPECTED = {
    "api-test-design", "ci-gate-design", "qa-test-design",
    "requirements-to-cases", "software-testing", "web-automation",
}


def test_discovers_all_six_skills():
    skills = r.discover_skills(SKILLS_DIR)
    names = {s.name for s in skills}
    assert EXPECTED <= names
    assert len(skills) == len(EXPECTED)


def test_each_skill_has_semver():
    for s in r.discover_skills(SKILLS_DIR):
        assert s.version and s.version != r.DEFAULT_VERSION, f"{s.name} 缺 version"
        assert s.version.count(".") == 2, f"{s.name} version 不是 x.y.z：{s.version}"


def test_frontmatter_parser_is_top_level_only():
    fm = "---\nname: x\ndescription: d\nnested:\n  a: 1\n---\nbody"
    meta = r._parse_frontmatter(fm)
    assert meta["name"] == "x"
    assert meta["description"] == "d"
    assert "nested" not in meta  # 不解析嵌套块


def test_missing_frontmatter_returns_empty():
    assert r._parse_frontmatter("no frontmatter here") == {}


def test_build_catalog_matches_discovery():
    cat = r.build_catalog(SKILLS_DIR)
    assert cat["skill_count"] == len(r.discover_skills(SKILLS_DIR))
    assert all("version" in s and s["version"] != r.DEFAULT_VERSION for s in cat["skills"])
    # 目录里的 path 必须是相对仓库根（可提交、跨机可读）
    assert not cat["skills"][0]["path"].startswith(("C:", "D:", "/"))


def test_catalog_paths_are_posix_relative():
    """跨平台守护：目录的 path 必须是 POSIX 相对路径。

    Windows 下用 `str(Path(...))` 会写出反斜杠（`agent-skills\\x\\SKILL.md`），
    而这份文件要提交进仓库、给 Linux（含 CI）读取 —— 反斜杠在 POSIX 下只是普通字符，
    会造成"本机看不出、跨平台不一致"的隐形缺陷。断言在任何平台都必须成立。
    """
    for s in r.build_catalog(SKILLS_DIR)["skills"]:
        p = s["path"]
        assert "\\" not in p, f"{s['name']} 的 path 含反斜杠（应为 POSIX 相对路径）：{p}"
        assert not p.startswith(("/", "C:", "D:")), f"{s['name']} 的 path 不是相对路径：{p}"
        assert p.startswith("agent-skills/"), f"{s['name']} 的 path 应相对仓库根：{p}"


def test_committed_catalog_not_drifted():
    """提交的 skills_catalog.json 必须与当前发现结果一致，避免改了技能忘了 regenerate。"""
    if not CATALOG.is_file():
        return
    on_disk = json.loads(CATALOG.read_text(encoding="utf-8"))
    live = r.build_catalog(SKILLS_DIR)
    assert on_disk["skill_count"] == live["skill_count"]
    assert {s["name"] for s in on_disk["skills"]} == {s["name"] for s in live["skills"]}
    # path 也要比：只比名字/数量的话，分隔符写错（Windows 反斜杠）会静默漏过。
    assert {s["path"] for s in on_disk["skills"]} == {s["path"] for s in live["skills"]}
