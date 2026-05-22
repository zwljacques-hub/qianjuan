# PRD-002 · 长篇一致性引擎(Continuity Engine)

> 文档版本: v1.1 · 2026-05-22
> 上游依赖: [PRD-001 双模工作台](PRD-001-dual-mode-workstation.md)、[AUDIT-001 现状审计](AUDIT-001-current-state.md)、[PRD-003 合规与 AI 标识](PRD-003-compliance-ai-labeling.md)
> 适用阶段: M3(W6-9)护城河 — 这是产品最值钱的一份 PRD
> 状态: **Draft,待 review**

---

## 0. 文档定位

本 PRD 定义"长篇一致性引擎"的**核心模块、数据模型、Agent 集成点和验收标准**。这是把我们从"能写 5 万字的 demo"变成"能写 100 万字不崩"的关键工程。

**没有这个引擎,SaaS 上线就是上线即口碑死。**

---

## 1. 问题陈述

### 1.1 长篇 AI 写作的"五大崩溃"

资深网文读者放弃 AI 长篇的真实原因(从已发布作品的扑街评论里归纳):

| # | 崩溃类型 | 典型现象 | 现有 demo 是否会犯 |
|---|---|---|---|
| 1 | **人物漂移** | 主角第 5 章是阴狠少年,第 50 章变成阳光暖男;NPC 第 10 章死了,第 80 章又出现 | **会**(无人物状态机) |
| 2 | **能力通胀** | 第 1 卷主角凝气期,第 2 卷开篇直接元婴;打不过的反派下章无理由变得能打 | **会**(无能力天花板锁) |
| 3 | **经济穿帮** | 第 3 章身上三两银子掏不出酒钱,第 4 章随手赏小二一锭金子 | **会**(无经济流水账) |
| 4 | **伏笔失踪** | 第 5 章埋的悬念到 100 章都没回收;同一伏笔被回收 3 次 | **会**(hooks 无生命周期) |
| 5 | **设定打架** | 第 10 章说断剑是邪修遗物,第 200 章变成上古仙器;同一招式不同章描述完全不同 | **会**(audit 只看本章) |

### 1.2 为什么现有 demo 解决不了

[AUDIT-001](AUDIT-001-current-state.md) 已指出:
- `truthAfter` 字段只是字符串("v12 pending" / "committed")
- `hooks` 数组只有标题,没有生命周期、责任章节、回收记录
- `characters` 是平铺 JSON,没有"上次更新章节"、"能力上限"、"经济流水"
- `audit` agent 只看当前章节,不读取历史章节,无法发现跨章冲突
- 没有任何**生成前的事实注入**机制,LLM 只凭 prompt 拼凑

### 1.3 引擎要解决什么

把"作者自己用脑子记"这件事接管到系统里,让 LLM **生成前先读"事实库",生成后被"事实校验"挑出问题**。

---

## 2. 产品目标与成功度量

### 2.1 引擎能力目标

| 能力 | 目标值 |
|---|---|
| 人物状态追踪 | 任意章节查询任意角色,能拿到该章节生成时的完整状态快照 |
| 能力天花板守护 | 每卷锁定上限,超出时阻止生成或弹警告 |
| 伏笔到期监控 | 到期前 5 章 Director 必须收到提示;到期未回收触发"伏笔焦虑"评分 |
| 设定冲突检测 | Audit Agent 检测设定冲突 F1 ≥ 0.85 |
| 长篇 RAG | 100 章后仍能在 5s 内拿到相关历史片段 |

### 2.2 内测验收(M4)

跑 1 部 20 万字中篇(80 章)端到端,验收门槛:

| 指标 | 通过线 |
|---|---|
| 人物状态前后冲突数 | ≤ 2 处 / 全书 |
| 能力越级数(未在 plan 中预告) | 0 处 |
| 经济穿帮数 | ≤ 1 处 / 全书 |
| 伏笔到期未提示数 | 0 处 |
| 设定冲突未被 audit 抓到的(漏报) | ≤ 5 处 / 全书 |
| audit 误报数(把不冲突的标冲突) | ≤ 10 处 / 全书 |

跑不过这个门槛,**M4 不允许进内测**。

---

## 3. 架构总览

### 3.1 五大模块

```
┌─────────────────────────────────────────────────────────────────┐
│                      长篇一致性引擎                              │
│                                                                 │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐      │
│  │ M1 人物状态机 │   │ M2 能力通胀  │   │ M3 经济流水   │       │
│  │ Character    │   │ Power Cap    │   │ Economy      │       │
│  │ State        │   │ Guard        │   │ Ledger       │       │
│  └──────┬───────┘   └──────┬───────┘   └──────┬───────┘       │
│         │                  │                  │                │
│         └──────────┬───────┴──────────┬───────┘                │
│                    ▼                  ▼                        │
│            ┌────────────────────────────────┐                  │
│            │  Fact Store (Postgres + pgvec) │                  │
│            └───────────┬────────────────────┘                  │
│                        │                                        │
│         ┌──────────────┼──────────────┐                         │
│         ▼              ▼              ▼                         │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐            │
│  │ M4 伏笔生命周期│ │ M5 设定冲突 │ │ M6 续章测试   │           │
│  │ Hook         │ │ Detector    │ │ Regression   │            │
│  │ Lifecycle    │ │ (Audit RAG) │ │ Probe        │            │
│  └──────────────┘ └──────────────┘ └──────────────┘            │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
        │                                              ▲
        │ Pre-generation Inject                        │ Post-generation Check
        ▼                                              │
   ┌─────────────────────────────────────────────────────┐
   │                  Agent Pipeline                      │
   │  planner → scene_writer → audit → reviser → ...    │
   └─────────────────────────────────────────────────────┘
```

### 3.2 引擎在 Agent 流程中的两个介入点

| 介入点 | 时机 | 作用 |
|---|---|---|
| **Pre-generation Inject** | 任何 agent 调 LLM 前 | 查 Fact Store,把"相关事实"作为 system context 注入 |
| **Post-generation Check** | LLM 返回后、写入 state 前 | 用规则 + RAG 检查产出是否违反既有事实,违反则拦截或修正 |

### 3.3 与 PRD-001 工作流的对应关系

```
PRD-001 4 步              一致性引擎介入
─────────────────────────────────────────────
1. 计划                   M4 伏笔(查到期/查未回收)
                          M2 能力(读本卷上限)
2. 正文                   M1 人物(注入当前状态)
                          M3 经济(注入主角流水)
                          所有模块: Pre-inject
3. 打磨                   M5 设定冲突(audit 调 RAG)
                          所有模块: Post-check
4. 归档                   M1/M2/M3/M4 全量更新
                          truthFile 版本号 +1
```

---

## 4. 模块详细设计

### 4.1 M1 · 人物状态机(Character State Machine)

#### 4.1.1 数据模型

每个角色一份"状态档案",按章节快照存储:

```json
{
  "characterId": "char_suhan",
  "name": "苏寒",
  "role": "protagonist",
  "snapshots": [
    {
      "chapter": 12,
      "createdAt": "...",
      "snapshot": {
        "alive": true,
        "location": "青云宗外门演武场",
        "powerLevel": "凝气七层",
        "powerLevelInt": 7,
        "economy": { "spiritStones": 3, "items": ["黑色断剑"] },
        "relations": [
          {"to": "陈玄", "type": "敌对", "intensity": 0.9, "since": 12},
          {"to": "苏家", "type": "断绝", "intensity": 1.0, "since": 1}
        ],
        "skills": ["御剑诀(初通)"],
        "knownSecrets": ["断剑会自发吸血"],
        "languageStyle": "克制、短句、少修辞、偶尔狠话",
        "emotionState": "压抑愤怒",
        "physicalState": "健康",
        "openCommitments": ["三日后生死战", "找回苏家旧物"]
      }
    }
  ],
  "lockedAttributes": {
    "name": "苏寒",
    "gender": "male",
    "languageStyleAnchor": "克制、短句"
  }
}
```

#### 4.1.2 关键字段说明

| 字段 | 必要性 | 说明 |
|---|---|---|
| `snapshots[]` | 必 | 每章归档时追加一条;不是每章全字段更新,而是只记差异?见 §4.1.3 |
| `lockedAttributes` | 必 | 创建角色时锁定,**永不允许修改**(除非用户明确走 `direction_amend_setting`) |
| `powerLevelInt` | 必 | 数值化的能力等级,便于和 M2 通胀守护对比 |
| `openCommitments` | 必 | 该角色未完成的承诺/任务,Director 排下一章时优先看这里 |
| `knownSecrets` | 必 | 该角色知道哪些秘密,避免出现"角色突然知道他不该知道的事" |
| `languageStyle` | 必 | 给 scene_writer 注入,确保对白风格一致 |

#### 4.1.3 快照存储策略

**全量快照 vs 增量快照?** → 用**全量但稀疏**:每章生成时取最近一次快照作为基线,LLM 产出新章后跑 diff,只存有变化的字段;查询时从最近 5 章全量快照合成。

**为什么不纯增量**: 增量回放成本太高,RAG 时不好用。
**为什么不每章全量**: 100 章后存储成本太大。
**折中**: 每 10 章一次全量 anchor + 中间章节增量 patch + 查询时按需合成。

#### 4.1.4 Pre-inject 规则

scene_writer 生成第 N 章前,引擎注入到 system prompt:

```
当前章节: 第 N 章
角色状态:
- 苏寒(主角): 凝气七层 / 青云宗外门 / 灵石 3 颗 / 与陈玄敌对 / 语言风格:克制、短句
- 陈玄(反派): 凝气九层 / 右臂受伤 / 与苏寒敌对
- 长老(背景): 立场未明,默许陈玄递战书

未完成承诺:
- 苏寒: 三日后生死战、找回苏家旧物

禁止违反的设定:
- 苏寒不会主动炫耀,语言克制
- 黑色断剑是苏寒唯一武器,设定为可吸血但来源不明
```

#### 4.1.5 Post-check 规则

LLM 产出后跑 5 类自动检查:

| 检查 | 实现 | 触发动作 |
|---|---|---|
| 角色突然出现/消失 | 对比 `alive` 状态 | 标 blocker |
| 能力越级 | `powerLevelInt` 变化 ≥ 2 阶 | 标 blocker |
| 语言风格漂移 | LLM 调用判断 | 标 minor 警告 |
| 关系突变 | `relations` 强度变化 ≥ 0.5 | 标 major 需用户确认 |
| 锁定属性被改 | 比对 `lockedAttributes` | 标 blocker,自动回滚 |

---

### 4.2 M2 · 能力/经济通胀守护(Power Cap & Inflation Guard)

#### 4.2.1 数据模型

```json
{
  "powerCap": {
    "version": "v3",
    "arcs": [
      { "arcId": "arc_1", "name": "外门篇", "chapters": [1, 30], "protagonistCap": "凝气九层", "protagonistCapInt": 9, "majorCharCap": "筑基" },
      { "arcId": "arc_2", "name": "内门篇", "chapters": [31, 80], "protagonistCap": "筑基", "protagonistCapInt": 12, "majorCharCap": "金丹" }
    ],
    "currentArcId": "arc_1",
    "globalRules": [
      "主角突破必须有触发事件(机缘/秘境/拼死一战)",
      "突破要消耗代价(伤、灵物、时间)",
      "本卷内主角能力不能跳级"
    ]
  }
}
```

#### 4.2.2 Pre-check(planner 阶段)

用户/Director 计划第 N 章时:

```python
def check_plan_against_power_cap(plan, chapter_num):
    arc = find_arc(chapter_num)
    if plan.proposes_protagonist_breakthrough:
        if plan.target_level_int > arc.protagonistCapInt:
            return BlockReason("超出本卷能力上限,需要先开新卷或修改大纲")
    return OK
```

#### 4.2.3 Post-check(scene_writer 阶段)

生成后扫描正文中的能力描述,LLM 提取出"本章后主角能力等级",和 powerCap 比对:
- 超出本卷上限 → blocker,回退本章
- 跳级超过 1 阶(凝气 → 元婴)→ blocker,即使在上限内
- 本章用了未在 plan 中预告的招式 → minor 警告

#### 4.2.4 经济流水(Economy Ledger)

主角(以及关键角色)的资源流水**强制做账**:

```json
{
  "economyLedger": {
    "protagonist": {
      "currentBalance": { "spiritStones": 3, "lowGradeHerb": 5 },
      "history": [
        { "chapter": 5, "delta": { "spiritStones": +10 }, "reason": "外门月例" },
        { "chapter": 8, "delta": { "spiritStones": -7 }, "reason": "购买疗伤丹" },
        { "chapter": 12, "delta": { "spiritStones": -3 }, "reason": "押注自己赢" }
      ]
    }
  }
}
```

**Post-check 规则**: 任何主角动用资源的描述,引擎扫描出"花了多少",和 currentBalance 比对。超支 → blocker。

---

### 4.3 M3 · 经济流水(Economy Ledger)

(并入 M2,见 §4.2.4)

---

### 4.4 M4 · 伏笔生命周期(Hook Lifecycle)

#### 4.4.1 数据模型

```json
{
  "hooks": [
    {
      "id": "hook_001",
      "title": "黑色断剑的来源",
      "category": "origin",           // origin | grudge | promise | mystery | reward
      "status": "open",                // open | hinted | partial | resolved | abandoned
      "introducedAt": 1,               // 章节号
      "dueChapter": 50,                // 期望回收章节(可空 = 长线)
      "weight": "major",               // minor | major | core
      "responsibleAgent": "planner",   // 谁负责管它
      "resolutions": [                 // 已回收次数
        { "chapter": 12, "type": "hint", "summary": "断剑第二次异动" }
      ],
      "constraints": [
        "断剑只对苏寒有反应",
        "不能在本卷揭示真实来源"
      ]
    }
  ]
}
```

#### 4.4.2 6 种状态机迁移

```
open ──hint──→ hinted ──partial reveal──→ partial ──full reveal──→ resolved
  │                                              │
  └─── 超过 dueChapter + 10 章未动 ───────────→ abandoned (扣分)
```

#### 4.4.3 Director 调用规则(planner 阶段)

排第 N 章计划时,引擎自动整理:

```
本章必须涉及(到期前 5 章内的 open / hinted hook):
- hook_001: 黑色断剑的来源(due: 50, 当前章 45,必须开始 partial reveal)

本章可选触发:
- hook_007: 长老的真实立场(due: 60, 当前章 45)

本章禁止解决(weight=core,本卷不能动):
- hook_002: 苏寒身世(due: 200)
```

Director 在生成 plan.mustAdvance / plan.mustNotResolve 时**必须读取这个清单**。

#### 4.4.4 伏笔焦虑评分(Hook Anxiety Score)

每章归档时计算:

```
anxiety = (overdue_hooks * 10) + (long_open_majors * 3) + (orphan_hints * 5)
```

| 分数 | 含义 |
|---|---|
| < 10 | 健康 |
| 10-30 | 警示(下章必须回收一个) |
| > 30 | 危险(强制弹"建议开新卷收线") |

---

### 4.5 M5 · 设定冲突检测(Audit RAG)

#### 4.5.1 现状问题

[AUDIT-001](AUDIT-001-current-state.md) 指出: `audit` agent 只看本章 + 上一章,无法发现"第 200 章和第 10 章设定打架"的情况。

#### 4.5.2 RAG 架构

**事实库(Fact Store)** 用 Postgres + pgvector:

```sql
CREATE TABLE facts (
  id           UUID PRIMARY KEY,
  project_id   TEXT NOT NULL,
  chapter      INT NOT NULL,
  category     TEXT NOT NULL,    -- character | setting | hook | event | rule
  subject      TEXT NOT NULL,    -- "苏寒" / "黑色断剑" / "青云宗"
  fact         TEXT NOT NULL,    -- "断剑会自发吸血,设定为邪修遗物"
  embedding    vector(1024),
  created_at   TIMESTAMPTZ
);

CREATE INDEX ON facts USING ivfflat (embedding vector_cosine_ops);
```

#### 4.5.3 Audit Agent 改造

新流程:

```python
def audit_chapter(state, chapter_num):
    text = get_chapter_text(state, chapter_num)
    
    # 1. LLM 抽取本章涉及的所有"事实声明"
    claims = llm_extract_factual_claims(text)
    # 例: [{"category": "setting", "subject": "黑色断剑", "fact": "断剑是仙器残片"}]
    
    issues = []
    for claim in claims:
        # 2. RAG 查询历史事实
        similar = vector_search(claim, top_k=10, same_subject=True)
        
        # 3. LLM 判断是否冲突
        for old_fact in similar:
            verdict = llm_check_contradiction(claim, old_fact)
            if verdict.is_conflict:
                issues.append({
                    "level": verdict.severity,
                    "location": f"chapter_{chapter_num}",
                    "issue": f"本章声明「{claim.fact}」与第 {old_fact.chapter} 章「{old_fact.fact}」冲突",
                    "fix": verdict.suggested_fix
                })
    
    return issues
```

#### 4.5.4 RAG 性能要求

| 全书章节数 | 查询 P99 | 备注 |
|---|---|---|
| ≤ 50 | < 500ms | 直接全表扫够用 |
| 50-200 | < 1s | ivfflat 索引 |
| 200-400 | < 2s | 加 subject 分区 + 缓存 |

#### 4.5.5 误报控制

LLM 判定冲突可能误报(把不冲突当冲突)。控制策略:
- 设置 `confidence` 字段,< 0.7 不出 issue,只 trace 记录
- 同一冲突连续 2 次出现才升 major(避免单次 LLM hallucination)
- 用户可"标记为不冲突",标记后该 fact 进白名单不再触发

---

### 4.6 M6 · 续章回归测试(Regression Probe)

#### 4.6.1 用途

每次 agent prompt 改动、引擎规则改动后,自动跑一遍**回归测试**,确保改动没让一致性变差。

#### 4.6.2 测试集

人工准备 5 部标杆作品(都是真人写的、口碑好的),每部:
- 取前 30 章为"已写就",作为引擎输入
- 用 LLM + 引擎生成第 31 章
- 与真人写的第 31 章做对比

#### 4.6.3 评分维度

| 维度 | 算法 |
|---|---|
| 人物一致 | 状态机字段对比,不一致字段数 |
| 能力一致 | powerLevel 是否在合理范围 |
| 经济一致 | currentBalance 是否对得上 |
| 伏笔承接 | 是否触及到期 hooks |
| 设定无冲突 | RAG audit 报出的 issue 数 |
| 风格相似 | LLM 嵌入相似度 |

每个维度 0-100 分,加权求总分。引擎改动后总分**不能下降超过 3 分**。

#### 4.6.4 自动运行

- 每次主分支合并触发回归测试(GitHub Actions / 内部 CI)
- 每周一次全量回归
- 每次模型升级(gpt-5.5 → 6.0)做一次全量

---

## 5. 数据模型扩展

### 5.1 顶层 state 新增字段

```json
{
  "continuity": {
    "characterStates": { "char_suhan": {...}, "char_chenxuan": {...} },
    "powerCap": {...},
    "economyLedger": {...},
    "hookLifecycle": [...],
    "factStore": {
      "lastSyncedChapter": 12,
      "factCount": 234
    }
  }
}
```

### 5.2 Postgres 新增表

```sql
-- 事实库(全文索引 + 向量)
CREATE TABLE facts (...);

-- 角色状态快照
CREATE TABLE character_snapshots (
  id           UUID PRIMARY KEY,
  project_id   TEXT,
  character_id TEXT,
  chapter      INT,
  snapshot     JSONB,
  is_anchor    BOOLEAN  -- 是否为 10 章一次的全量 anchor
);

-- 经济流水
CREATE TABLE economy_ledger_entries (
  id           UUID PRIMARY KEY,
  project_id   TEXT,
  character_id TEXT,
  chapter      INT,
  delta        JSONB,
  reason       TEXT
);

-- 伏笔
CREATE TABLE hooks (
  id           UUID PRIMARY KEY,
  project_id   TEXT,
  title        TEXT,
  category     TEXT,
  status       TEXT,
  introduced_at INT,
  due_chapter  INT,
  weight       TEXT,
  constraints  JSONB
);

CREATE TABLE hook_resolutions (
  id           UUID PRIMARY KEY,
  hook_id      UUID REFERENCES hooks(id),
  chapter      INT,
  type         TEXT,
  summary      TEXT
);
```

### 5.3 新增 API 端点

| Method + Path | 用途 |
|---|---|
| `GET /api/continuity/character/{id}/snapshot?chapter=N` | 取某章某角色快照 |
| `GET /api/continuity/power-cap` | 取当前 powerCap |
| `POST /api/continuity/power-cap/update` | 修改 powerCap(影响后续生成) |
| `GET /api/continuity/economy/{characterId}` | 经济流水 |
| `GET /api/continuity/hooks` | 全部伏笔 + 状态 |
| `POST /api/continuity/hooks/{id}/resolve` | 标记伏笔回收 |
| `GET /api/continuity/anxiety` | 当前伏笔焦虑分 |
| `POST /api/continuity/facts/search` | RAG 查询(供 audit 调用) |
| `POST /api/continuity/regression-test/run` | 触发回归测试 |
| `GET /api/continuity/regression-test/latest` | 最近一次回归结果 |

---

## 6. Agent 集成点详细规约

### 6.1 planner

**Pre-inject**:
- 当前 arc 的 powerCap
- 到期前 5 章内的 open / hinted hooks
- 主角当前 openCommitments

**Post-check**:
- plan.mustAdvance 是否包含必推 hook
- plan.mustNotResolve 是否覆盖 weight=core 的 hooks
- 如果 plan 提议主角突破,验证是否符合 powerCap

### 6.2 scene_writer

**Pre-inject**:
- 所有出场角色的最近快照(摘要版,300 字内/角色)
- 主角 economyLedger.currentBalance
- 本章可触及的 hooks 清单

**Post-check**:
- 5 类人物检查(§4.1.5)
- 能力天花板检查(§4.2.3)
- 经济花费检查(§4.2.4)

### 6.3 audit

**Pre-inject**:
- 本章正文
- RAG 查询出的相关历史事实

**Post-check**:
- 把 audit 产出的 issues 写入 state.issues

### 6.4 reviser

**Pre-inject**:
- 本章 issues 列表
- 涉及问题点的相关事实(从 factStore 拉)

**Post-check**:
- 修订后再跑一次 audit 子集(只查被修订段落的相关 facts)

### 6.5 truth_merger(归档)

**Post-update**:
- 扫描本章正文,LLM 提取新增 facts → 写入 factStore + 计算 embedding
- 更新所有出场角色的 snapshot(可能写 anchor)
- 更新 economyLedger
- 更新 hooks 状态
- 计算并写入 anxiety 分

---

## 7. 长篇 RAG 与上下文窗口策略

### 7.1 问题

100 万字长篇,完整文本 = ~150 万 token,任何 LLM 都装不下。

### 7.2 分级 context 策略

| 层级 | 内容 | 字数预算 |
|---|---|---|
| **永久 anchor** | 项目级核心设定(三层真相文件 v1) | 2000 字 |
| **当前 arc** | 当前卷大纲 + arc 内所有 hooks 摘要 | 3000 字 |
| **近况** | 最近 5 章摘要 | 5000 字 |
| **本章** | 本章 plan + scenes | 3000 字 |
| **RAG 召回** | 按查询拉相关历史片段 | 5000 字 |
| **角色注入** | 出场角色当前快照 | 3000 字 |
| **合计** | | **~21000 字** |

按 1 字 ≈ 1.5 token,约 31500 token。GPT-5.5 / Claude 4.7 上下文都装得下,留出生成空间。

### 7.3 摘要更新策略

- 每章归档后,truth_merger 同时更新"最近 5 章摘要"和"当前 arc 摘要"
- 跨 arc 时,旧 arc 浓缩为 500 字进永久 anchor

### 7.4 ⚠ 数据出境约束(受 PRD-003 §6.4 约束)

**关键工程约束**: 境外 LLM(GPT / Claude)调用时,用户**作品原文不出境**,只发脱敏 context。

#### 7.4.1 模型分级

| 模型类别 | 处理边界 | 适用场景 |
|---|---|---|
| **境内合规模型**(deepseek / 通义千问 / 文心一言 / 智谱 GLM) | 可发原文 | 默认全部 agent 调用 |
| **境外模型**(GPT-5.5 / Claude-4-7) | 仅发脱敏 context | 用户明确同意后启用 |

#### 7.4.2 脱敏 context 是什么

发到境外模型的 context **不能包含**:
- 用户作品的连续正文段落(超过 50 字的原文片段)
- 用户角色姓名、地名、独特设定的具体名词

发到境外模型的 context **可以包含**:
- 结构化事实("主角凝气七层,与反派敌对")
- 数值化状态("当前章节:12 / 400")
- 抽象任务指令("写一段冲突对白,300 字内")
- LLM 自身刚生成的内容(即时回送,不属于"用户作品")

#### 7.4.3 对 §7.2 分级 context 的影响

| 层级 | 境内模型(默认) | 境外模型(用户明确同意) |
|---|---|---|
| 永久 anchor | 全量发送 | 脱敏后发送(具体姓名替换为代号) |
| 当前 arc | 全量发送 | 脱敏后发送 |
| 近况摘要 | 全量发送 | 仅发结构化事实摘要 |
| 本章 plan | 全量发送 | 仅发结构化目标 |
| RAG 召回 | 全量发送 | **不发**(改为只发角色 ID + 状态) |
| 角色快照 | 全量发送 | 全量发送(本就是结构化数据) |

→ 境外模型下,有效 context 字数从 ~21000 字降至 ~8000 字。**生成质量会下降**。

#### 7.4.4 工程实现

```python
def build_context(state, agent, target_model):
    if target_model.is_domestic:
        return full_context(state)
    else:
        return desensitized_context(state)
```

`desensitized_context`:
- 用 NER 抽取人名/地名/独特名词
- 用代号 `[CHAR_001]` `[LOC_002]` `[SETTING_003]` 替换
- 替换映射保存在请求 ID 下,返回结果时还原

#### 7.4.5 默认策略

- M4 内测前**默认全用境内模型**,境外模型 opt-in
- 用户 settings 里有"启用境外模型(质量更高)"开关,默认关
- 开启时弹一次合规告知

#### 7.4.6 对成本的影响

境内模型当前价格略低于境外,且更稳定。这反而是好消息:
- deepseek-chat: ¥0.001 / 1k token(国内)
- gpt-5.5: ¥0.06 / 1k token(假设值,境外)

**境内优先反而省钱**。仅在境内模型质量不够(如长篇情感戏)时,用户主动 opt-in 境外。

---

## 8. 失败与降级策略

### 8.1 引擎本身失败

| 故障 | 降级 |
|---|---|
| Postgres 不可用 | 回落到 JSON 文件(M2 兼容);引擎 read-only |
| pgvector 查询失败 | RAG 降级为按 chapter 范围拉文本 |
| LLM 抽取 facts 失败 | 跳过本章 facts 入库,trace 记录,下次手动触发回填 |
| 状态机 diff 出错 | 保留旧快照,本章不更新,Audit 弹"状态机更新失败"警告 |

### 8.2 用户主动覆盖

引擎是工具不是警察。用户始终可以:
- 在 Studio 模式手动改 character snapshot
- 在 settings 里关掉某类检查
- 标记"我知道这是冲突,继续"

但**关掉检查不影响**引擎数据持续累积,用户后悔时随时可重新开启。

### 8.3 引擎判断错误怎么办

- audit 误报 → 用户点"忽略此 issue" → 白名单
- 状态机字段误更新 → 用户手动改 snapshot → 修正后所有快照参考此最新版本
- powerCap 设置过严 → 用户在 Studio 改 powerCap

---

## 9. 实施路线(M3 4 周拆解)

### W6 · 基础设施

- [ ] Postgres + pgvector 部署
- [ ] facts / character_snapshots / hooks 表 schema 建立
- [ ] 从现有 JSON state 迁移到 Postgres 的脚本
- [ ] 引擎服务骨架(`app/continuity/` 模块)
- [ ] 基础 API 端点 ≥ 5 个

### W7 · 人物状态机 + 能力/经济

- [ ] M1 完整实现 + Pre-inject 接入 scene_writer
- [ ] M2 实现 + Pre-check 接入 planner
- [ ] economyLedger 实现 + Post-check 扫描
- [ ] 配合修 [BUG-007](AUDIT-001-current-state.md)(hardcoded suHan key)

### W8 · 伏笔 + RAG audit

- [ ] M4 伏笔生命周期完整状态机
- [ ] anxiety 计算
- [ ] facts 抽取 + embedding 入库
- [ ] M5 audit RAG 重写
- [ ] Studio 模式资料库右抽屉接入新数据

### W9 · 回归测试 + 端到端验证

- [ ] M6 回归测试框架
- [ ] 准备 5 部标杆作品测试集
- [ ] 端到端跑 1 部 20 万字中篇,达 §2.2 验收门槛
- [ ] 修出来的所有问题归档,进 M4 内测前再扫一遍

---

## 10. 风险与对冲

| 风险 | 概率 | 影响 | 对冲 |
|---|---|---|---|
| audit RAG 误报率高 | 高 | 用户体验崩 | 准备人工标注集 200 条,M3 末跑 baseline,> 误报率 15% 不上线 |
| LLM 抽取 facts 不稳定 | 高 | factStore 数据脏 | 用 Haiku 4.5 抽取,gpt-5.5 校验,两轮一致才入库 |
| 100 章后状态机查询慢 | 中 | 生成体验降级 | M3 W9 必须做 100 章压测,P99 < 2s 才放行 |
| 用户嫌引擎"管太严" | 中 | 流失 | 所有检查可关,但默认开 |
| Postgres 部署运维复杂 | 中 | M2 进度受影响 | M2 W5 必须完成 Postgres on-call 演练 |
| pgvector 在百万级 facts 上不够快 | 低 | 远期问题 | M5 后看情况上 Qdrant / Milvus |

---

## 11. 验收 checklist

M3 W9 末:

- [ ] 5 部标杆中篇全部跑完,达 §2.2 验收 6 项门槛
- [ ] 人物状态机能在任意章节回放任意角色状态
- [ ] powerCap 阻挡过越级生成 ≥ 3 次(测试中)
- [ ] economyLedger 抓出过经济穿帮 ≥ 3 处(测试中)
- [ ] 伏笔焦虑分能正确触发预警
- [ ] audit RAG 在 80 章测试集上 F1 ≥ 0.85
- [ ] 100 章状态查询 P99 < 2s
- [ ] 引擎所有数据用户均可手动修改
- [ ] 引擎所有检查用户均可在 settings 关闭
- [ ] 回归测试 CI 跑通

---

## 12. 未在本 PRD 解决的事项

| 议题 | 留待 |
|---|---|
| 5 部标杆作品具体怎么选 | M3 W6 开始时决定(避免版权问题,用公版 / 自己授权) |
| LLM 抽取 facts 的具体 prompt 与 schema | 工程实施时 |
| 多角色同时出场时 Pre-inject 字数预算如何分配 | 工程实施时,先按"主角 1000 字、配角 500 字、群演 200 字"试 |
| 用户协作场景下,谁负责裁决冲突 | M5 协作版本时 |
| 跨书引擎(同作者多本书共享某些设定) | 远期 |

---

## 13. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-05-22 | 初稿。5 大模块 + Fact Store + RAG audit + 回归测试。 |
| v1.1 | 2026-05-22 | 新增 §7.4 数据出境约束,境内模型为默认,境外模型 opt-in 且仅发脱敏 context。受 PRD-003 §6.4 约束。 |

---

> **本 PRD 决定产品能不能上线。** 5 大模块缺任何一个,长篇都会崩。
> 评审人: 产品总监 / 工程负责人 / 算法负责人 / 至少 1 位真实签约作者
