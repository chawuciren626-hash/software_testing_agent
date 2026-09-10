"""软件测试智能体 · 可运行流水线入口（无需 LLM 即可跑）。

把本项目的扩展能力串成一条可执行流水线，体现"测试智能体"的工程闭环：
  1) 需求 → 用例        （extensions/requirements_to_cases）
  2) 接口自动化 pytest  （extensions/api_testing，可选 --run-api）
  3) 报告聚合 HTML      （extensions/reporting）

说明：
- 基座上层 Web 探索（agent-explorer）需要 Claude/Gemini key；
  本入口不依赖 LLM，用于在没有 key / 后端未起时也能跑通流程、产出报告。
- 有 key 后，可再用 agent-explorer 跑 Web 探索，产物（report_*）也会被第 3 步聚合。

用法：
    python software_testing_agent.py                                    # 仅需求→用例 + 报告
    python software_testing_agent.py --run-api                          # 加跑接口自动化
    python software_testing_agent.py --requirements <file.md> --run-api --api-base http://localhost:8080
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def step_requirements(req_file: str) -> Path:
    sys.path.insert(0, str(ROOT / "extensions" / "requirements_to_cases"))
    import generate_cases as gc  # noqa: E402

    text = Path(req_file).read_text(encoding="utf-8")
    items = gc.parse_requirements(text)
    cases = gc.gen_cases(items)
    out_md = ROOT / "extensions" / "requirements_to_cases" / "cases.md"
    out_md.write_text(gc.to_markdown(cases, req_file), encoding="utf-8")
    print(f"[1/3] 需求→用例：解析 {len(items)} 条需求，生成 {len(cases)} 条用例 → {out_md}")
    return out_md


def step_api(run_api: bool, api_base: str | None) -> None:
    if not run_api:
        print("[2/3] 接口自动化：跳过（未加 --run-api）")
        return
    env = os.environ.copy()
    if api_base:
        env["BASE_URL"] = api_base
    print("[2/3] 接口自动化：运行 pytest（mall-admin 不可达会自动 skip）...")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "extensions/api_testing", "-v",
         "--alluredir=allure-results"],
        cwd=ROOT, env=env,
    )
    print(f"[2/3] 接口自动化：pytest 退出码 {result.returncode}")


def step_report() -> Path:
    sys.path.insert(0, str(ROOT / "extensions" / "reporting"))
    import generate_report as gr  # noqa: E402

    html = gr.render()
    out = ROOT / "test_report_index.html"
    out.write_text(html, encoding="utf-8")
    print(f"[3/3] 报告聚合：已生成 {out}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="软件测试智能体流水线入口")
    ap.add_argument("--requirements", default="extensions/requirements_to_cases/sample_requirements.md")
    ap.add_argument("--run-api", action="store_true", help="是否运行接口自动化 pytest")
    ap.add_argument("--api-base", default=None, help="被测服务地址，默认读 BASE_URL 环境变量")
    args = ap.parse_args()

    step_requirements(args.requirements)
    step_api(args.run_api, args.api_base)
    step_report()
    print("\n✅ 流水线完成。打开 test_report_index.html 查看总览；"
          "有 LLM key 后可用 agent-explorer 补充 Web 探索产物。")


if __name__ == "__main__":
    main()
