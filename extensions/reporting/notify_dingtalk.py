"""钉钉群机器人通知（CI 末尾调用）。

通过 GitHub Secrets 注入 DINGTALK_WEBHOOK / DINGTALK_SECRET。
支持「加签」安全设置（HmacSHA256 + Base64），兼容关键字/签名两种模式。
纯标准库实现（urllib + hashlib + hmac + base64 + time + json）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
import urllib.error


def _sign(secret: str) -> tuple[str, str]:
    """钉钉加签：返回 (timestamp, sign)。"""
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    sign = base64.b64encode(hmac_code).decode("utf-8")
    return timestamp, sign


def send(text: str, webhook: str, secret: str = "") -> None:
    if not webhook:
        print("未配置 DINGTALK_WEBHOOK，跳过钉钉通知。")
        return
    url = webhook
    if secret:
        ts, sign = _sign(secret)
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}timestamp={ts}&sign={sign}"
    payload = {
        "msgtype": "text",
        "text": {"content": f"[软件测试智能体] {text}"},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print("钉钉通知结果：", resp.read().decode("utf-8", "replace"))
    except urllib.error.URLError as e:
        print("钉钉通知失败：", e)


def main() -> None:
    # CI 中可传构建结论，这里简单取环境变量或默认文案
    conclusion = os.getenv("WORKFLOW_CONCLUSION", "测试流水线执行完成")
    send(conclusion, os.getenv("DINGTALK_WEBHOOK", ""), os.getenv("DINGTALK_SECRET", ""))


if __name__ == "__main__":
    main()
