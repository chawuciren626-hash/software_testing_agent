"""163 邮箱通知（CI 末尾调用）。

通过 GitHub Secrets 注入 MAIL_USERNAME / MAIL_PASSWORD / MAIL_TO。
使用 163 SMTP（smtp.163.com:465 SSL）。纯标准库实现（smtplib + email）。
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email.mime.text import MIMEText
from email.header import Header


def send(subject: str, body: str, username: str, password: str, to_addr: str) -> None:
    if not (username and password and to_addr):
        print("未配置 163 邮箱 Secrets，跳过邮件通知。")
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = Header(username, "utf-8")
    msg["To"] = Header(to_addr, "utf-8")
    msg["Subject"] = Header(subject, "utf-8")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.163.com", 465, context=context) as server:
        server.login(username, password)
        server.sendmail(username, [to_addr], msg.as_string())
    print(f"163 邮件已发送至 {to_addr}")


def main() -> None:
    conclusion = os.getenv("WORKFLOW_CONCLUSION", "测试流水线执行完成")
    send(
        "软件测试智能体 · 测试流水线通知",
        f"流水线结论：{conclusion}\n详见 Allure 报告与 test_report_index.html。",
        os.getenv("MAIL_USERNAME", ""),
        os.getenv("MAIL_PASSWORD", ""),
        os.getenv("MAIL_TO", ""),
    )


if __name__ == "__main__":
    main()
