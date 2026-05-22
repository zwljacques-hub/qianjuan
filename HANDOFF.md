# 千卷 · 服务器上线交接 (HANDOFF)

> **致接手 AI**: 你将接手这个项目把它部署到服务器。下面是你需要知道的一切。
> 打包日期: 2026-05-22 · 交接来源: 千卷 PM(AI 秘密基地 母品牌下的产品总监)

---

## ⚡ 30 秒速览

| 维度 | 值 |
|---|---|
| 产品名 | **千卷**(slug `qianjuan`) |
| 类别 | AI 网文写作工作台(双模:Auto 一键 + Studio 作者辅助) |
| 母品牌 | **AI 秘密基地**(aisecretlair.com),挂在 **AI 工具箱** 板块下 |
| 最终 URL | `aisecretlair.com/toolbox/qianjuan`(M5 公测时上线) |
| 当前阶段 | M1 产品收口已完成,M2 工程地基刚起步(P0 安全补丁已打) |
| 12 周路线 | M1 ✅ → M2 工程 → M3 一致性引擎 → M4 内测 → M5 公测上线 |
| 你要做什么 | **见 §3「你的任务」**(不是直接重写,先读 PM 文档) |

---

## 1. 项目当前状态

### 1.1 这是什么

一个**本地可运行**的网文 AI 写作 demo,带:
- Python 后端(`backend/server.py`,2222 行单文件,基于 `http.server`)
- 静态前端(`index.html` + `app.js` + `styles.css`)
- OpenAI 兼容的 LLM 适配器(`backend/llm_adapter.py`)
- JSON 文件持久化(`backend/data/`)
- 完整的多 Agent 工作流: Director → Planner → Scene Writer → Audit → Reviser → Style → Truth Merger

### 1.2 本地启动方式

Windows PowerShell:
```powershell
.\start.ps1
```

或者:
```bash
cd backend
python server.py
```

默认监听 `http://127.0.0.1:5180/`。无需任何依赖安装(纯 Python 标准库)。

### 1.3 用真实 LLM(可选)

复制 `.env.example` 为 `.env.local` 并填:
```
NOVEL_LLM_API_KEY=你的 key
NOVEL_LLM_BASE_URL=https://api.aisecretlair.cc/v1
NOVEL_LLM_MODEL=gpt-5.5
```

不填则用本地确定性 Fake Pipeline 跑通流程。

---

## 2. 项目已做的事(必读)

### 2.1 M1 PM 交付物 ✅(已完成)

7 份产品文档,共 3091 行,在 `pm-deliverables/` 下,**部署前先读这 3 份**:

| 必读优先级 | 文档 | 看什么 |
|---|---|---|
| 🔴 P0 | `OVERVIEW-M1-review.md` | 15 分钟快速理解整个项目状态 |
| 🔴 P0 | `AUDIT-001-current-state.md` | 已知的所有 Bug 和拆分计划 |
| 🟡 P1 | `INTEGRATION-001-ai-toolbox.md` | SSO + URL + 入驻流程,部署强相关 |
| 🟢 P2 | `PRD-001-dual-mode-workstation.md` | Auto/Studio 产品规约 |
| 🟢 P2 | `PRD-002-continuity-engine.md` | 长篇一致性引擎(M3 才做,但你需要知道) |
| 🟢 P2 | `PRD-003-compliance-ai-labeling.md` | 合规清单 |
| 🟢 P2 | `DESIGN-001-ui-sketch.md` | UI 草图 |

### 2.2 M2 W3-1 安全补丁 ✅(已应用到 server.py)

为了不让部署在公网上立刻被打,4 个 P0 Bug 已用 ~50 行临时补丁堵住:

| Bug | server.py 位置 | 改动 |
|---|---|---|
| BUG-002 原子写 | `_atomic_write_json()` + `StateStore.save/save_project_copy` | tempfile + `os.replace` |
| BUG-003 路径穿越 | `NovelStudioHandler.translate_path()` | `posixpath.normpath` + `relative_to(ROOT)` 二次校验 |
| BUG-004 请求体上限 | `do_POST` 头部 | 1MB 上限,超出 413 |
| BUG-005 异常脱敏 | `_new_error_id()` + `_log_error()` | 客户端只见 errorId,栈帧只进日志 |

**未修但已知的 P0**:
- **BUG-001 StateStore 无锁** — 并发请求会丢更新。等 Postgres 迁移时一起解决。**短期对策: 反向代理层限流到单进程,或者只允许单用户。**
- **BUG-006 LLM 失败默认通过** — 场景空内容会被标 ready。等 agent 拆模块时修。

完整 Bug 清单见 `pm-deliverables/AUDIT-001-current-state.md`。

### 2.3 M2 app/ 应用骨架 ✅(空目录,带映射表)

`app/` 已建,带 `README.md` 内含 server.py 2222 行 → 目录树的逐段拆分映射。**M2 真正拆代码时按这个映射搬,不要重新设计目录。**

---

## 3. 你的任务

### 3.1 优先级与选择

接手 AI,你需要根据用户的需求选择以下一种部署模式。**先问用户**:

#### 模式 A · 立即上 demo(最快,1-2 天)

把当前 demo 加上 P0 补丁直接部署。适合:
- 想让 PM/作者快速试用
- 单用户场景(并发风险可控)
- M5 公测前的内部 demo

**部署清单**:
- [ ] 选服务器(Linux 优先)
- [ ] 安装 Python 3.11+
- [ ] 把 `backend/` 拷上去,跑 `python server.py`
- [ ] **反向代理**(Nginx)前置,HTTPS,限流(防止 BUG-001 被并发触发)
- [ ] 准备 `/api/state` 健康检查
- [ ] 部署在 **子路径** `/toolbox/qianjuan/` 而不是根路径(配合 INTEGRATION-001)
- [ ] **限制为单进程**(`ThreadingHTTPServer` 多线程已知有 StateStore 并发问题)
- [ ] 加 systemd unit 做开机自启
- [ ] 加日志轮转

#### 模式 B · M2 工程地基重写后再部署(2-4 周)

按 `pm-deliverables/AUDIT-001-current-state.md` §5 跑 7 工序:
1. 拆 `app/store/` + Postgres 迁移
2. 拆 `app/workflow/state_machine.py` + pytest
3. FastAPI 整体重写
4. 拆 `app/agents/` + 抽 prompts
5. 邀请码登录 + 多租户
6. 任务队列(Arq + Redis)+ SSE 流式
7. 端到端测试

这是 PM 推荐路径,但工程量大,**只有用户要求"做扎实再上"才选 B**。

#### 模式 C · 混合(推荐)

短期 A,长期向 B 迁移:
- **第 1-2 天**: A 模式上线,作内部预览
- **第 3-15 天**: 按 M2 路线图重写,新代码在 `app/` 下
- **第 15 天后**: 切流到 `app/` 新版,旧 `backend/server.py` 下线

### 3.2 必须遵守的红线

1. **产品名 = 千卷**,不要改名,不要叫"Novel Studio"或"笔录"
2. **品牌路径 = AI 秘密基地 · AI 工具箱 · 千卷**
3. **导出文件必须嵌入 AI 标识**(见 `pm-deliverables/PRD-003-compliance-ai-labeling.md` §2.2)
4. **用户作品文本不得发往境外 LLM**(默认境内合规模型,境外 opt-in 且只发脱敏 context;见 PRD-002 §7.4)
5. **API Key 不进项目文件**(运行时配置 + 加密存储,见 PRD-003 §6)
6. **未经 PM 同意不要修改 PRD**(用户协议条款是法务硬约束)

### 3.3 你不需要做的事

- 不要做付费/订阅系统(用户已明确推迟到上线后)
- 不要做多设备同步(M3 才考虑)
- 不要做移动端工作台 UI(M5 不强制,作者用 PC)
- 不要改变品牌挂载关系(笔录已 deprecated,改成千卷)

---

## 4. 项目文件结构

```
qianjuan-handoff-20260522/
├── HANDOFF.md                        ← 你正在读
├── PERMANENT_MEMORY.md               ← 项目记忆,看完 HANDOFF 接着看这个
├── ORIGINAL_EXPRESSION_REVIEW.md     ← 原创表达审校方法论
├── README.md                         ← 原 demo README(用户向)
├── .env.example                      ← LLM 配置模板
├── start.bat, start.ps1              ← Windows 启动脚本
│
├── backend/                          ← Python 后端(已打 P0 补丁)
│   ├── server.py                     ← 2222 行单文件,即将拆到 app/
│   ├── llm_adapter.py                ← OpenAI 兼容适配器
│   ├── README.md
│   └── data/
│       ├── project_state.json        ← 当前 demo 项目状态
│       └── projects/                 ← 多书架副本
│
├── index.html, app.js, styles.css    ← 前端(单页应用)
│
├── app/                              ← M2 拆模块目标骨架(空目录)
│   ├── README.md                     ← server.py → app/ 拆分映射表
│   └── {agents,workflow,domain,handlers,store,llm,fixtures}/
│
└── pm-deliverables/                  ← 7 份 PM 文档
    ├── AUDIT-001-current-state.md
    ├── DESIGN-001-ui-sketch.md
    ├── PRD-001-dual-mode-workstation.md
    ├── PRD-002-continuity-engine.md
    ├── PRD-003-compliance-ai-labeling.md
    ├── INTEGRATION-001-ai-toolbox.md
    └── OVERVIEW-M1-review.md
```

---

## 5. 上线前安全/合规 checklist

无论模式 A B C,**上公网前**这些必须满足:

- [ ] HTTPS(Let's Encrypt 或商业证书)
- [ ] 反向代理(Nginx / Caddy)前置,直接暴露 5180 端口禁止
- [ ] 限流(单 IP 100 req/min 起步)
- [ ] 防 CSRF(目前 API 无任何 auth,M5 上线 SSO 前必须加邀请码或 IP 白名单)
- [ ] 静态文件目录穿越已修(BUG-003 补丁),验证 `curl https://your-host/../etc/passwd` 返回 index.html
- [ ] 请求体上限已修(BUG-004 补丁),验证 `curl -X POST -d "$(head -c 2M /dev/urandom)" https://your-host/api/state` 返回 413
- [ ] **AI 标识嵌入**: 导出 Markdown 检查文首 banner 与文末水印(PRD-003 §2.2)
- [ ] **首次登录用户协议弹窗**(M5 前可临时跳过,但要有 backlog)
- [ ] 日志服务 / Sentry 接入
- [ ] 备份策略(`backend/data/` 每日打 snapshot)

---

## 6. 关键联系点

| 事项 | 联系方 |
|---|---|
| 产品/PRD 问题 | 千卷 PM(找用户) |
| 主站 SSO/品牌问题 | AI 秘密基地 主站工程 |
| 合规/AI 标识/用户协议 | 法务(待主站指派联系人) |
| 服务器/SSH | 用户(私钥 PM 不持有,且不会持有) |
| 内测作者邀请 | 运营(待指派) |

---

## 7. 你最容易踩的 5 个坑

1. **以为可以直接重命名 backend/ 为 app/ 然后改包名** — 不行。app/ 是 M2 目标结构,服务端逻辑要按映射表逐段拆,不是 rename。
2. **以为 demo 已经是产品** — 不是。demo 缺并发安全(BUG-001)、用户隔离(多租户)、SSO、付费体系。
3. **修改 PRD 文档** — 不要。PRD 是 PM 与法务/工程/设计的契约,要改请提单给 PM。
4. **直接用境外 LLM 发用户作品** — 违反 PRD-002 §7.4 + PRD-003 §6.4 数据出境约束。
5. **以为 P0 补丁堵住了所有 P0** — 没有。BUG-001 和 BUG-006 还没修,完整状态见 §2.2。

---

## 8. 当你卡住的时候

按这个顺序找答案:
1. `pm-deliverables/OVERVIEW-M1-review.md` § 对应章节
2. `pm-deliverables/AUDIT-001-current-state.md`(Bug 与拆分细节)
3. `pm-deliverables/PRD-00X-*.md`(对应主题的具体规约)
4. `PERMANENT_MEMORY.md`(项目过往运行验证记录)
5. **以上都没有,问用户。不要自己猜。**

---

## 9. 第一天行动建议

```
小时 1-2:  读 HANDOFF.md(本文)+ OVERVIEW-M1-review.md
小时 3-4:  本地跑通 demo (start.bat / python backend/server.py)
小时 5-6:  读 AUDIT-001 + INTEGRATION-001,做部署方案 mini-PRD 给用户审
小时 7-8:  收用户反馈,定模式 A / B / C
第 2 天起:  按选定模式开干
```

祝你顺利。**慢就是快**,先理解再动手,不要急着改代码。

---

> **打包日期**: 2026-05-22
> **打包人**: 千卷 PM(Claude Opus 4.7)
> **下游交付**: 接手 AI(模型与厂商不限,看到这个文件即视为接手)
