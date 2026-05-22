# AUDIT-001 · Novel Studio 代码现状审计

> 审计日期: 2026-05-22 · 审计人: PM 视角(资深架构师协同)
> 代码版本: `novel-studio-demo-review-20260522.zip`
> 目标: 为 SaaS 化重写提供事实基线

---

## 1. 文件耦合度

| 文件 | 行数 | 现状 |
|---|---|---|
| `backend/server.py` | **2222** | 单文件大杂烩,11 类职责混合 |
| `backend/llm_adapter.py` | 215 | 干净,可保留 |
| `app.js` | 1355 | 单文件无模块化,但前端单页应用可接受 |
| `index.html` | 665 | 模板写死,所有面板都在初始 DOM |
| `styles.css` | 1875 | 无命名空间隔离 |

### server.py 11 类职责清单(必须拆)

| 序号 | 职责 | 代表位置 | 建议归属 |
|---|---|---|---|
| 1 | 默认 fixture 与示例项目 | [27-209](../backend/server.py:27) | `app/fixtures/` |
| 2 | 题材识别与画像 | [210-548](../backend/server.py:210) | `app/agents/ideation/` |
| 3 | 流派配置 / 篇幅配置 | [549-645](../backend/server.py:549) | `app/domain/genre.py` `app/domain/length.py` |
| 4 | 大纲与章节构建 | [614-1078](../backend/server.py:614) | `app/agents/planner/` |
| 5 | 项目创建与归档 | [1079-1314](../backend/server.py:1079) | `app/domain/project.py` |
| 6 | StateStore I/O | [1315-1372](../backend/server.py:1315) | `app/store/` |
| 7 | 工作流状态机 | [1410-1488](../backend/server.py:1410) | `app/workflow/state_machine.py` |
| 8 | 公共序列化 / 导出 | [1491-1568](../backend/server.py:1491) | `app/handlers/serializers.py` |
| 9 | Scene Writer / Style 规则 | [1569-1819](../backend/server.py:1569) | `app/agents/scene_writer/` `app/agents/style/` |
| 10 | mutate 大派发(700 行 if/elif) | [1820-2131](../backend/server.py:1820) | `app/handlers/`(按 endpoint 拆) |
| 11 | HTTP Handler | [2133-2222](../backend/server.py:2133) | 整体替换为 FastAPI |

### 建议目录树

```
app/
├── agents/
│   ├── ideation/        # 题材→书案
│   ├── planner/         # 大纲→章节计划
│   ├── scene_writer/    # 场景→正文
│   ├── audit/           # 审计
│   ├── style/           # 原创表达
│   └── truth/           # 真相归档
├── workflow/
│   ├── state_machine.py # workflow_meta 主体
│   └── transitions.py   # 状态迁移规则
├── domain/
│   ├── project.py
│   ├── chapter.py
│   ├── scene.py
│   ├── character.py
│   ├── hook.py
│   ├── truth.py
│   ├── genre.py
│   └── length.py
├── handlers/            # FastAPI routers
│   ├── projects.py
│   ├── workflow.py
│   ├── scenes.py
│   ├── audit.py
│   ├── style.py
│   ├── llm.py
│   └── export.py
├── store/
│   ├── repository.py    # Postgres ORM
│   └── snapshots.py     # 备份策略
├── llm/
│   ├── adapter.py       # 现有 llm_adapter.py
│   └── prompts/         # 抽出 system / user 模板
└── fixtures/            # 默认 demo 数据(只做 seed,不进生产逻辑)
```

---

## 2. 工作流状态机分支(workflow_meta)

[server.py:1410-1488](../backend/server.py:1410) 共 **8 个状态分支**,串行推进:

```
new_project / null
    ↓ plan
generated
    ↓ lock
locked + has_draft_scenes
    ↓ write
locked + scenes_ready + draftScore <= 0
    ↓ audit
draftScore > 0 + has_open_issues (blocker/major)
    ↓ revise
no_open_issues + !style_done + !truth_committed
    ↓ style
style_done + !truth_committed
    ↓ settle
truth_committed
    ↓ export
```

### 状态机逻辑漏洞 5 处

1. **issue 注入是硬编码,不是真审计** — [scenes/save](../backend/server.py:1884) 手动改稿后塞入一条假 issue `正文已手动修改,之前的审计结果需要重新确认`,这不是审计结果而是 UX 文案,前端无法区分"假 issue"和"真 issue",会污染指标。
2. **revise 端点改死值** — [revise](../backend/server.py:1909) 直接 `state["draftScore"] = 91`、`state["scores"][0]["score"] = 90`,没有重新跑审计。这意味着一次 revise 就过审,无法表达"修了但没修对"。
3. **`/api/scenes/{id}/revise` 写死苏寒动作链** — [1906](../backend/server.py:1906) 用主角名拼接固定文本,所有项目都得到同一段。换主角后语义可能错位。
4. **truth_committed 用字符串匹配** — `"committed" in state.get("truthAfter", "")`,任何包含该子串的字段值都会误判已归档。应改成枚举字段 `truthStatus: "pending" / "committed"`。
5. **export 后无显式终态** — workflow 走到 export 后再调用一次 `workflow/next` 仍返回 export,无法表达"本章已交付,等待下一章"。`/api/chapters/next` 推进章节才能解锁下一轮,但前端如果不点这个按钮永远卡在 export。

---

## 3. Bug 与遗漏点(按严重度排序)

### P0(SaaS 上线前必修)

| ID | 文件:行 | 描述 |
|---|---|---|
| BUG-001 | [server.py:1338](../backend/server.py:1338) `StateStore.save` | **无锁。** ThreadingHTTPServer 并发请求会丢更新。load → 长 LLM 调用 → save 之间窗口达 60-120s,另一个请求随时可以覆盖。多用户场景必崩。 |
| BUG-002 | [server.py:1338](../backend/server.py:1338) | **非原子写入。** `json.dump` 直接写目标文件,进程被 kill 留下损坏 JSON,无 `.tmp` + rename 模式。 |
| BUG-003 | [server.py:2136](../backend/server.py:2136) `translate_path` | **静态文件目录穿越。** 重写后绕过 `SimpleHTTPRequestHandler` 的安全归一化,`GET /../config.json` 可能能读到项目外文件。 |
| BUG-004 | [server.py:2193](../backend/server.py:2193) `do_POST` | **请求体无大小上限。** `Content-Length` 可设极大值,内存炸。 |
| BUG-005 | [server.py:2198](../backend/server.py:2198) | **异常字符串直接回客户端。** `str(exc)` 可能泄露文件路径、栈帧或内部状态。 |
| BUG-006 | [server.py:1854](../backend/server.py:1854) | **LLM 失败默认通过。** scene_writer 抛错后 `for scene: scene["status"] = "ready"` 仍执行,场景空内容也被标 ready,审计直接拿空文跑分。 |

### P1(M2 前必修)

| ID | 文件:行 | 描述 |
|---|---|---|
| BUG-007 | [server.py:1905](../backend/server.py:1905) | 硬编码 `state["characters"]["suHan"]` key,新项目改 key 后崩。 |
| BUG-008 | [server.py:1910](../backend/server.py:1910) | `state["scores"][0]` 无边界检查,scores 为空时 IndexError。 |
| BUG-009 | [llm_adapter.py:207](../backend/llm_adapter.py:207) | JSON 解析失败用子串提取兜底,失败后又抛,但调用方无 schema 校验,downstream 拿到错误 shape 会更晚才崩。 |
| BUG-010 | [server.py:1860](../backend/server.py:1860) | `humanStyleReport` 状态字段没有 enum,前端 `status === "done"` 判断到处散落,容易写错。 |
| BUG-011 | [server.py:1330](../backend/server.py:1330) `save_project_copy` | 每次 `save()` 都同时写 `project_state.json` 和 `projects/{id}.json`,但写顺序不在事务里,中断后两份不一致。 |
| BUG-012 | [llm_adapter.py:28](../backend/llm_adapter.py:28) | API Key 全局变量在进程内,多用户 SaaS 共用一个进程时会串号。 |

### P2(可上线后修)

- BUG-013: `add_trace` 无大小上限,跑久了 trace 数组无限增长。
- BUG-014: `find_scene` 线性扫描,可换 dict 索引。
- BUG-015: 前端 schema 与后端无校验,字段重命名后只在跑到才报错。

---

## 4. 必须立刻修的 6 条(P0 全量)

进入 M2 工程地基阶段前**这 6 个必须解决**:

1. **`StateStore` 加锁 + 原子写**(`tempfile` + `os.replace`),为后续替换成 Postgres 抢出时间窗口
2. **`translate_path` 用 `posixpath.normpath` 二次校验,拒绝 `..` 段**
3. **`do_POST` 上限 1MB,超出 413**
4. **异常响应统一 `error_id` + 后端日志,前端只看通用文案**
5. **`scene_writer` 失败时 scene 状态保持 `draft`,审计端口检查"是否所有 scene 都 ready"再放行**
6. **`/api/scenes/{id}/save` 重新触发审计而不是塞假 issue**

---

## 5. 重写优先级(给 M2 用)

工程地基阶段(W3-5)的工序:

1. (W3-1) 拆 `app/store/` + Postgres schema + 原子事务(顺手修 BUG-001/002/011)
2. (W3-2) 拆 `app/workflow/state_machine.py` + pytest 覆盖 8 个状态分支(顺手修状态机 5 个漏洞)
3. (W3-3) FastAPI router 重写 `app/handlers/`(顺手修 BUG-003/004/005)
4. (W4-1) 拆 `app/agents/` 各 agent + 抽 `app/llm/prompts/`(顺手修 BUG-006/009/010)
5. (W4-2) 邀请码登录 + 多租户 user_id 隔离(顺手修 BUG-012)
6. (W4-3) 任务队列(Arq + Redis)+ SSE 流式进度
7. (W5) 端到端跑通单章生成测试环境

---

## 附:今天就能挑出来的"快赢"

不打地基直接修,半天工:

- BUG-002 原子写: `tempfile` + `os.replace` (~20 行)
- BUG-003 路径穿越: `posixpath.normpath` 校验 (~5 行)
- BUG-004 请求体上限: `if length > 1_048_576: 413` (~3 行)
- BUG-005 异常脱敏: 统一错误响应函数 (~15 行)

加起来约 50 行代码,可作为"上线前安全补丁",和重写并行不冲突。

---

> **结论:** 工程基本盘可用,但**并发安全和兜底逻辑是定时炸弹**,SaaS 化重写之前要么修补,要么用 FastAPI 整体替换。后者更划算,趁早动。
