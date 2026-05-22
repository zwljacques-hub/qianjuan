# Novel Studio Demo Persistent Memory

## Product Identity (Updated 2026-05-22)

- **产品命名**: 千卷 (slug: `qianjuan`)
- **品牌定位**: AI 秘密基地 · AI 工具箱 · 千卷(M5 公测时挂载到 aisecretlair.com/toolbox/qianjuan)
- **代号历史**: 项目早期叫 "Novel Studio",2026-05-22 评审后正式命名 "千卷"
- **目标用户**: 一键写完类(Auto)+ 作者辅助类(Studio)双模,同一引擎

## Project Identity

- Local web app for a Chinese web-novel multi-agent writing workflow.
- Root path: `D:\AI\Agent Skills\novel-studio-demo`
- Primary URL: `http://127.0.0.1:5180/`

## M1 PM Deliverables (2026-05-22, 收口完成)

7 份产品文档已落盘到 `pm-deliverables/`,总计 3091 行:

- `AUDIT-001-current-state.md` (182 行) — 现状审计,11 类职责拆分 + 15 Bug 分级 + 6 P0
- `DESIGN-001-ui-sketch.md` (224 行) — UI 收敛,8 Tab→4 Tab、7 步→4 步、Auto/Studio 双模
- `PRD-001-dual-mode-workstation.md` v1.1 (576 行) — 双模工作台,10 类 intent,11 个新 API
- `PRD-002-continuity-engine.md` v1.1 (827 行) — 长篇一致性引擎,5 大模块 + 出境约束
- `PRD-003-compliance-ai-labeling.md` (533 行) — 合规与 AI 标识,12 项工程交付
- `INTEGRATION-001-ai-toolbox.md` (525 行) — AI 工具箱集成方案,矩阵入口
- `OVERVIEW-M1-review.md` (224 行) — 15 分钟评审演讲稿

## M2 W3-1 安全补丁(2026-05-22 已应用到 backend/server.py)

4 个 P0 Bug 已用 ~50 行代码堵住,等 M2 真正拆模块时搬到 app/ 对应位置:

- **BUG-002 原子写**: 新增 `_atomic_write_json(path, data)` 用 tempfile + `os.replace`;`StateStore.save` 和 `save_project_copy` 都已切换。进程崩不再留损坏 JSON
- **BUG-003 路径穿越**: `NovelStudioHandler.translate_path` 加 `posixpath.normpath` + `resolve().relative_to(ROOT)` 二次校验,拒绝 `..` 段和绝对路径
- **BUG-004 请求体上限**: `MAX_REQUEST_BODY = 1_048_576` (1MB),`do_POST` 头部检查,超出返回 413
- **BUG-005 异常脱敏**: `_new_error_id()` + `_log_error()`,客户端只见 errorId,栈帧只进服务器日志
- 验证: `python -m py_compile server.py` ✅ + `python -c "import server"` ✅
- **未修的 P0**: BUG-001 StateStore 无锁(等 Postgres 迁移时一起解决),BUG-006 LLM 失败默认通过(等 agent 拆模块时修)

## M2 应用骨架(2026-05-22 已立)

`app/` 目录树已建,15 个 `__init__.py` + `README.md`:

```
app/
├── agents/{ideation,planner,scene_writer,audit,style,truth}/
├── workflow/  domain/  handlers/  store/  llm/prompts/  fixtures/
```

`app/README.md` 内含 server.py 2222 行到目录树的逐段拆分映射表 + 7 工序顺序。后续 M2 拆模块按此映射搬代码,不用再设计目录。

## Runtime

- Backend entrypoint: `backend/server.py`
- Static frontend files: `index.html`, `app.js`, `styles.css`
- Start command: `.\start.ps1`
- `start.ps1` loads `.env.local` if present, then runs `python backend/server.py`.

## Data

- Current app state: `backend/data/project_state.json`
- Per-project bookshelf copies: `backend/data/projects/{project_id}.json`
- New projects include story length metadata: `storyLength`, `storyLengthLabel`, `targetWords`, `targetChapters`, and `chapterWords`.
- Runtime LLM config can be set through the web UI and is held only in backend process memory; API keys are not persisted to project files.
- A GPT connection test backup was created as `backend/data/project_state.before-gpt-test.20260522-032452.json`.

## Model Configuration

- Uses OpenAI-compatible `/v1/chat/completions`.
- Environment variables:
  - `NOVEL_LLM_API_KEY`
  - `NOVEL_LLM_BASE_URL`
  - `NOVEL_LLM_MODEL`
- Verified on 2026-05-22 with:
  - Base URL: `https://api.aisecretlair.cc/v1`
  - Model: `gpt-5.5`
- Do not store real API keys in project files or memory.

## Verified Behavior

- `GET /api/llm/status` reports whether the model is configured.
- `POST /api/llm/test` returned `大模型连接成功`.
- `POST /api/ideation/generate` generated a real LLM book concept with `llmGenerated=true`.
- `POST /api/ideation/generate` stores generated new-book ideas in `pendingIdeaDraft`, so exploring a new idea does not overwrite the active book's adopted `ideaDraft`/outline.
- The recommended workflow completed through Markdown export:
  - generate first chapter plan
  - lock plan
  - generate first chapter text
  - audit chapter
  - revise by audit
  - settle TruthPatch
  - export Markdown
- Scene generation recorded `lastLLMRun.used=true` and `model=gpt-5.5`.
- `POST /api/chapters/next` archives the completed current chapter, advances to the next chapter from the generated outline, builds new scene cards, and resets the workflow to planning.
- Archived chapters can be exported with `GET /api/export/markdown?chapter=N`.
- Manual scene edits are persisted with `POST /api/scenes/{scene_id}/save`. If a chapter had already been audited, saving text clears the score and asks the user to re-audit.
- Full manuscript export is available at `GET /api/export/book`.
- `GET /api/projects` lists bookshelf project summaries; `POST /api/projects/switch` switches the active project.
- The current active project is copied into `backend/data/projects/` when listing or saving projects.
- Verified on 2026-05-22 by advancing from chapter 1 to chapter 2. Current local state is chapter 2, `为了第一块火晶`, with workflow waiting at `确认计划，进入写作`.
- Verified on 2026-05-22 that manual save writes to state, full-book export contains the saved text, and test state restoration works.
- Verified on 2026-05-22 that a temporary second project can be created, appears in the bookshelf, and switching back restores the original active project. Temporary test project was removed.
- Verified on 2026-05-22 with a full restored E2E run: topic -> pending idea -> create book -> `plan -> lock -> write -> audit -> revise -> settle -> export` -> current Markdown export -> full book export -> archive chapter -> next chapter -> bookshelf listing. Temporary test project was removed and the active project restored.
- Verified on 2026-05-22 that story length selection works: manual short project creates 12 chapter planning; generated medium idea keeps 200,000 word / 80 chapter metadata, adoption preserves it, and pending ideas do not overwrite the active project's adopted outline.
- Verified on 2026-05-22 that generated book ideas provide multiple selectable titles, protagonist names, and platform synopsis options; the selected title/name/synopsis are preserved when adopting the idea into a project.
- Verified on 2026-05-22 that `/api/llm/config` returns default and per-agent model config, `POST /api/llm/config` updates runtime config without writing API keys to files, and `/api/llm/test` can test a specific Agent role.
- Verified on 2026-05-22 that the recommended chapter workflow includes `plan -> lock -> write -> audit -> revise -> style -> settle -> export`; the `style` step calls `/api/style/human-edit` and writes `humanStyleReport`.
- Verified on 2026-05-22 that original-expression editing can complete with either the audit Agent model or the local fallback, then updates the `AI 腔控制` / `原创表达` scores and records a `Human Style Editor` trace.
- Original-expression method notes are documented in `ORIGINAL_EXPRESSION_REVIEW.md`.
- Verified on 2026-05-22 after service restart: `/api/state` returns `humanStyleReport.platformGuide`, `/api/export/markdown` returns 200, and `POST /api/llm/test` for the audit Agent returns `大模型连接成功`.

## UX Decisions

- New-user path should prioritize "enter one topic -> generate book plan -> adopt -> follow recommended workflow".
- New-book creation offers story length choices: short (~50,000 words / 12 chapters), medium (~200,000 words / 80 chapters), and long (~1,000,000 words / 400 chapters). The choice affects arcs, generated chapter outlines, project summary, bookshelf, and next-chapter upper limit.
- Topic-to-book results let users choose from multiple titles, protagonist names, and synopsis styles before adopting a project.
- Character relationships now render dynamically from the current character file, with node labels and relationship cards instead of a fixed static map.
- The "模型配置" view lets users configure a default OpenAI-compatible endpoint/model and optional per-Agent model overrides.
- The workspace first screen now includes a quick-start topic input, model connection status, current project, and current recommended next step.
- Sidebar primary action is "只输入题材自动生成"; manual creation is secondary.
- Static HTML placeholders are neutral loading text to avoid flashing old demo project names before JavaScript renders live state.
- Long-running actions set button text to a busy state so users know the app is working while the model responds.
- The workflow panel has an "自动写完本章" button for new users who want the system to run the whole chapter workflow without repeated clicks.
- The completion panel now offers three clear choices: export current chapter, continue to next chapter, or start another new book.
- Done chapters in the sidebar export their archived Markdown instead of doing nothing.
- "作品总览" view shows book progress, archived chapters, and generated chapter outline.
- The "当前作品" button opens a bookshelf modal for switching between books.
- Archived chapter cards in "作品总览" can open an in-browser reader without changing the active chapter.
- Direct workflow buttons are disabled when the current step does not allow that action, keeping new users on the recommended path.
- "原创表达审校" is positioned as originality/editorial polish: reduce template language, repeated paragraph starts, mechanical explanation, and abstract emotion while preserving story facts. It is not positioned as platform-detection evasion.
- Original-expression reports include platform guidance for Fanqie, Qidian, JJWXC, and Qimao so the editor can bias toward the target platform's reading expectations.

## Open Tasks

- M1 收口已完成,所有产品文档锁定。
- **M2 W3 工程地基**(待启动): 拆 `app/store/` + Postgres 迁移、拆 `app/workflow/` + pytest、FastAPI 重写。需指派工程负责人与人力。
- **M2 W3-1 P0 补丁已应用,但 BUG-001(StateStore 无锁)和 BUG-006(LLM 失败默认通过)留到正式拆模块时一并解决**
- **法务待办**(M2 起步): 起草《用户协议》《隐私政策》《AI 辅助创作说明》v1,主体备案(深度合成 + 算法)
- **设计待办**(M2 中后期): DESIGN-001 ASCII 草图 → Figma,千卷 logo,工具箱 8 大类目 icon
- 运营待联系 3 位真实签约作者作为 M4 闭门内测人选
- Browser-level visual regression is still manual; API and static page health checks passed
- Keep README examples key-free
