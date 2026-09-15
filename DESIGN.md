# DESIGN.md — 测试智能体控制台设计系统

> 适用范围：`web_console/`（Flask 控制台，8 页）
> 参考品牌：**Linear**（深色侧栏 + 高信息密度 + 克制阴影）、**Vercel**（扁平高对比、无装饰渐变）、
> **Stripe**（语义色成对定义、文字色与底色绑定）
> 本文所有数值基于 2592 行代码的脚本实测，非目测。诊断过程见 `docs/DESIGN_REVIEW.md`。
> **本文是唯一判据**：任何样式改动以本文为准；与本文冲突的既有代码视为待收敛项。

---

## 1. Visual Theme & Atmosphere

**设计哲学**：这是给测试工程师用的**工作台**，不是给访客看的营销页。
每一屏都要回答"现在能不能发版"，所以信息密度优先于留白呼吸感，状态可辨识优先于视觉愉悦。

**核心特征关键词**：`工具感` · `克制` · `高密度` · `状态优先` · `扁平`

**光影与质感**：
- 纯扁平，**禁止装饰性渐变**。唯一允许的渐变是品牌 logo（它是标识，不是背景）。
- 阴影只用于表达**层级高度**（浮起/弹窗），不用于装饰卡片。
- 不使用毛玻璃，除弹窗遮罩的 `blur(2px)`（表达"背景不可用"）。
- 深色侧栏 + 浅色内容区的**双 surface 架构**是刻意的，不要为"统一"改成全白。

---

## 2. Color Palette & Roles

### 2.1 品牌与主色

| 角色 | 变量 | 值 | 用途 |
|---|---|---|---|
| 主色 | `--primary` | `#6366f1` | 主按钮、激活态、焦点环 |
| 主色（深） | `--primary-600` | `#4338ca` | 主按钮 hover |
| 主色（浅底） | `--primary-soft` | `#eef2ff` | 激活导航底色、提示底 |

### 2.2 中性灰阶

| 角色 | 变量 | 值 | 用途 |
|---|---|---|---|
| 正文 | `--text` | `#0f172a` | 正文、标题 |
| 次要文字 | `--muted` | `#5c6b82` | 说明文字、表头 |
| 三级文字 | `--muted-2` | `#64748b` | 辅助信息（≥11px 方可使用） |

> ⚠️ `--ink` 与 `--text` 同值冗余。**新代码一律用 `--text`**，`--ink` 仅为历史别名、不得新增引用。

### 2.3 Surface 与边框

| 角色 | 变量 | 值 |
|---|---|---|
| 页面底 | `--bg` | `#f6f8fb` |
| 面板/卡片 | `--panel` | `#ffffff` |
| 主边框 | `--border` | `#e6eaf0` |
| 次边框 | `--border-2` | `#eef2f7` |

### 2.4 语义色（**成对定义，不得拆用**）

| 语义 | 文字色 | 底色 | 对比度（实测） |
|---|---|---|---|
| 成功 | `--ok` `#047857` | `--ok-soft` `#d1fae5` | 4.89:1 |
| 错误 | `--bad` `#b91c1c` | `--bad-soft` `#fee2e2` | 5.36:1 |
| 警告 | `--warn` `#b45309` | `--warn-soft` `#fef3c7` | 4.51:1 |
| 信息 | `--info` `#1d4ed8` | `--info-soft` `#dbeafe` | 5.50:1 |
| 中性 | `--muted` `#5c6b82` | `--border-2` `#eef2f7` | — |
| 品牌紫 | `--purple` `#7c3aed` | `--purple-soft` `#f3e8ff` | 4.52:1 |

> **改色纪律**：改任何一个语义色**必须重跑对比度脚本**（见 `docs/UI_UX_REVIEW.md` §3.3），
> 目测不可靠——`#6b7a91` 目测约 4.6、实算 4.36 不达标。

### 2.5 深色侧栏（第二主题，独立 token 组）

```css
--nav-bg:#0f172a;            /* 侧栏底色（纯色，禁止渐变） */
--nav-text:#cbd5e1;          /* 侧栏正文 */
--nav-text-muted:#8b9ab3;    /* 分组标题、次要 */
--nav-border:rgba(148,163,184,.14);
--nav-hover:rgba(255,255,255,.06);
--nav-active:rgba(99,102,241,.16);
--nav-focus:#a5b4fc;         /* 焦点环（深色描边在深底看不见，必须用亮色） */
```

### 2.6 阴影色

统一 `rgba(15,23,42,α)`，不随语义色变化。焦点环色：`--primary` / 深色区用 `--nav-focus`。

---

## 3. Typography Rules

**字体族**（中文优先级已正确，勿改顺序）

```css
--sans:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei","PingFang SC",Roboto,Helvetica,Arial,sans-serif;
--mono:ui-monospace,"Cascadia Code","JetBrains Mono",Consolas,"Courier New",monospace;
```

### 3.1 Type Scale（6 档，**新增字号一律从本表取**）

| Token | Size | Weight | Line-height | Letter-spacing | 用途 | 收编原值 |
|---|---|---|---|---|---|---|
| `--fs-display` | 26px | 700 | 1.2 | -0.02em | 统计大数字 | 25, 26 |
| `--fs-title` | 17px | 700 | 1.35 | -0.01em | 页面标题 H1 | 17, 19 |
| `--fs-lg` | 15px | 600 | 1.45 | 0 | 区块/卡片标题 | 14.5, 15, 15.5, 16 |
| `--fs-base` | 13px | 400 | 1.6 | 0 | 正文 | 13, 13.5, 14 |
| `--fs-sm` | 12px | 400 | 1.55 | 0 | 次要信息、表格 | 12, 12.5 |
| `--fs-xs` | 11px | 600 | 1.4 | .2px | 徽标、辅助说明 | 9, 10.5, 11, 11.5 |

合计覆盖 98 处，与实测总数一致，无遗漏。

**字重 4 档**：`400` 正文 / `500` 次级强调 / `600` 标题与控件 / `700` 页面标题与大数字。
（`800` 已废弃，仅历史残留。）

**设计哲学**：这是**数据密集型界面**，正文 13px 是刻意的——不是"字太小"，是要在一屏内放下足够多行。
补偿手段是行高给足（1.6）和层级分明，不是把字号放大。

**数字排版**：统计数字、表格数字列**必须** `font-variant-numeric:tabular-nums`，保证纵向对齐。

---

## 4. Component Stylings

### 4.1 Buttons

```css
.btn{display:inline-flex;align-items:center;gap:var(--space-inline);
     padding:var(--space-control-y) var(--space-control-x);
     border:1px solid var(--border);background:var(--panel);color:var(--text);
     border-radius:var(--r-md);font-size:var(--fs-base);font-weight:600;
     font-family:inherit;white-space:nowrap;cursor:pointer;transition:.15s;}
.btn:hover{border-color:var(--muted-2);}
.btn:active{transform:translateY(1px);}
.btn--sm{padding:6px 11px;font-size:var(--fs-sm);border-radius:var(--r-sm);}
.btn--primary{background:var(--primary);border-color:var(--primary);color:#fff;}
.btn--primary:hover{background:var(--primary-600);border-color:var(--primary-600);}
.btn--danger{background:var(--bad);border-color:var(--bad);color:#fff;}
.btn--ghost{background:transparent;border-color:transparent;color:var(--muted);}
.btn--ghost:hover{background:var(--border-2);}
.btn--block{width:100%;justify-content:center;}
```

**维度必须分开**：`--sm` 是尺寸、`--primary/--danger/--ghost` 是语义、`--block` 是形态，可叠加但不得新增组合类。
**`.btn.tab` 是历史错误**——tab 是独立组件，不寄生在 btn 上（见 §4.4）。

### 4.2 Cards

```css
.card{background:var(--panel);border:1px solid var(--border);
      border-radius:var(--r-lg);padding:var(--space-card-y) var(--space-card-x);
      box-shadow:var(--shadow-sm);display:flex;flex-direction:column;
      gap:var(--space-stack);transition:box-shadow .2s,border-color .2s;}
.card:hover{box-shadow:var(--shadow-md);border-color:var(--muted-2);}
```

> 保留 `flex-direction:column` + `gap` 的写法，**禁止改回 margin 堆叠**（gap 免疫折叠、插删元素不影响间距）。

### 4.3 Inputs & 表单控件

```css
.field{border:1px solid var(--border);border-radius:var(--r-md);background:var(--panel);
       padding:var(--space-control-y) var(--space-control-x);font-size:var(--fs-base);}
.field:focus{border-color:var(--primary);box-shadow:0 0 0 3px rgba(99,102,241,.14);outline:none;}
.field::placeholder{color:var(--muted-2);}
.field[aria-invalid="true"]{border-color:var(--bad);}
```

### 4.4 Navigation

```css
.nav-item{display:flex;align-items:center;gap:11px;padding:10px 12px;
          border-radius:var(--r-md);color:var(--nav-text);font-size:13.5px;
          width:100%;text-align:left;border:0;background:transparent;
          font-family:inherit;cursor:pointer;}
.nav-item:hover{background:var(--nav-hover);}
.nav-item.is-active{background:var(--nav-active);color:#fff;}
.nav-group{font-size:10.5px;font-weight:700;letter-spacing:1.2px;
           color:var(--nav-text-muted);text-transform:uppercase;padding:14px 12px 7px;}
```

导航项必须是 `<button type="button">` + `aria-current`，禁用 `div`。

### 4.5 Badges / Tags

```css
.badge{display:inline-flex;align-items:center;gap:5px;padding:4px 10px;
       border-radius:var(--r-pill);font-size:var(--fs-xs);font-weight:600;
       letter-spacing:.2px;white-space:nowrap;}
.badge--ok{background:var(--ok-soft);color:var(--ok);}
.badge--bad{background:var(--bad-soft);color:var(--bad);}
.badge--warn{background:var(--warn-soft);color:var(--warn);}
.badge--info{background:var(--info-soft);color:var(--info);}
.badge--neutral{background:var(--border-2);color:var(--muted);}
```

**状态不得只靠颜色**：`.badge--ok::before{content:"✓"}` / `.badge--bad::before{content:"✕"}`（CSS 伪元素，零 JS）。
**`gray` 与 `none` 语义重叠，统一为 `neutral`**。

### 4.6 Modals / Dialogs

```css
.mask{position:fixed;inset:0;background:rgba(15,23,42,.5);backdrop-filter:blur(2px);
      z-index:var(--z-modal);overflow-y:auto;padding:48px 16px;}
.modal{background:var(--panel);border-radius:var(--r-xl);width:580px;max-width:100%;
       padding:var(--space-section);box-shadow:var(--shadow-xl);}
```

打开时焦点必须移入首个可聚焦元素（延迟 ~30ms，等 display 生效）；Esc 关闭**最上层**且复用各自 close 函数。

### 4.7 Tables

```css
.tbl{width:100%;border-collapse:collapse;font-size:var(--fs-sm);}
.tbl th{position:sticky;top:0;background:var(--panel);text-align:left;
        font-weight:600;color:var(--muted);padding:var(--space-row-y) var(--space-row-x);
        border-bottom:1px solid var(--border);}
.tbl td{padding:var(--space-row-y) var(--space-row-x);border-bottom:1px solid var(--border-2);}
.tbl td.num{font-variant-numeric:tabular-nums;text-align:right;}
```

---

## 5. Layout Principles

### 5.1 间距原始层（4px 网格，7 档）

```css
--sp-1:4px; --sp-2:8px; --sp-3:12px; --sp-4:16px;
--sp-5:20px; --sp-6:24px; --sp-8:32px;
```

### 5.2 间距语义层（**组件只准用这一层**）

| Token | 值 | 用途 | 收编现值 |
|---|---|---|---|
| `--space-inline` | 4px | 图标↔文字 | 2, 3, 5, 6 |
| `--space-control-y` | 8px | 按钮/输入/徽标 纵向 | 6, 7, 9 |
| `--space-control-x` | 12px | 按钮/输入 横向 | 10, 11 |
| `--space-row-y` | 12px | 列表行/表格行 纵向 | 12, 14 |
| `--space-row-x` | 16px | 列表行 横向 | 14, 18 |
| `--space-card-y` | 16px | 卡片 纵向 | **14** |
| `--space-card-x` | 20px | 卡片 横向 | 18, 20, 22 |
| `--space-stack` | 12px | 同组元素纵向间距（gap） | 10, 12, 14 |
| `--space-section` | 24px | 区块之间 | 18, 26 |
| `--space-page` | 24px | 页面容器 padding | 26 |

### 5.3 `14px` 判定判据（关键）

`14px` 出现 9 处之所以难收敛，是因为它**同时承担了三种语义**。判定方法不是选 12 还是 16，
而是**看它所在的容器是什么**：

| 出现位置 | 容器性质 | 归属 |
|---|---|---|
| `.stat{padding:14px 18px}` | 卡片 | `--space-card-y` / `--space-card-x` → 16 / 20 |
| `.task-item`、`.kb-item` | 列表行 | `--space-row-y` / `--space-row-x` → 12 / 16 |
| `.topbar{padding:14px 26px}` | 页面头 | 纵向 `--space-row-y`(12) / 横向 `--space-page`(24) |
| `.md-code`、`.note`、`.ps-panel` | 内容面板 | `--space-card-y` / `--space-card-x` → 16 / 20 |
| 表单控件 `padding:9px 14px` | 控件 | `--space-control-y` / `--space-control-x` → 8 / 12 |

> **规则**：先判定容器类型 → 查表得 token → 不再看原数值。原数值只用于校验归类是否合理。

### 5.4 Grid 与容器

```css
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:var(--space-card-y);}
.container{max-width:1440px;margin:0 auto;padding:32px 24px 48px;}
@media(min-width:1600px){.container{max-width:1600px;}}
```

`.grid` 用 `auto-fill + minmax` 是正确思路（无媒体查询即自适应），**推广到所有多列布局，不要改断点堆叠**。

**留白哲学**：纵向留白给足（区块 24px），横向收紧（卡片内 20px）。
这是高密度工具界面的正确取舍——纵向留白帮助扫读，横向留白只是浪费屏宽。

---

## 6. Depth & Elevation

```css
--shadow-xs:0 1px 2px rgba(15,23,42,.04);
--shadow-sm:0 1px 2px rgba(15,23,42,.04), 0 1px 3px rgba(15,23,42,.05);
--shadow-md:0 1px 2px rgba(15,23,42,.04), 0 8px 24px rgba(15,23,42,.07);
--shadow-lg:0 12px 32px rgba(15,23,42,.10), 0 2px 8px rgba(15,23,42,.06);
--shadow-xl:0 24px 48px rgba(15,23,42,.16);
```

**Surface 层级**：`--bg`(页面) → `--panel`(卡片) → `--shadow-md`(hover 浮起) → `--shadow-xl`(弹窗)

```css
--z-sticky:50; --z-drawer:55; --z-modal:60; --z-toast:200;
```

> 现有 26 处 `box-shadow` 只有 2 个 token，其余散落硬编码 → 全部收敛到上表 5 档。
> 遮罩 `backdrop-filter:blur(2px)` 是唯一的模糊效果，保留。

---

## 7. Do's and Don'ts

**Do's**

1. 新样式**只引用语义层 token**（`--space-card-y`），不引用原始层（`--sp-4`），更不写数值。
2. 改任何色值后**跑对比度脚本复算**，别凭观感。
3. 组件间距用 `gap`，不用 `margin` 堆叠。
4. 状态表达**至少双通道**（颜色 + 图形/文字），不靠颜色单打独斗。
5. 数字列加 `tabular-nums`。
6. 交互元素用原生语义标签（`button`/`a`/`label`），不靠 `div + onclick` 模拟。
7. 深色区元素用 `--nav-*` 组，不复用浅色 token。
8. 弹窗打开后焦点入内、Esc 可关、复用原 close 函数。

**Don'ts**

1. **禁止装饰性渐变**（背景、卡片、按钮）。渐变只允许出现在品牌 logo。
2. 禁止在组件里写裸数值（`padding:14px`、`font-size:12.5px`）。
3. 禁止跨层引用（组件层直取 `--sp-*` 原始层，或页面层直取 HEX）。
4. 禁止新增字号档位——6 档不够用说明该复用，不是该新增。
5. 禁止用 `outline:none` 而不给替代焦点样式。
6. 禁止深色底上用深色描边做焦点环（看不见 = 没有）。
7. 禁止把 `tab` 之类的形态塞进 `.btn` 修饰符——形态是独立组件。
8. 禁止批量正则替换 `14px`（间距/圆角），必须按 §5.3 判据归类。
9. 禁止为一个用途发明新语义 token——先查 §5.2 表，没有就近归类。

---

## 8. Responsive Behavior

| 断点 | 范围 | 变化 |
|---|---|---|
| Wide | ≥1600px | 容器 1600px |
| Desktop | 1280–1599px | 容器 1440px，侧栏 250px |
| Tablet | 768–1279px | 侧栏 212px，内容 padding 20px，网格 `minmax(300px,1fr)` |
| Mobile | ≤767px | 侧栏抽屉化（transform 移出视口），汉堡按钮，统计条两列 |

**触摸目标**：≥44×44px（移动端）。导航项、按钮在 ≤767px 下 `min-height:44px`。

**折叠策略**：侧栏抽屉三个收起入口——切页、点遮罩、Esc（Esc 优先关最上层）。

**字体缩放**：字号**不随断点缩放**（13px 正文在移动端仍可读，这是工具界面）。
只调整布局密度与容器宽度。

**动效**：`@media(prefers-reduced-motion:reduce)` 下关闭 `fadeUp` 与 `transition`。

---

## 9. Agent Prompt Guide

### 9.1 Quick Reference

```
色彩：--primary #6366f1 / --text #0f172a / --muted #5c6b82 / --muted-2 #64748b
      --ok #047857 / --bad #b91c1c / --warn #b45309 / --info #1d4ed8
      深色区：--nav-bg #0f172a / --nav-text #cbd5e1 / --nav-focus #a5b4fc
字号：26 / 17 / 15 / 13 / 12 / 11  （--fs-display…--fs-xs，仅 6 档）
圆角：3 / 6 / 10 / 14 / 20 / 999px / 50%  （--r-xs…--r-full）
间距：卡片 16/20 · 列表行 12/16 · 控件 8/12 · 区块 24 · 图标文字 4
阴影：xs / sm / md / lg / xl   层级：sticky50 drawer55 modal60 toast200
铁律：只用语义 token · 无装饰渐变 · 状态双通道 · 改色必复算对比度
```

### 9.2 Component Prompts（可直接复制）

**统计卡片**
> 用 `--panel` 白底、`--r-lg` 圆角、`--shadow-sm`、`padding:var(--space-card-y) var(--space-card-x)`；
> 标签 `--fs-sm` + `--muted`，数值 `--fs-display` + 700 + `tabular-nums` + 语义色。

**状态徽标**
> `.badge` + 语义修饰类；`--fs-xs` 600、`--r-pill`；必须带 `::before` 图形前缀（✓/✕/!），
> 不得只靠颜色表达状态。

**数据表格**
> `.tbl` 契约：sticky 表头（`--panel` 底 + `--border` 下边框）、行高 `var(--space-row-y)`、
> 数字列 `tabular-nums` 右对齐、行分隔 `--border-2`。

**表单控件**
> `.field`：`--r-md`、`--sp-2/--sp-3` 内距、focus 时 `--primary` 边框 + 3px 外发光；
> 校验失败 `aria-invalid="true"` + `--bad` 边框。

**空态**
> 居中、图标 42px `--muted-2`、文案 `--fs-base` `--muted`、下方一个 `.btn--primary` 引导操作。

### 9.3 Iteration Guide

1. 先问"这是**什么容器**"，再查 §5.2 语义表；不问"原来是多少 px"。
2. 需要新色值 → 先查 §2 是否已有语义色；没有才新增，且**必须配 `-soft` 底色成对定义**。
3. 需要新字号 → **先拒绝**，从 6 档里选最接近的。真不够用说明布局方案有问题。
4. 改色/改字号后，**截图逐像素比对**验证是否无预期外变化。
5. 一次只改**一个维度**（本轮只动字号，下轮只动圆角），否则出问题无法定位。
6. 加 token 本身是零视觉变化的操作——**先加 token、验证无变化，再让组件消费**。
7. 深色区与浅色区是两套 surface，不要互相借用 token。
8. 新增交互元素优先用原生语义标签，其次补 `role`/`tabindex`/`aria-*`。
9. 任何"看起来差不多"的数值差异（0.5px 级）**一律合并**，不要保留。
10. 遇到本文未覆盖的情况——**先更新本文**，再写代码。本文是判据，不是事后记录。

---

## 附录：已完成的合规整改

`docs/UI_UX_REVIEW.md` 的 12 项路线图中，1–9 项已落地并提交（`a5a870f`）：
对比度 AA 达标、键盘可达（焦点环 + Esc）、hash 路由、导航语义化、响应式断点、
搜索框作用域、Toast `aria-live` + 状态图标、骨架屏、表单即时校验。
剩余：字号/间距/圆角 token 化、92 处内联样式收敛、表格 sticky 表头。
