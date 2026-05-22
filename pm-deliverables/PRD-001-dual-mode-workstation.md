# PRD-001 · 双模工作台

> 文档版本: v1.1 · 2026-05-22
> 上游依赖: [DESIGN-001 UI 收敛草图](DESIGN-001-ui-sketch.md)、[AUDIT-001 现状审计](AUDIT-001-current-state.md)
> 适用阶段: M1(W1-2)产品收口 → M2(W3-5)工程实现
> 状态: **Draft,待 review**

---

## 0. 文档定位

本 PRD 定义"双模工作台"的**精确交互规约**和**数据契约**,工程团队据此可直接出接口表和前端组件清单。不包含视觉风格(留给视觉稿)、模型 prompt(留给 PRD-002)、合规标识(留给 PRD-003)。

---

## 1. 产品目标

### 1.1 用户问题陈述

| 用户类型 | 当前痛点 | 本 PRD 解决 |
|---|---|---|
| 小白 / 兴趣写作 | 看不懂"Director/Planner/Scene Writer/Audit"专业术语,8 Tab 7 步导致放弃 | **Auto 模式**:输入题材 → 输入自然语言指令 → 看正文,无术语 |
| 腰部签约作者 | 想 AI 出活,但又要逐章把控人物/伏笔/钩子,现状缺少介入手感 | **Studio 模式**:4 步流水线,每步可手动改写、回退、跳过 |
| 跨场景用户 | 想"AI 先写,我再改" 或 "我写大纲,AI 写正文",混合工作流 | **同源双模 + 控制权随时切换**,不重置数据 |

### 1.2 设计原则

1. **同一引擎,两种披风**:Auto 和 Studio 是同一套后端工作流的两种 UI 投影,数据 schema 完全一致。
2. **切换是控制权,不是项目**:切换不重置任何字段,只改"接下来谁动手"。
3. **AI 失败必须可视化**:不静默吞错。Auto 失败 → 弹切 Studio;Studio 失败 → 当前步骤红色标注 + 重试按钮。
4. **可逆优于自动**:任何 AI 决策都能被用户回退到上一个 saved 版本。

### 1.3 成功度量(M4 内测验收)

| 指标 | 目标 |
|---|---|
| 新用户 5 分钟内完成"题材输入 → 第一章正文" | ≥ 80% |
| 新用户首章完成后选择继续写第二章 | ≥ 60% |
| Studio 模式用户至少使用一次"局部重写"或"AI 腔降级" | ≥ 70% |
| Auto ↔ Studio 切换中无数据丢失投诉 | 100% |
| NPS | ≥ 30 |

---

## 2. 用户旅程

### 2.1 新用户首次进入

```
登录 / 邀请码 → 进入空书架
  ↓
点"开新书"
  ↓
[弹窗 1] 题材输入 + 篇幅选择(短/中/长)
  ↓
[弹窗 2] 偏好初始化(只弹这一次):
  "想让 AI 接管更多,还是自己掌控更多?"
  [● 让 AI 多写,我看进度]    → preferredMode = "auto"
  [○ 我自己掌控,AI 辅助]      → preferredMode = "studio"
  [○ 我先试试,再说]           → preferredMode = "auto"(默认),不写入 profile
  ↓
按 preferredMode 进入工作台
  ↓
首章生成 → 用户验收 → 进入连载循环
```

### 2.2 老用户进入(已设 preferredMode)

```
登录 → 书架 → 点书 → 直接进 preferredMode 对应工作台
```

### 2.3 章节内循环(Auto 模式)

```
显示当前章节进度
  ↓
AI 自动跑 4 步: 计划 → 正文 → 打磨 → 归档
  ↓
每步推进时 SSE 推送进度
  ↓
用户可随时:
  · 在输入框打指令(改剧情/加场景/改风格/改设定)
  · 按 ⏸ 暂停
  · 按 👋 接管 → 切到 Studio
  · 按 ↻ 重写本章 → 回到第 1 步
  · 等到第 4 步完成自动开始下一章
```

### 2.4 章节内循环(Studio 模式)

```
显示 4 步进度条 + 中央编辑流水线
  ↓
用户点"开始第 1 步: 计划"
  ↓
AI 生成计划 → 用户确认/修改 → 进入第 2 步
  ↓
每一步用户可:
  · 主动点"开始本步"触发 AI
  · 修改 AI 产出
  · 跳过本步(仅审计可跳过,计划/正文/归档不可跳)
  · 回退到任何已完成步骤
  ↓
第 4 步完成 → 弹"继续下一章 / 暂停 / 导出"3 选 1
```

---

## 3. 模式开关:精确规约

### 3.1 切换入口

- **位置**: 顶部 logo 右侧,永远可见
- **样式**: 双段 toggle `[● Auto │ ○ Studio]`,当前模式高亮
- **快捷键**: `Cmd/Ctrl + .` 切换

### 3.2 Auto → Studio 切换规则

| 当前 Auto 状态 | 切换后 Studio 表现 |
|---|---|
| AI 正在第 N 步运行中 | Studio 步骤条停在第 N 步,显示"AI 运行中(可暂停)" |
| AI 已完成第 N 步等待 | Studio 步骤条停在第 N+1 步,显示"开始第 N+1 步"主按钮 |
| 用户在输入框写了指令但 AI 未响应 | 该指令转为 Studio 计划面板顶部的"Director 备注"卡片 |
| AI 失败弹了切换提示 | Studio 步骤条对应步骤显示红色"失败,重试" |

**切换动作**: **无确认弹窗**,直接切换。理由:Auto 任何时刻都允许接管。

### 3.3 Studio → Auto 切换规则

| 当前 Studio 状态 | 切换后 Auto 表现 |
|---|---|
| 用户改了一半还没保存 | **弹确认**:"未保存修改,切到 Auto 会让 AI 接管。保存并切 / 丢弃并切 / 取消" |
| 用户已保存,处于步骤 N | Auto 显示进度条"当前第 N 步",AI 自动从第 N 步继续 |
| 用户跳过了某些步骤 | Auto 警告条:"你跳过了「打磨」,继续吗?"二选一 |

### 3.4 数据保留规则(关键)

切换模式时,以下字段**完全保留,不重置**:

```
project.* (id, title, genre, platform, storyLength, ...)
plan.* (readerPromise, mustAdvance, mustNotResolve, status)
scenes[].* (id, title, content, words, status)
characters.*
hooks.*
truthFile.*
chapterArchive.*
issues.*
scores.*
humanStyleReport.*
trace.*
```

仅以下字段**会被重置**:

| 字段 | 重置时机 | 重置为 |
|---|---|---|
| `uiMode` | 切换时 | `"auto"` 或 `"studio"` |
| `autoIntent` | 切换到 Studio 时 | 转为 `plan.directorNote` 后清空 |
| `studioUnsaved` | 切换到 Auto 时 | 弹确认后清空 |

---

## 4. Auto 模式详细规约

### 4.1 界面元素清单

| 元素 | 类型 | 必现 | 行为 |
|---|---|---|---|
| 顶部进度条 | 区块 | 是 | 显示当前章/总章、卷名、字数进度、当前步骤 |
| 章节阅读器 | 区块 | 是 | 流式渲染场景正文,带场景分隔符 |
| 自然语言输入框 | 区块 | 是 | 单行 + Enter 提交;Shift+Enter 换行;最大 500 字 |
| ⏸ 暂停 | 按钮 | AI 运行中可见 | 停止当前步,保留已生成内容 |
| 👋 接管 | 按钮 | 是 | 切到 Studio |
| ↻ 重写本章 | 按钮 | 第 2 步及之后可见 | 弹确认,回到第 1 步,清空本章 scenes |
| → 写下一章 | 按钮 | 第 4 步完成后可见 | 触发 `/api/chapters/next` |
| 📂 资料库入口 | 浮动按钮 | 是 | 点击展开右抽屉 |
| 📋 日志入口 | 浮动按钮 | 是 | 点击展开底部抽屉 |
| ⚙ 设置 | 顶部图标 | 是 | 模型/账号/导出/合规 |

### 4.2 进度条字段规约

```json
{
  "currentChapter": 12,
  "totalChapters": 400,
  "arcName": "外门大比篇",
  "wordsWritten": 32000,
  "wordsTarget": 1000000,
  "currentStep": "polish",       // plan | write | polish | archive
  "stepLabel": "打磨",
  "stepStatus": "running",       // idle | running | succeeded | failed
  "stepDetail": "AI 正在审计连续性..."
}
```

### 4.3 自然语言输入框 → 10 类 Intent

每条用户输入后端做意图分类,路由到对应 agent:

| Intent | 触发例子 | 路由 | 是否需要二次确认 |
|---|---|---|---|
| `direction_continue` | (空,Enter) | `workflow/next` | 否 |
| `direction_revise_plot` | "陈玄不要这么快服软" | `planner/adjust` | 否 |
| `direction_insert_scene` | "中间加一段苏寒回忆" | `scene_writer/insert` | 否 |
| `direction_revise_dialogue` | "把陈玄那句话改得阴狠一点"、"苏寒不要这么大声" | `scene_writer/dialogue_edit` | 否 |
| `direction_revise_style` | "更口语化一点"、"少点形容词" | `style/adjust` | 否 |
| `direction_set_pace` | "节奏放快"、"这章慢一点别赶" | `planner/pace_hint` + `scene_writer/pace_hint` | 否 |
| `direction_change_pov` | "换成陈玄视角"、"用第一人称写苏寒" | `scene_writer/pov_switch` | **是**(影响整章风格,弹"切换视角会重写本章,确认?") |
| `direction_amend_setting` | "把断剑改成断刀"、"主角改名为陈宇" | `truth/amend` | **是**(影响既有章节) |
| `direction_pause` | "停一下"、"先别写" | UI 暂停按钮 | 否 |
| `direction_meta_query` | "现在写到哪了"、"主角是谁来着" | 显示进度/资料卡片 | 否 |

**Intent 分类实现**:
- 用一个轻量 LLM 调用做分类(Haiku 4.5),system prompt 输出固定 JSON `{intent, params}`
- 兜底: 分类失败默认 `direction_continue` 且把用户输入存到 `plan.directorNote`
- **二次确认类(`change_pov` / `amend_setting`)**: 分类后先弹确认弹窗,确认后再执行

### 4.4 4 步自动推进流程

```
[第 1 步 · 计划]
  调用 planner agent
  → 生成 plan {readerPromise, mustAdvance, mustNotResolve}
  → 写入 state.plan, status=generated
  → 推进到第 2 步,无需用户确认

[第 2 步 · 正文]
  调用 scene_writer agent (流式)
  → 按 scenes[] 依次生成 content
  → SSE 推送每个场景的字增量
  → 全部 scenes.status=ready 后推进到第 3 步

[第 3 步 · 打磨]
  调用 audit agent → 拿 issues
  if issues 有 blocker/major:
    调用 reviser agent → 修正
    重新跑 audit
    if 仍有 blocker(超过 2 轮): 弹"AI 卡住"切 Studio
  调用 style agent → AI 腔降级
  → 写入 humanStyleReport
  → 推进到第 4 步

[第 4 步 · 归档]
  调用 truth_merger agent
  → 更新 truthFile, hooks, characters
  → 触发 /api/chapters/next 进入下一章
  → 重新从第 1 步开始
```

### 4.5 错误处理

| 错误类型 | 表现 | 动作 |
|---|---|---|
| LLM 超时(>120s) | 进度条 stepStatus = "failed" + 红色 | 弹"AI 卡住了,要不要切到 Studio 接管?"(二选一: 切 Studio / 再试一次) |
| LLM 返回 schema 错误 | 同上 | 同上 |
| 网络断 | 进度条 stepStatus = "failed" | 5s 后自动重试 1 次,失败再弹 |
| 用户余额不足(M4+) | stepStatus = "failed" | 弹"试笔版额度已用尽,升级套餐 / 换自带 Key" |

### 4.6 资料库右抽屉自动弹规则

满足任一条件自动弹出 3 秒:
- 当前步骤 = `archive`(章末)
- 当前章节有伏笔被回收或新增
- 当前章节有角色关系变动
- 用户在输入框打了 `direction_amend_setting` intent

弹出后停留 3 秒,无交互则自动半收起(收起到右侧 24px 宽提示条);hover 该提示条再展开。

---

## 5. Studio 模式详细规约

### 5.1 界面元素清单

| 区块 | 位置 | 内容 |
|---|---|---|
| 左侧导航 | 固定 | 4 入口(📝写作 / 📂资料 / 📋日志 / ⚙设置) + 章节列表 |
| 4 步进度条 | 中央顶部 | `● 1.计划 ─ ● 2.正文 ─ ◔ 3.打磨 ─ ○ 4.归档` |
| 章节计划面板 | 中央 | 读者承诺 / 必须推进 / 禁止解决 + Director 备注 |
| 场景卡区 | 中央 | 当前章节所有 scene 横向卡片 |
| 正文编辑器 | 中央 | 选中的场景正文,可直接 contentEditable |
| 打磨结果面板 | 中央 | 草稿分 + AI 腔分 + issues 清单 + 修订建议 |
| 资料库抽屉 | 右侧 | 真相文件 + 伏笔 + 角色三段 |
| 日志抽屉 | 底部 | 默认折叠为一行,展开看 trace 时间线 |

### 5.2 4 步进度条交互

```
● 已完成    ◔ 当前进行中    ○ 未开始    ⊘ 已跳过    ⚠ 失败可重试
```

- **可点击**: 已完成 / 当前 / 失败的步骤可点击跳转
- **不可点击**: 未开始的步骤(避免跳过审计)
- **回退到已完成步骤**: 弹确认 "回到第 N 步会保留后续修改但不再自动跑,继续吗?"
- **重试失败步骤**: 直接重新调用对应 agent

### 5.3 各步骤手动操作清单

#### 第 1 步 · 计划

| 操作 | 行为 |
|---|---|
| `开始第 1 步` | 调用 planner agent |
| `编辑 readerPromise` | inline 编辑,blur 保存 |
| `编辑 mustAdvance` | 同上 |
| `编辑 mustNotResolve` | 同上 |
| `重新生成` | 重新调用 planner,旧版本进入历史 |
| `锁定计划` | plan.status = locked,推进到第 2 步 |

#### 第 2 步 · 正文

| 操作 | 行为 |
|---|---|
| `按场景卡生成全部` | 批量调用 scene_writer |
| `单个场景生成` | 只对该场景调用,流式 |
| `添加场景卡` | 在 scenes[] 插入,需提供 title + summary,可选 type |
| `删除场景卡` | 二次确认后删除 |
| `调整场景顺序` | 拖拽 |
| `编辑场景标题/摘要/类型` | inline 编辑 |
| `编辑场景正文` | contentEditable + 自动保存(防抖 1s) |
| `局部重写` | 选中一段正文 + 输入"想改成什么" → 调用 scene_writer/inline |
| `锁定本场景` | 该场景不再被任何 agent 改动,直到解锁 |

#### 第 3 步 · 打磨

| 操作 | 行为 |
|---|---|
| `运行审计` | 调用 audit agent → 出 issues + scores |
| `按建议自动修订` | 调用 reviser agent 处理所有 blocker/major |
| `手动改` | 直接跳到对应场景编辑器 |
| `跳过本步` | issues.severity 必须全 ≤ minor 才允许跳过;blocker/major 存在时跳过按钮灰 |
| `AI 腔降级` | 调用 style agent |
| `查看本场景所有版本` | 弹历史版本对比 |

#### 第 4 步 · 归档

| 操作 | 行为 |
|---|---|
| `写入真相文件` | 调用 truth_merger,truthFile 版本号 +1 |
| `查看 TruthPatch 预览` | 弹对比 diff |
| `归档章节` | scene[]→chapterArchive,project.chapterNumber +1,重置 plan |
| `导出本章 Markdown` | 嵌入 AI 标识(见 PRD-003) |
| `继续下一章` | 自动归档 + 进入下一章计划 |

### 5.4 章节列表(左侧 sidebar)

```
章节
├─ 011 ✓ 剑鸣外门
├─ 012 ▶ 生死战书      ← 当前
├─ 013 ⚙ 三日决战      ← auto_running
├─ 014   (未开始)
└─ +新章
```

| 状态图标 | 含义 |
|---|---|
| ✓ | done(已归档) |
| ▶ | active(当前打开) |
| ⚙ | auto_running(Auto 模式正在写)— 呼吸灯 |
| 📝 | draft(开始但未完成) |
| (空) | 未开始 |

hover 显示 tooltip: 字数 / 卷名 / 最后更新时间。

---

## 6. 数据契约

### 6.1 顶层 state schema 改动

新增 / 调整字段:

```json
{
  "project": {
    // 现有字段保留
    "uiMode": "auto",                  // 新增: auto | studio
    "chapterArchive": [],
    "preferredMode": "auto"            // 用户偏好(写入 userProfile)
  },
  "workflow": {
    "currentStep": "polish",           // plan | write | polish | archive
    "stepStatus": "running",           // idle | running | succeeded | failed
    "stepDetail": "AI 正在审计...",
    "stepHistory": [
      {"step": "plan", "completedAt": "...", "by": "ai" }
    ]
  },
  "plan": {
    "readerPromise": "...",
    "mustAdvance": "...",
    "mustNotResolve": "...",
    "directorNote": "陈玄不要太快服软",  // 新增: Auto 输入框残留指令
    "status": "locked"
  },
  "scenes": [
    {
      "id": "scene_01",
      "locked": false,                 // 新增: Studio 锁场景
      "versions": []                   // 新增: 历史版本快照
    }
  ],
  "lastIntent": {                      // 新增: Auto 输入框最近一次意图
    "raw": "陈玄不要这么快服软",
    "intent": "direction_revise_plot",
    "params": {"role": "陈玄", "instruction": "不要这么快服软"},
    "timestamp": "..."
  },
  "userProfile": {
    "preferredMode": "auto",
    "modePromptShown": true            // 已弹过偏好初始化弹窗
  }
}
```

### 6.2 新增 API 端点

| Method + Path | 用途 | 参数 |
|---|---|---|
| `POST /api/intent/classify` | Auto 输入框意图分类 | `{ "raw": "string" }` → `{ "intent", "params" }` |
| `POST /api/intent/execute` | 分类后执行 | `{ "intent", "params" }` |
| `POST /api/mode/switch` | 切换 uiMode | `{ "to": "auto" \| "studio", "force": bool }` |
| `POST /api/preferences/update` | 写 userProfile.preferredMode | `{ "preferredMode": "..." }` |
| `GET /api/sse/progress` | SSE 流推进度 | 服务器主推 `{event, data}` |
| `POST /api/scenes/{id}/lock` | 锁/解锁场景 | `{ "locked": bool }` |
| `POST /api/scenes/{id}/inline-rewrite` | 局部重写 | `{ "selection": "...", "instruction": "..." }` |
| `GET /api/scenes/{id}/versions` | 查历史版本 | — |
| `POST /api/scenes/{id}/restore` | 回滚到某历史版本 | `{ "versionId": "..." }` |
| `POST /api/workflow/step/{step}/retry` | 失败重试 | — |
| `POST /api/workflow/step/{step}/jump` | 跳转步骤 | — |

### 6.3 SSE 事件类型

```
event: progress
data: {"step": "write", "scene": "scene_03", "delta": "陈玄笑了。"}

event: step_complete
data: {"step": "write", "duration_ms": 18342}

event: step_failed
data: {"step": "polish", "error": "LLM timeout", "errorId": "err_abc123"}

event: intent_classified
data: {"intent": "direction_revise_plot", "params": {...}}

event: chapter_archived
data: {"chapter": 12, "nextChapter": 13}
```

---

## 7. 偏好初始化弹窗(只弹一次)

### 7.1 触发条件

`userProfile.modePromptShown !== true && 用户创建了第一本书`

### 7.2 弹窗内容

```
┌─────────────────────────────────────────────────────┐
│  最后一步:你想怎么和 AI 一起写?                       │
├─────────────────────────────────────────────────────┤
│                                                     │
│  ┌───────────────────────────────────────────────┐  │
│  │ 🤖 让 AI 多写,我看进度就行                       │ │
│  │ 适合先看到成品,中途想改可以随时接管。           │ │
│  │ 进入 Auto 模式。                               │ │
│  └───────────────────────────────────────────────┘  │
│                                                     │
│  ┌───────────────────────────────────────────────┐  │
│  │ ✍ 我自己掌控,AI 当助手                          │ │
│  │ 适合已有写作习惯、想精修每一章。                │ │
│  │ 进入 Studio 模式。                             │ │
│  └───────────────────────────────────────────────┘  │
│                                                     │
│  [跳过,先试试看]                                    │
│                                                     │
└─────────────────────────────────────────────────────┘
```

### 7.3 行为

| 选择 | preferredMode | modePromptShown | 进入 |
|---|---|---|---|
| 让 AI 多写 | "auto" | true | Auto |
| 我自己掌控 | "studio" | true | Studio |
| 跳过 | (不设置) | true | Auto(默认) |

修改: 在 `设置 → 偏好` 里可重新选。

---

## 8. 边界 case 与约束

### 8.1 章节边界

| Case | 行为 |
|---|---|
| 用户在 Auto 模式,AI 写到最后一章(chapter === targetChapters) | 第 4 步归档后弹"全书完结!导出全书 / 加写番外 / 关闭" |
| 用户在 Studio 改了一半章节,切到其他章节 | 自动保存当前章节(防抖 1s),无确认 |
| 用户删除章节 | 二次确认。已归档章节删除会同时清空对应 chapterArchive 条目 |

### 8.2 并发与协作(M2 限制)

- 同一用户同时只能有 **1 个浏览器 tab 打开同一本书**(后端会话锁)
- 第二个 tab 打开时:弹"另一个标签页正在编辑这本书,要从这里接管吗?"二选一
- M3 才考虑多设备同步

### 8.3 LLM 调用约束

| 步骤 | 默认超时 | 重试次数 | 兜底 |
|---|---|---|---|
| planner | 60s | 1 | 显示失败,引导用户填空 |
| scene_writer | 180s/章 | 1 | 失败的场景标 needs_fix,不影响其他场景 |
| audit | 60s | 1 | 失败显示"审计未完成",允许跳过(在 Studio) |
| style | 60s | 1 | 失败保留原文,提示 |
| truth_merger | 60s | 2 | 失败阻塞归档,必须重试。长篇 100+ 章后真相文件较大,留出足够窗口 |
| intent_classify | 10s | 2 | 失败 fallback 到 `direction_continue` |

### 8.4 性能约束

- Auto 模式从用户按"开始" → 章节阅读器出第一个字: **≤ 5s**
- Studio 单步用户操作响应: **≤ 200ms**(UI 反馈,LLM 调用走 SSE)
- 章节列表显示: 即使有 400 章也 **≤ 100ms** 首屏(虚拟滚动)

---

## 9. 上线前检查清单

工程交付前必须满足:

- [ ] Auto 4 按钮(暂停 / 接管 / 重写 / 下一章)全部可用
- [ ] Studio 4 步进度条可点击跳转,跳转规则正确
- [ ] 切换模式不丢数据,测试覆盖 8 种切换场景(含未保存修改/AI运行中/失败状态)
- [ ] 偏好初始化弹窗只弹一次,设置页可重设
- [ ] 10 类 intent 分类准确率 ≥ 90%(用 80 条测试 case,每类 8 条)
- [ ] **二次确认类 intent**(`change_pov` / `amend_setting`)分类后必弹确认,确认前不执行
- [ ] SSE 断线重连可恢复进度
- [ ] 历史版本可回滚,不破坏 chapterArchive
- [ ] 资料库 4 条触发规则全部触发正确
- [ ] auto_running 章节呼吸灯样式正确,切换章节后正确解除
- [ ] 全 11 个新增 API 端点单元测试覆盖
- [ ] 全链路 e2e 测试: 创建 → Auto 跑 3 章 → 切 Studio → 改正文 → 切 Auto → 写第 4 章 → 导出全书

---

## 10. 未在本 PRD 解决的事项

| 议题 | 留待 |
|---|---|
| Intent 分类的具体 prompt 设计 | M2 工程实现时迭代 |
| 长篇 80+ 章的一致性引擎 | PRD-002 |
| 导出文件的 AI 标识与平台合规 | PRD-003 |
| 视觉风格(色彩、字号、间距) | 视觉稿(W2 后期) |
| 定价模型与套餐切换 | 上线前(用户已确认推迟) |
| 多设备同步 | M3 |
| 协作编辑(多人同书) | 上线后 |

---

## 11. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-05-22 | 初稿,基于 DESIGN-001 + 5 项已锁定决定 |
| v1.1 | 2026-05-22 | Intent 由 7 类扩到 10 类(新增 `revise_dialogue` / `set_pace` / `change_pov`);`change_pov` / `amend_setting` 列为二次确认类;`truth_merger` 超时 30s → 60s |

---

> **本 PRD 经评审通过后,M2 工程地基阶段(W3-5)可直接据此排期。**
> 评审人: 产品总监 / 工程负责人 / 视觉负责人 / 法务对接(PRD-003 关联部分)
