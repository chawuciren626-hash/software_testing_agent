# 扩展③：报告与 CI 增强

把分散的测试产物聚合并接入 CI 通知（钉钉 + 163 邮件）。

## 目录
- `generate_report.py`：扫描 `report_*/test_report.md` 与 `allure-results`，生成 `test_report_index.html` 总览。
- `notify_dingtalk.py`：钉钉群机器人通知（支持加签），CI 末尾调用。
- `notify_email.py`：163 邮箱通知（SMTP SSL），CI 末尾调用。
- `.github/workflows/test.yml`：GitHub Actions 模板（pytest + Allure + 钉钉 + 163 邮件）。

## 本地预览
```bash
# 接口测试产出 Allure 结果
.venv/Scripts/python -m pytest extensions/api_testing --alluredir=allure-results

# 聚合 HTML 总览
.venv/Scripts/python extensions/reporting/generate_report.py
# 打开 test_report_index.html
```

## CI Secrets（在仓库 Settings → Secrets 配置）
| Secret | 说明 |
|---|---|
| `BASE_URL` | 被测服务地址（默认 http://localhost:8080） |
| `APP_USERNAME` / `APP_PASSWORD` | 登录凭据 |
| `DINGTALK_WEBHOOK` / `DINGTALK_SECRET` | 钉钉群机器人（加签可选） |
| `MAIL_USERNAME` / `MAIL_PASSWORD` / `MAIL_TO` | 163 邮箱（授权码，非登录密码） |

> 该 CI 设计复用了你 api_auto_demo 中已验证的「钉钉 + 163 邮件」通知经验。

## 待办
- [ ] 接入基座 Web 探索产物：`agent-explorer` 跑完的 `report_*` 自动进聚合。
- [ ] 在通知中附 Allure 报告的下载链接/摘要。
- [ ] 增加失败用例的明细摘要到通知正文。
