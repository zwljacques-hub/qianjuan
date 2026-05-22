# Novel Studio MVP

这是一个本地可运行的“多 Agent 长篇网文写作工作台”MVP。

默认情况下系统会使用确定性的 Fake Agent Pipeline 跑完整流程；配置大模型后，题材成书和正文生成会优先调用 OpenAI 兼容接口，失败时回退到本地流程：

```text
Director
  -> Chapter Planner
  -> Scene Writer
  -> Audit Board
  -> Reviser
  -> Canon Settler
  -> TruthMerger
```

## 启动

PowerShell：

```powershell
.\start.ps1
```

或者：

```powershell
python backend/server.py
```

然后打开：

```text
http://127.0.0.1:5180/
```

## 已完成

- Web 可视化工作台
- 多作品书架与作品切换
- 新建作品向导
- 题材成书生成器
- 新书篇幅选择：短篇约 5 万字、中篇约 20 万字、长篇约 100 万字
- 书名、主角名和平台简介多候选选择
- 网页内大模型配置和按 Agent 分配模型
- 自动写完本章按钮
- 原创表达审校：减少模板句、机械解释和空泛情绪，并按平台给出表达侧重
- 热门题材识别：规则怪谈、御兽、无限流、高武、末世囤货、游戏入侵、直播文娱、种田基建等
- 章节工作台
- Chapter Plan
- Scene Cards 场景卡
- Director 面板
- TruthPatch 预览
- 三层真相文件
- 伏笔看板
- 角色关系图
- Audit Board
- Run Trace
- Markdown 导出
- 已归档章节网页阅读
- JSON 文件持久化
- Fake Agent Pipeline
- 后端 API

## API

```text
GET  /api/state
GET  /api/projects
GET  /api/llm/status
GET  /api/llm/config
POST /api/llm/test
POST /api/llm/config
POST /api/projects/create
POST /api/projects/create-from-idea
POST /api/projects/switch
POST /api/ideation/generate
POST /api/workflow/next
POST /api/workflow/run
POST /api/plan/generate
POST /api/plan/lock
POST /api/scenes/generate
POST /api/scenes/{scene_id}/save
POST /api/scenes/{scene_id}/revise
POST /api/audit/run
POST /api/revise/auto
POST /api/style/human-edit
POST /api/hooks/advance
POST /api/truth/settle
POST /api/chapters/next
POST /api/reset
GET  /api/export/markdown
GET  /api/export/book
```

## 数据

项目状态保存在：

```text
backend/data/project_state.json
```

当前打开的作品保存在 `backend/data/project_state.json`，每本作品的独立副本保存在：

```text
backend/data/projects/{project_id}.json
```

删除 `project_state.json` 或调用 `/api/reset` 可以恢复默认演示项目。

## 新用户流程

### 方式 A：直接新建

1. 打开 `http://127.0.0.1:5180/`
2. 点击左侧「手动新建」
3. 填写书名、流派、平台、篇幅/目标字数、主角名、一句话设定、第一章目标
4. 点击「创建并进入工作台」
5. 按「推荐流程」里的主按钮一路往下走：
   - 生成第一章计划
   - 确认计划，进入写作
   - 生成第一章正文
   - 审计章节
   - 按审计建议修订
   - 写入真相文件
   - 导出 Markdown
   - 继续写下一章

页面会根据当前状态自动切换主按钮，不需要新用户理解内部 Agent 和工程步骤。

### 方式 B：只输入题材

1. 点击左侧「题材成书」
2. 输入一个题材或脑洞
3. 点击「生成书名、大纲和细纲」
4. 选择候选书名、主角名和平台简介
5. 点击「采用方案创建作品」
6. 进入工作台后，继续按「推荐流程」主按钮写第一章

不想逐步确认时，可以点「自动写完本章」。系统会自动跑完计划、正文、审计、修订和真相写入，直到本章可导出。

## 原创表达审校

推荐流程会在「审计修订」之后、「写入真相文件」之前自动进入「原创表达审校」。这个步骤用于：

```text
减少高频模板词和万能转折
检查段首重复、解释过度、抽象情绪过多
保留剧情事实、角色动机、章节钩子和既有设定
输出审校报告和局部编辑后的正文
```

审校报告会根据目标平台附带侧重点：番茄偏强钩子和即时反馈，起点偏机制和长期伏笔，晋江偏人物关系和对白分寸，七猫偏类型清晰和移动端节奏。完整方法论见 `ORIGINAL_EXPRESSION_REVIEW.md`。

边界：本功能用于提升原创表达质量和人工编辑感，不用于规避平台审核或隐瞒 AI 使用。发布作品时应遵守目标平台关于 AI 辅助创作、原创性、版权和标识的规则。

## 篇幅规划

创建新书时可以选择篇幅：

```text
短篇小说：约 5 万字 / 12 章，强调单一主线和结局回收
中篇小说：约 20 万字 / 80 章，适合 2-3 个单元的完整故事
长篇连载：约 100 万字 / 400 章，适合多单元升级和长期伏笔
```

篇幅会写入项目元数据，并影响大纲分卷、章节细纲数量和下一章推进上限。

## 模型配置

「模型配置」页面可以填写 OpenAI 兼容接口：

```text
默认 API Key
默认调用地址
默认模型
每个 Agent 的独立调用地址 / 模型 / API Key
```

网页填写的 API Key 只保存在当前本地后端进程内，不写入项目文件；重启服务后需要重新填写，或者继续用 `.env.local` 管理长期配置。

### 继续旧书

1. 点击左侧「当前作品」
2. 在「我的书架」中选择一本书
3. 点击「打开继续写」
4. 系统会回到这本书当前章节和当前推荐步骤

## 连载推进

章节完成并写入真相文件后，完成面板会出现「继续写下一章」。点击后系统会：

1. 把当前章节正文归档到 `chapterArchive`
2. 按大纲进入下一章
3. 生成下一章场景卡
4. 把推荐流程重置到「生成/确认计划」

已归档章节可以在「作品总览」中直接查看正文，也可以导出：

```text
GET /api/export/markdown?chapter=1
```

全书导出：

```text
GET /api/export/book
```

## 下一步

已接入 OpenAI 兼容大模型适配器。未配置模型或模型失败时，系统会回退到本地确定性流程：

```text
Fake Agent Pipeline
  -> LLM Client
  -> Structured Output
  -> Same API / Same Frontend
```

## 大模型接入

默认不需要大模型，系统会使用 Fake Agent Pipeline。

如需启用真实模型，推荐创建 `.env.local`：

```powershell
Copy-Item .env.example .env.local
notepad .env.local
.\start.ps1
```

`.env.local` 示例：

```text
NOVEL_LLM_API_KEY=你的 API Key
NOVEL_LLM_BASE_URL=https://api.aisecretlair.cc/v1
NOVEL_LLM_MODEL=gpt-5.5
```

也可以只在当前 PowerShell 会话里设置：

```powershell
$env:NOVEL_LLM_API_KEY="你的 API Key"
$env:NOVEL_LLM_BASE_URL="https://api.aisecretlair.cc/v1"
$env:NOVEL_LLM_MODEL="gpt-5.5"
python backend/server.py
```

检查状态：

```text
GET /api/llm/status
POST /api/llm/test
```
