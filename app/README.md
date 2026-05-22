# 千卷 M2 工程地基 · 应用骨架

> 这是 [AUDIT-001](../pm-deliverables/AUDIT-001-current-state.md) §1 提议的拆分目标结构。
> 当前是空骨架,实际代码仍在 `backend/server.py`(2222 行单文件)。
> M2 W3-5 阶段按下表把单文件拆到这里。

---

## 拆分映射(从 server.py 到这里)

| server.py 行号 | 职责 | 目标位置 |
|---|---|---|
| [27-209](../backend/server.py) | 默认 fixture 与示例项目 | `app/fixtures/default_state.py` |
| [210-548](../backend/server.py) | 题材识别与画像 | `app/agents/ideation/{intent.py, profile.py}` |
| [549-645](../backend/server.py) | 流派配置 / 篇幅配置 | `app/domain/{genre.py, length.py}` |
| [614-1078](../backend/server.py) | 大纲与章节构建 | `app/agents/planner/{outline.py, arcs.py}` |
| [1079-1314](../backend/server.py) | 项目创建与归档 | `app/domain/project.py` |
| [1315-1372](../backend/server.py) | StateStore I/O | `app/store/repository.py`(M2 改 Postgres) |
| [1410-1488](../backend/server.py) | 工作流状态机 | `app/workflow/state_machine.py` |
| [1491-1568](../backend/server.py) | 公共序列化 / 导出 | `app/handlers/serializers.py` |
| [1569-1683](../backend/server.py) | Scene Writer 调 LLM | `app/agents/scene_writer/{builder.py, prompts.py}` |
| [1685-1819](../backend/server.py) | Style 规则与原创表达 | `app/agents/style/{rules.py, prompts.py}` |
| [1820-2131](../backend/server.py) | mutate() 700 行 if/elif | `app/handlers/*.py`(按 endpoint 拆) |
| [2133-2222](../backend/server.py) | HTTP Handler | 整体替换为 FastAPI `app/main.py` |

---

## 子目录责任边界

```
app/
├── agents/         # 每个 agent 一个子包,内部有 prompts/ + builder.py + tools.py
│   ├── ideation/   # 题材 → 书案
│   ├── planner/    # 大纲 → 章节计划
│   ├── scene_writer/ # 场景 → 正文
│   ├── audit/      # 审计 + RAG(PRD-002 §4.5)
│   ├── style/      # 原创表达审校
│   └── truth/      # 真相归档
│
├── workflow/       # 工作流状态机 + 转移规则
│
├── domain/         # 业务实体定义(无 I/O,无 LLM)
│
├── handlers/       # FastAPI router,按 endpoint 拆
│
├── store/          # Postgres ORM + 备份策略(M2 W3 重点)
│
├── llm/            # LLM 适配器 + 共享 prompt 工具
│   └── prompts/    # 跨 agent 共享的 prompt 片段
│
└── fixtures/       # demo seed data,只在测试环境用
```

---

## M2 拆分顺序(7 工序)

按 [AUDIT-001 §5](../pm-deliverables/AUDIT-001-current-state.md):

1. **W3-1** 拆 `app/store/` + Postgres 迁移(顺手修 BUG-001 并发锁、BUG-002 原子写、BUG-011 双写不一致)
   - BUG-002 已在 [server.py:_atomic_write_json](../backend/server.py) 临时打补丁,等真正拆 store/ 时合并到 Postgres 事务
2. **W3-2** 拆 `app/workflow/state_machine.py` + pytest(顺手修状态机 5 个漏洞)
3. **W3-3** FastAPI 整体重写 `app/handlers/`(顺手修 BUG-003 路径穿越、BUG-004 请求体上限、BUG-005 异常脱敏)
   - BUG-003/004/005 已在 [server.py:NovelStudioHandler](../backend/server.py) 临时打补丁,FastAPI 迁移时迁过去
4. **W4-1** 拆 `app/agents/` + 抽 `app/llm/prompts/`(顺手修 BUG-006 LLM 失败默认通过、BUG-009 schema、BUG-010 enum)
5. **W4-2** 邀请码登录 + 多租户 user_id 隔离(顺手修 BUG-012 API Key 串号)
6. **W4-3** 任务队列(Arq + Redis)+ SSE 流式进度
7. **W5** 端到端打通,测试环境跑通单章生成

---

## 已应用的 P0 临时补丁(在 server.py 中)

这些补丁在 M2 真正拆模块前先把安全风险堵住,**不需要在 app/ 下重复实现**。
M2 拆模块时把这些逻辑搬到对应位置:

| Bug | server.py 补丁位置 | 搬到 app/ 的目标 |
|---|---|---|
| BUG-002 原子写 | `_atomic_write_json()` | `app/store/atomic.py` |
| BUG-003 路径穿越 | `NovelStudioHandler.translate_path()` | FastAPI 自带,删 |
| BUG-004 请求体上限 | `do_POST` 头部检查 | FastAPI 中间件 `app/handlers/middleware.py` |
| BUG-005 异常脱敏 | `_new_error_id()` + `_log_error()` | `app/handlers/exception_handler.py` |
