# 扩展③：报告与 CI 增强

把分散的测试产物聚合成报告，并把**门禁结论**通知出去（钉钉 + 163 邮件）。

## 目录
- `generate_report.py`：扫描 `report_*/test_report.md` 与 `allure-results`，生成 `test_report_index.html` 总览。
- `notify_dingtalk.py`：钉钉群机器人通知（支持加签），纯标准库。
- `notify_email.py`：163 邮箱通知（SMTP SSL），纯标准库。
- `gate_notify.py`：**门禁结果摘要与通知**（O3）。汇总三道门禁结论 → 可读摘要 → 发送；也作 CI 硬门禁（`--fail-on-gate`）。
- 流水线在**仓库根** `.github/workflows/ci.yml`（GitHub 只读这个位置）。

## 本地预览
```bash
# 接口测试产出 Allure 结果
.venv/Scripts/python -m pytest extensions/api_testing --alluredir=allure-results

# 聚合 HTML 总览
.venv/Scripts/python extensions/reporting/generate_report.py
# 打开 test_report_index.html

# 门禁摘要（不发通知）
.venv/Scripts/python extensions/reporting/gate_notify.py --dry-run
# 只看一个项目 + 门禁不达成就退出码 1（CI 用法）
.venv/Scripts/python extensions/reporting/gate_notify.py --dry-run --project mall-admin --fail-on-gate
```

## gate_notify 的判定口径

这是全项目「防假绿」口径在通知层的延续，**三态而不是两态**：

| 状态 | 含义 | 是否算门禁达成 |
|---|---|---|
| ✅ 通过 | 产物存在且 `all_pass: true` | 是 |
| ❌ 未通过 | 产物存在且 `all_pass: false` | 否 |
| ⏭ 未执行 | 声明了门禁（`regression.yaml` / `web.yaml`）但 `artifacts/` 下没有产物 | **否**（未执行 ≠ 通过） |
| — 未配置 | 项目根本没声明这道门禁 | 不参与判定（避免无谓红） |

两个容易踩的坑，都是实际写 CI 时踩出来的：

1. **必须传 `--project`。** CI 只跑 `STA_PROJECT_ID` 指定的那一个项目；
   不加过滤时，仓库里其余从未跑过的项目全是「未执行」→ 门禁**永远红**且看不出原因。
2. **必须先登记项目。** `projects/` 是运行期目录、不入库，全新 checkout 里没有项目；
   不先 `project_manager.py create` 就直接跑门禁 → 摘要输出「未发现任何已接入项目」。

## CI 配置

**Variables**（Settings → Secrets and variables → Actions → Variables）—— 决定项目级门禁跑不跑：

| Variable | 说明 |
|---|---|
| `STA_PROJECT_ID` | 参与门禁的项目 ID（留空则项目级门禁**不执行**，并打一条警告，不静默判绿） |
| `STA_GATE_BASE_URL` | 被测环境地址 |
| `STA_AUTH_TYPE` | `none` / `form` / `bearer`，默认 `none` |
| `STA_LOGIN_URL` | 登录路径，默认 `/admin/login` |

**Secrets**（Settings → Secrets）：

| Secret | 说明 |
|---|---|
| `APP_USERNAME` / `APP_PASSWORD` | 登录凭据 |
| `DINGTALK_WEBHOOK` / `DINGTALK_SECRET` | 钉钉群机器人（加签可选） |
| `MAIL_USERNAME` / `MAIL_PASSWORD` / `MAIL_TO` | 163 邮箱（授权码，非登录密码） |

未配置钉钉/邮箱时 `gate_notify` 只打印「跳过通知」，不会失败 —— 没有凭据也能验证流水线逻辑。

## 流水线结构（.github/workflows/ci.yml）

| 作业 | 是否硬门禁 | 说明 |
|---|---|---|
| `unit-tests` | **是** | 本仓库自有代码单测 + 评测基线；Allure 报告 + HTML 聚合。无 `\|\| true`，失败即红 |
| `base-tests` | 否 | fork 上游基座的遗留测试（`continue-on-error`），只为可见性，不卡交付 |
| `project-gates` | **是**（仅当已配置环境） | 核心回归 / 性能安全 / Web 冒烟；三步都 `continue-on-error` 收集全部结论后统一判定 |
| `notify` | 否 | 复用上游摘要文件发送钉钉 + 163 邮件 |

> 该 CI 设计复用了你 api_auto_demo 中已验证的「钉钉 + 163 邮件」通知经验。

## 待办
- [ ] 接入基座 Web 探索产物：`agent-explorer` 跑完的 `report_*` 自动进聚合。
- [ ] 在通知中附 Allure 报告的下载链接/摘要。
- [ ] 增加失败用例的明细摘要到通知正文（当前只到场景级 `summary`）。
