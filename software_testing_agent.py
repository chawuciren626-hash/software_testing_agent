"""软件测试智能体 · 轻量流水线入口（无 LLM 即可跑）。

⚠️ 本入口已并入 project_manager：逻辑统一由 ``project_manager.run_pipeline`` 实现，
本文件仅作"无 LLM 快速跑通"的薄封装，避免与 CLI 双入口漂移。

流水线（与 project_manager.py 同源）：
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
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import project_manager as pm  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="软件测试智能体流水线入口（薄封装）")
    ap.add_argument("--requirements", default="extensions/requirements_to_cases/sample_requirements.md")
    ap.add_argument("--run-api", action="store_true", help="是否运行接口自动化 pytest")
    ap.add_argument("--api-base", default=None, help="被测服务地址，默认读 BASE_URL 环境变量")
    args = ap.parse_args()

    out = pm.run_pipeline(args.requirements, run_api=args.run_api, api_base=args.api_base)
    print(f"\n✅ 流水线完成。打开 {out} 查看总览；"
          "有 LLM key 后可用 agent-explorer 补充 Web 探索产物。")


if __name__ == "__main__":
    main()
