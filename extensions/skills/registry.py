"""技能自动发现与版本目录（O1 基础件，不依赖任何市场 / 外部服务）。

解决的问题（现状见 docs/AGENT_REVIEW_AND_ROADMAP.md 的 O1）：
- **自动发现**：`agent-skills/` 当前是静态目录，没有任何代码扫描它。本模块扫描每个子目录的
  `SKILL.md` 前置元数据（YAML frontmatter），无需手工登记即可列出全部技能。
- **版本管理（本地）**：每个技能在 `SKILL.md` 里声明自己的 `version`（语义化版本，
  **独立于仓库版本 `pyproject.toml`**——技能是独立组件，各自演进）；`build_catalog()`
  汇总成 `skills_catalog.json`，供控制台 / 未来市场读取。

⚠️ 范围边界：本模块**只做本地发现 + 本地版本清单**。是否接市场（自托管 vs 公共市场）、
版本如何分发 / 升级 / 回滚，属于待决策项，见 `docs/O1_SKILL_MARKET_AND_VERSIONING.md`。
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SKILLS_DIR = ROOT / "agent-skills"
DEFAULT_VERSION = "0.0.0"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class Skill:
    name: str
    description: str
    version: str
    dirname: str
    path: str

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_frontmatter(text: str) -> dict:
    """极简 YAML frontmatter 解析：只取顶层 `key: value`，足够读取 name/description/version。

    不引 pyyaml：保持零额外依赖，且避免 Windows 下编码问题。嵌套结构本就用不到。
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        if line[0].isspace():      # 缩进的行 = 嵌套内容，跳过（只看顶层）
            continue
        if line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if not v:                  # 空值键是嵌套块的 opener（如 `nested:`），不是标量字段
            continue
        out[k] = v
    return out


def discover_skills(skills_dir: Path = SKILLS_DIR) -> list[Skill]:
    """扫描 skills_dir 下每个含 SKILL.md 的子目录，自动发现技能。"""
    if not skills_dir.is_dir():
        return []
    found: list[Skill] = []
    for d in sorted(skills_dir.iterdir()):
        if not d.is_dir():
            continue
        skill_md = d / "SKILL.md"
        if not skill_md.is_file():
            continue
        meta = _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        if not meta.get("name"):
            continue  # 缺 name 的目录不是合法技能，跳过
        found.append(Skill(
            name=meta["name"],
            description=meta.get("description", ""),
            version=meta.get("version", DEFAULT_VERSION),
            dirname=d.name,
            path=str(skill_md),
        ))
    return found


def build_catalog(skills_dir: Path = SKILLS_DIR) -> dict:
    """汇总所有技能的版本清单（不含市场元数据，那是 O1 待决策项）。

    path 存相对仓库根的路径，保证 skills_catalog.json 跨机可读、可提交。

    ⚠️ 必须用 `as_posix()` 而不是 `str(Path(...))`：后者在 Windows 下会写出反斜杠
    （`agent-skills\\api-test-design\\SKILL.md`），这份文件是要提交进仓库、给 Linux
    （含 CI）读取的 —— 反斜杠在 POSIX 下只是普通字符，会导致提交物与另一平台上的
    发现结果不一致，属于"本机看不出问题"的跨平台缺陷。
    """
    skills = discover_skills(skills_dir)
    skills_out = []
    for s in skills:
        d = s.to_dict()
        try:
            d["path"] = Path(s.path).resolve().relative_to(ROOT.resolve()).as_posix()
        except ValueError:
            pass  # 可忽略：不在仓库内的技能保留原路径（跨平台相对路径仅对仓库内技能有意义）
        skills_out.append(d)
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "skill_count": len(skills),
        "skills": skills_out,
    }


def write_catalog(out_path: Path, skills_dir: Path = SKILLS_DIR) -> dict:
    cat = build_catalog(skills_dir)
    out_path.write_text(
        json.dumps(cat, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return cat


def main(argv=None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="技能目录（自动发现 + 版本清单）")
    p.add_argument("--skills-dir", default=str(SKILLS_DIR), help="技能根目录")
    p.add_argument("--catalog", metavar="OUT", help="生成 skills_catalog.json 到指定路径")
    p.add_argument("--list", action="store_true", help="打印技能列表（默认行为）")
    args = p.parse_args(argv)
    sd = Path(args.skills_dir)

    if args.catalog:
        cat = write_catalog(Path(args.catalog), sd)
        print(f"已写目录：{args.catalog}（{cat['skill_count']} 个技能）")
        return 0

    skills = discover_skills(sd)
    for s in skills:
        print(f"{s.name:24} v{s.version:8} {s.dirname}")
    print(f"共 {len(skills)} 个技能")
    return 0


if __name__ == "__main__":
    sys.exit(main())
