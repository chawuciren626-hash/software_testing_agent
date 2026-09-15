# 控制台 UI/UX 设计评审报告

> 评审对象：`web_console/templates/index.html`（2396 行，8 个页面）+ `web_console/app.py`
> 评审方式：代码静态审查 + 运行时实测（`http://127.0.0.1:8765`）+ WCAG 2.2 对比度实算
> 评审日期：2026-09-15
> 标准：WCAG 2.2 AA、移动优先响应式、8px 基线栅格

---

## 0. 总体结论

**现状：这是一套完成度不错的内部工具界面**——视觉风格统一（设计 token 已建立、色板语义清晰）、卡片信息密度经过一轮优化、微交互有基本反馈（hover 抬升、Toast、进度轮询）。

**但有三个系统性短板，会随功能增长放大：**

| 级别 | 短板 | 影响 |
|---|---|---|
| **P0** | 页面切换无 URL 路由，全靠 JS 变量 `currentPage` | 刷新丢失当前页、浏览器前进/后退失效、无法分享链接——**8 个页面的入口全是"一次性"的** |
| **P0** | 零键盘可达性：无 `keydown` 监听、可点击元素多为 `div`、无 `:focus-visible` | 键盘用户完全无法操作（Tab 到不了导航项、Esc 关不掉弹窗） |
| **P1** | **零响应式断点**（全文 `@media` 数量为 0） | 侧边栏固定 250px + `body{overflow:hidden}`，<1280px 屏幕挤压、平板/小屏直接不可用 |

其余问题集中在**对比度不达标（已实测 8 处）**、**反馈态不完整**、**内联样式泛滥（87 处）** 三个方向。

---

## 1. 信息架构与导航

### 1.1 【P0】页面无路由，刷新即丢失

**现状**（第 816 行、第 2067 行）：

```js
document.querySelectorAll('.nav-item').forEach(el=>{
  el.onclick=()=>{
    document.querySelectorAll('.nav-item').forEach(e=>e.classList.remove('active'));
    el.classList.add('active');
    // ...仅切 class，不改 URL
  };
});
```

用户点「门禁」页 → F5 刷新 → 回到「项目」页。**在测试工具的真实使用场景里，这意味着"我把门禁页链接发给同事"这件事做不到。**

**建议**：用 `location.hash` 做最小改造（无需引入路由库）：

```js
// 切换页面时同步 hash
function switchPage(page){
  currentPage = page;
  document.querySelectorAll('.nav-item').forEach(e=>e.classList.toggle('active', e.dataset.page===page));
  document.querySelectorAll('.page').forEach(e=>e.classList.toggle('active', e.id==='page-'+page));
  if(location.hash.slice(1)!==page) history.pushState(null,'','#'+page);
  renderCurrentPage();
}
// 支持前进/后退 & 直接带 hash 进入
window.addEventListener('popstate', ()=>switchPage(location.hash.slice(1)||'projects'));
document.addEventListener('DOMContentLoaded', ()=>switchPage(location.hash.slice(1)||'projects'));
```

> 收益：3 处改动换来「可刷新 / 可后退 / 可分享」，是本次评审**投入产出比最高**的一项。

### 1.2 【P1】搜索框作用域不一致，用户会以为坏了

**现状**（第 2403 行）：

```js
function applySearch(){
  if(currentPage==='projects') renderProjects();
  else if(currentPage==='automation') renderAutomation();
  else if(currentPage==='skills') loadSkills();
  else if(currentPage==='knowledge'){ /* ... */ }
  // tasks / reports / gates / models 四页：无任何分支
}
```

8 个页面里只有 4 页支持搜索，但搜索框在**所有页面顶栏常驻**。切到「任务」页输入内容毫无反应——这是典型的"控件存在但失效"。

**建议**：二选一，**不要维持现状**：
- **方案 A（推荐）**：只对不支持的页面隐藏搜索框（`.topbar` 里 `#searchBox` 加 `hidden`），并给支持页加 `placeholder` 提示范围，如「搜索项目名称 / ID」。
- **方案 B**：给剩余 4 页也接上搜索（任务页按 tid/kind 过滤性价比最高）。

---

## 2. 响应式与布局

### 2.1 【P1】零断点，不是「不完美」而是「小屏不可用」

**现状**：全文 `@media` 数量为 **0**。核心布局：

```css
body{display:flex;height:100vh;overflow:hidden;}
.sidebar{width:250px;flex-shrink:0;}   /* 硬编码，永不收起 */
.content{flex:1;overflow-y:auto;padding:26px;}
.grid{grid-template-columns:repeat(auto-fill,minmax(360px,1fr));}  /* 网格本身是自适应的，这点做得对 */
```

好消息：`.grid` 用了 `auto-fill + minmax`，**卡片网格已经天然自适应**——说明作者对 CSS Grid 的理解是对的，只是没把同样的思路用在骨架上。

**建议**：加两条断点即可覆盖绝大多数场景：

```css
/* 平板 / 小笔记本：侧栏收窄，内容区减 padding */
@media (max-width:1279px){
  .sidebar{width:200px;}
  .nav-item{font-size:13px;padding:9px 10px;}
  .content{padding:20px;}
  .grid{grid-template-columns:repeat(auto-fill,minmax(300px,1fr));}
}
/* 手机 / 窄屏：侧栏抽屉化，默认隐藏 */
@media (max-width:767px){
  .sidebar{position:fixed;left:0;top:0;bottom:0;z-index:60;transform:translateX(-100%);
           transition:transform .25s ease;width:250px;}
  .sidebar.open{transform:none;}
  .topbar{padding:12px 14px;}
  .search{width:auto;flex:1;min-width:0;}
  .summary .stat{min-width:calc(50% - 6px);}   /* 统计条两列 */
  .card .meta-grid{grid-template-columns:repeat(2,minmax(0,1fr));}  /* 元信息两列 */
}
```

配合一个汉堡按钮（放在 `.topbar` 最左，仅 <768px 显示）即可。

### 2.2 【P2】`height:100vh` 在移动端浏览器会被地址栏遮挡

建议改为 `height:100dvh`（并保留 `100vh` 作为回退）：

```css
body{height:100vh; height:100dvh;}
```

---

## 3. 无障碍（WCAG 2.2 AA）

### 3.1 【P0】键盘完全不可达

**实测证据**：

| 检查项 | 结果 |
|---|---|
| `keydown` / `Escape` 监听 | **0 处** |
| `:focus-visible` 样式 | **0 处** |
| `tabindex` | **0 处** |
| `aria-*` 属性 | 仅 2 处（`role="img"` 在趋势图上） |

导航项是 `div`（第 33 行 `.nav-item`），弹窗只能点遮罩或按钮关闭——**Esc 无效**（第 2411 行只绑了 click）。

**建议**（最小改动，三步）：

```css
/* ① 全局焦点环：只在键盘导航时出现，鼠标点击不显示 */
:focus{outline:none;}
:focus-visible{outline:2px solid var(--primary);outline-offset:2px;border-radius:6px;}
.sidebar :focus-visible{outline-color:#a5b4fc;}   /* 深色底上换亮色环 */
```

```html
<!-- ② 导航项改为 button，语义 + 键盘 + 读屏一次到位 -->
<button class="nav-item active" data-page="projects" aria-current="page">
  <span class="ico">…</span><span>项目</span>
</button>
```

```js
// ③ 弹窗：Esc 关闭 + 打开时把焦点移进去
document.addEventListener('keydown', e=>{
  if(e.key==='Escape'){ document.querySelectorAll('.mask.show').forEach(m=>m.classList.remove('show')); }
});
function openModal(){
  const m=document.getElementById('modalMask');
  m.classList.add('show');
  m.querySelector('input,select,textarea,button')?.focus();
}
```

### 3.2 【P0】Toast 读屏用户完全感知不到

**现状**（第 746 行）：

```js
function showToast(msg,type=''){
  const box=document.getElementById('toast');
  box.appendChild(el);   // 只是往容器里塞节点
}
```

**建议**：加 `aria-live`，且容器**必须在页面加载时就存在**（不能动态创建，否则读屏不会监听）：

```html
<div id="toast" role="status" aria-live="polite" aria-atomic="false"></div>
```

```js
function showToast(msg,type=''){
  const box=document.getElementById('toast'); if(!box) return;
  const el=document.createElement('div');
  el.className='t-item'+(type?' '+type:'');
  el.textContent=msg;
  // 语义色之外再补一个文字前缀：颜色不是唯一信息载体
  if(type) el.dataset.type=type;
  box.appendChild(el);
  setTimeout(()=>{el.style.opacity='0';setTimeout(()=>el.remove(),320);},3600);
}
```

### 3.3 【P1】对比度实测：8 处不达标（含具体数值）

我用 WCAG 相对亮度公式逐项实算（非估算）：

| 位置 | 前景 / 背景 | 实测比值 | AA 要求 | 判定 |
|---|---|---|---|---|
| `.mi-k` 元信息标签（11px） | `#94a3b8` / `#ffffff` | **2.54:1** | 4.5:1 | ❌ |
| `.nav-group` 分组标题（10.5px） | `#5b6b85` / `#0f172a` | **3.34:1** | 4.5:1 | ❌ |
| `.badge.ok` 成功徽标 | `#059669` / `#d1fae5` | **3.32:1** | 4.5:1 | ❌ |
| `.badge.bad` 失败徽标 | `#dc2626` / `#fee2e2` | **3.95:1** | 4.5:1 | ❌ |
| `.badge.info` 信息徽标 | `#2563eb` / `#dbeafe` | **4.19:1** | 4.5:1 | ❌ |
| `.badge.skip/warn` | `#d97706` / `#fef3c7` | **2.80:1** | 4.5:1 | ❌ |
| `.btn:hover` 文字 | `#6366f1` / `#ffffff` | **4.47:1** | 4.5:1 | ⚠️ 差 0.03 |
| `.empty svg` / `.search svg` 图标 | `#94a3b8` / `#ffffff` | **2.54:1** | 3:1（图形） | ❌ |

**已达标的可保留**：`--muted #64748b`/白 = 4.76:1 ✓、`.brand-sub #7c8aa5`/深底 = 5.17:1 ✓、`.nav-item #aeb9cc`/深底 = 9.02:1 ✓。

**修复方案**（只改 token，不改任何组件代码——这正是设计 token 的价值）：

```css
:root{
  /* 文字色：整体加深一档，徽标底色不动，视觉观感几乎无变化 */
  --muted:#5c6b82;      /* 原 #64748b → 实测 5.41:1 */
  --muted-2:#64748b;    /* 原 #94a3b8 → 实测 4.76:1（11px 小字） */
  --ok:#047857;         /* 原 #059669 → 实测 4.84:1 */
  --bad:#b91c1c;        /* 原 #dc2626 → 实测 5.30:1 */
  --info:#1d4ed8;       /* 原 #2563eb → 实测 5.49:1 */
  --warn:#b45309;       /* 原 #d97706 → 实测 4.51:1 */
  --primary-600:#4338ca;/* 原 #4f46e5 → 实测 7.90:1（白底）/ 7.07:1（soft 底） */
}
.sidebar .nav-group{color:#8b9ab3;}  /* 原 #5b6b85 → 实测 6.27:1 */
```

> 备注：`--ok/--bad/--info/--warn` 同时被 `stat .v`（26px 大字）和 `badge`（11px 小字）使用。加深后大字观感会略深，但 26px 大字的对比度本来就富余，**不会出现可读性问题**。

### ✅ 已修复（2026-09-15）

上述色值已落地，另补修**报告预览区白底样式**的两处漏网（不在 `:root` token 里，是内嵌 `<style>` 硬编码）：
`.ok #059669 → #047857`（原 3.77:1）、`.muted #9ca3af → #6b7280`（原 2.52:1）。

修复后用脚本复算 **13 项全部达标、0 不达标**：

```
--muted/白 5.41 · --muted-2/白 4.76 · nav-group/深底 6.27
badge.ok 4.84 · badge.bad 5.30 · badge.info 5.49 · badge.warn 4.51
btn.hover/白 7.90 · primary-600/soft 7.07 · badge.purple 4.83
chip.get 5.17 · nav-item/深底 9.02 · brand-sub/深底 5.13
```

**保留未改**：`TREND_PAL` 图表配色（`#4f46e5` 等）—— 那是**图形元素**，适用 3:1 而非 4.5:1，
实测 6.29:1 已远超标准；且改色会改变多项目对比图的辨识度。

### 3.4 【P1】状态仅靠颜色传达

`.badge.ok/.bad/.skip` 只用颜色区分。色觉障碍用户（约 8% 男性）无法分辨。

**建议**：徽标加图形/文字冗余：

```js
const BADGE_ICON={ok:'✓',bad:'✕',skip:'⏸',warn:'!',info:'i',none:'—'};
function badge(kind,text){
  return `<span class="badge ${kind}"><span aria-hidden="true">${BADGE_ICON[kind]||''}</span>${esc(text)}</span>`;
}
```

---

## 4. 反馈与状态设计

### 4.1 【P1】缺少加载骨架屏，长任务期间是"白屏等待"

已有 `pollTask` 轮询（任务进行中），但列表/卡片初次加载是**直接空着**。

**建议**：给卡片列表加骨架屏（只动画 `opacity`，符合性能约束）：

```css
.skel{background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);
      padding:20px 22px;height:186px;position:relative;overflow:hidden;}
.skel::after{content:"";position:absolute;inset:0;
  background:linear-gradient(90deg,transparent,rgba(148,163,184,.13),transparent);
  animation:shimmer 1.3s infinite;}
@keyframes shimmer{from{transform:translateX(-100%);}to{transform:translateX(100%);}}
```

```js
async function renderProjects(){
  const box=document.getElementById('projectGrid');
  box.innerHTML = PROJECT_SKELETON.repeat(6);   // 先铺骨架
  const d = await (await fetch('/api/projects')).json();
  box.innerHTML = d.projects.length ? d.projects.map(cardHTML).join('') : EMPTY_HTML;
}
```

### 4.2 【P2】危险操作缺少「撤销」通道

`deleteProject` / `setProjectDisabled` 是破坏性操作。当前 `askDeleteProject` 有确认弹窗（这点是对的），但**删除后无法撤销**。

**建议**：Toast 里带 6 秒撤销按钮，成本低、体验提升明显：

```js
function showUndo(msg, onUndo){
  const el=document.createElement('div');
  el.className='t-item';
  el.innerHTML=`<span>${esc(msg)}</span><button class="btn sm ghost" style="margin-left:10px">撤销</button>`;
  el.querySelector('button').onclick=()=>{onUndo();el.remove();};
  document.getElementById('toast').appendChild(el);
  setTimeout(()=>el.remove(),6000);
}
```

### 4.3 【P2】空态只给了一句话

`.empty` 只有居中文字 + 图标（第 80 行）。**空态是引导用户的最好时机**——应带主行动按钮：

```html
<div class="empty">
  <svg>…</svg>
  <p>还没有项目</p>
  <p style="color:var(--muted);font-size:12.5px;margin-top:6px">创建第一个项目后，即可生成用例、跑回归与门禁</p>
  <button class="btn primary" style="margin-top:16px" onclick="openModal()">+ 新建项目</button>
</div>
```

---

## 5. 视觉一致性与 Token 纪律

### 5.1 【P1】87 处内联 `style="..."`，绕过了设计 token

实测：内联 `style=` 属性 **87 处**、`<table>` 12 处（部分用于布局而非数据）。这些值不进 token，改主题时会漏。

**不建议一次性全清**（风险高、收益递减）。建议**新增代码一律走 class**，并在以下高频处抽公共类：

```css
.mt-6{margin-top:6px;} .mt-14{margin-top:14px;} .mt-16{margin-top:16px;}
.text-muted{color:var(--muted);} .text-sm{font-size:12.5px;}
.mono{font-family:var(--mono);font-size:11.5px;}
.row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
```

### 5.2 【P2】字号刻度未成体系

当前出现的字号：10.5 / 11 / 11.5 / 12 / 12.5 / 13 / 13.5 / 15.5 / 17 / 26 px——10 档偏多，且 11.5、12.5、13.5 这种半档容易失控。

**建议收敛为 7 档（8px 基线友好）**：

| Token | 值 | 用途 |
|---|---|---|
| `--fs-xs` | 11px | 徽标、辅助说明 |
| `--fs-sm` | 12px | 元信息值、次要文本 |
| `--fs-base` | 13px | 正文、按钮 |
| `--fs-md` | 14px | 卡片标题 |
| `--fs-lg` | 16px | 页面 H1 |
| `--fs-xl` | 20px | 统计数字 |
| `--fs-2xl` | 26px | 大数字强调 |

迁移可分批做，先新增 token，再逐个替换。

### 5.3 【P2】动效缺 `prefers-reduced-motion` 兜底

已有 `fadeUp`、卡片 `translateY(-2px)`、徽标过渡——方向都对（只动 `transform`/`opacity`）。但前庭功能障碍用户需要能关掉：

```css
@media (prefers-reduced-motion: reduce){
  *,*::before,*::after{animation-duration:.01ms !important;animation-iteration-count:1 !important;
                       transition-duration:.01ms !important;}
}
```

---

## 6. 表单与输入

### 6.1 【P1】创建项目表单校验是「事后报错」

`createProject()` 直接 `fetch`，错误由后端返回后显示在 `#formErr`。

**建议**：加即时前端校验（后端校验保留——前端校验只为体验，不为安全）：

```js
function validateProjectForm(){
  const pid=document.getElementById('f_pid').value.trim();
  const base=document.getElementById('f_base').value.trim();
  const errs=[];
  if(!/^[a-zA-Z0-9][a-zA-Z0-9_-]{1,31}$/.test(pid)) errs.push('项目 ID：2-32 位，仅字母数字/下划线/连字符，且以字母数字开头');
  if(!/^https?:\/\/.+/.test(base)) errs.push('Base URL 需以 http:// 或 https:// 开头');
  return errs;
}
```

并在每个 `input` 上加 `aria-describedby` 指向错误容器，让读屏能读到。

### 6.2 【P2】长文本被截断但看不到全貌

`.card .mi-v{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}` —— URL 长时会被截成 `https://localho…`，且**只有 35 处 `title=`**。

**建议**：给所有截断元素补 `title` 属性（或 `data-tip` + tooltip），让 hover 能看到完整值：

```js
// 统一入口，避免逐处遗漏
function ellip(text){
  return `<span class="mi-v" title="${esc(text)}">${esc(text)}</span>`;
}
```

---

## 7. 数据展示

### 7.1 【P2】12 处 `<table>` 无 sticky 表头

长表格滚动后表头消失，认不出列含义。

```css
.tbl thead th{position:sticky;top:0;z-index:2;background:var(--panel);
              box-shadow:inset 0 -1px 0 var(--border);}
```

### 7.2 【P2】数字未做等宽对齐

统计数字（`.stat .v` 26px）用 `font-weight:800` 但非等宽字体，多次刷新时数字宽度跳动、视觉抖动。

```css
.stat .v{font-variant-numeric:tabular-nums;}
```

---

## 8. 整改路线图（按投入产出比排序）

| 序 | 项目 | 级别 | 改动量 | 收益 |
|---|---|---|---|---|
| 1 | **对比度 token 修正**（7 个色值） | P0 | 1 处 CSS | 一次性消除 8 处合规问题，零重构风险 |
| 2 | **Esc 关闭弹窗 + 焦点环** | P0 | ~15 行 | 键盘可用性从 0 到可用 |
| 3 | **hash 路由** | P0 | ~12 行 JS | 可刷新 / 可后退 / 可分享 |
| 4 | **导航项 `div` → `button`** | P0 | 8 处 HTML | 语义 + 键盘 + 读屏三合一 |
| 5 | **两条 `@media` 断点** | P1 | ~20 行 CSS | 平板/小屏从不可用变为可用 |
| 6 | **搜索框作用域统一**（隐藏 or 补全） | P1 | 小 | 消除"控件失效"困惑 |
| 7 | **Toast `aria-live` + 状态图标冗余** | P1 | 小 | 读屏可感知 + 色觉友好 |
| 8 | 卡片列表骨架屏 | P1 | ~20 行 | 消除白屏等待 |
| 9 | 表单即时校验 | P1 | ~15 行 | 减少一次往返 |
| 10 | 字号 token 化 / 内联样式收敛 | P2 | 中 | 长期可维护性 |
| 11 | 表格 sticky 表头 + `tabular-nums` | P2 | 4 行 | 数据可读性 |
| 12 | `prefers-reduced-motion` | P2 | 5 行 | 无障碍合规收尾 |

**落地批次**：
- **第一批（1-4）**：合规底线，纯增量、零重构。
  ✅ **已于 2026-09-15 完成** —— 浏览器实测 16 项全通过、0 JS 错误；
  回归测试 **188 passed**（console 守卫 + timing + pipeline + project_manager）。
- **第二批（5-9）**：体验提升，涉及交互改动。
  ✅ **已于 2026-09-15 完成** —— 浏览器实测 **31 + 3 = 34 项全通过、0 JS 错误**；
  回归 **188 passed**。
- 第三批（10-12）：`prefers-reduced-motion` 已顺手并入第二批（5 行、零风险）；
  余下字号 token 化 / 内联样式收敛 / 表格 sticky 表头 + `tabular-nums` 可并入日常迭代。

### 第二批落地说明（与原文案的差异）

| 项 | 文档建议 | 实际做法 | 为什么 |
|---|---|---|---|
| 7 状态冗余 | JS `badge()` helper | **纯 CSS 伪元素**（`.badge.ok::before{content:"✓"}`） | 零 JS 改动即全量生效，不存在漏改的 badge。同时**删掉了 7 处 `●` 前缀**——既然有 ✓/✕ 它就是重复装饰 |
| 8 骨架屏 | 直接铺 | **延迟 150ms 才铺** | 本地 fetch 常 <50ms，立刻铺会闪一下，"优化"反而变成新的视觉噪音。实测：慢加载(900ms) 350ms 时 6 个骨架、快加载 60ms 时 0 个 |
| 6 搜索框 | 方案 A（隐藏）/ B（补全）二选一 | **方案 A** | 8 页里只有 4 页有过滤语义，补全另外 4 页是凭空造需求 |
| 5 断点 | 1279 / 767 | 同，另加 `.side-scrim` 遮罩 + 汉堡 `aria-expanded` | 抽屉需要点击外部区域收起 |

---

## 9. 值得肯定的设计决策（不要改）

评审也要说好话，这几处是对的，重构时别误伤：

1. **`.grid` 用 `auto-fill + minmax`** —— 免媒体查询的自适应网格，思路正确，只是没推广到骨架。
2. **卡片 hover `translateY(-2px)` + shadow** —— 只动 `transform`/`opacity`，GPU 友好，幅度克制。
3. **设计 token 已建立**（`--primary`/`--ok`/`--bad`/`--radius`/`--shadow-*`）—— 这是本轮对比度能"改 7 个值解决 8 个问题"的前提。
4. **徽标独占一行 + 元信息网格化**（上一轮优化）—— 正确解决了信息密度问题。
5. **破坏性操作有二次确认**（`askDeleteProject`）—— 该有的都有。
6. **任务进度用轮询而非长连接** —— 对内部工具是合理的复杂度取舍。
