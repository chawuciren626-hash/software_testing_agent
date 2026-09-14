"""门禁判定谓词（唯一定义处）。

这是本项目最硬的一条防假绿纪律：**全 SKIP 不判绿**。
环境不可达 / 浏览器起不来时，`failed == 0` 但 `passed == 0` —— 若只看 `failed`
就会把"什么都没跑"判成通过，CI 拿到虚假的安全信号。所以谓词必须是
"无失败 **且** 至少一条实际通过"。

`failed == 0 and passed > 0` 曾散落在 `run_regression.py` 的两处与 `run_web.py`
的一处。收敛到这里的意义：这条纪律只应有一个定义处，改一次即处处生效。
见 `docs/HARNESS_ARCHITECTURE_REVIEW.md` §4.1、§2.6。
"""
from __future__ import annotations


def all_pass(failed: int, passed: int) -> bool:
    """"无失败且至少一条实际通过"。全 SKIP 返回 `False`（防假绿）。

    ⚠ 本谓词**不含**"配置问题"维度：Web 冒烟额外要求 `not issues`，由调用方与本
    谓词组合（`all_pass(failed, passed) and not issues`），而不是在这里再分叉一份。
    """
    return failed == 0 and passed > 0
