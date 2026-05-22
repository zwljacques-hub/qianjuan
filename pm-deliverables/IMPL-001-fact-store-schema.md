# IMPL-001 · Fact Store Schema 设计稿

> 文档版本: v1.0 · 2026-05-23
> 上游依赖: [PRD-002 长篇一致性引擎](PRD-002-continuity-engine.md)
> 适用阶段: M3 W6 · 基础设施
> 状态: **D1 交付,待用户审,审通过后 D2 开始 Postgres 部署**
> 撰写: 千卷接手工程(Claude Opus 4.7)

---

## 0. 文档定位

本文档把 PRD-002 §4-5 的"Fact Store"从概念落到**可直接 `psql -f` 执行的 SQL DDL**。

落地范围:
- 所有 Postgres 表的完整字段、类型、约束、索引
- 多租户隔离策略(`user_id` 列下推到每张表)
- 增量 + Anchor 快照策略(PRD-002 §4.1.3)的索引支持
- pgvector 向量索引参数(维度、距离度量、`ivfflat` `lists` 值)
- 查询模式与性能预算映射(每个 PRD-002 §4.5.4 SLA 对应一条索引)
- Python 一致性引擎客户端接口形状(供 D3 开工时直接 stub)
- 与现有 JSON state 的字段对应(供 D12 迁移脚本)

**本文档不解决**:
- LLM fact extraction 的具体 prompt(D3-D4 工作)
- 业务逻辑(状态机迁移、能力上限校验)(D5-D10 工作)
- 部署运维(D2 工作)

---

## 1. 设计原则

### 1.1 三个核心原则

| 原则 | 体现 |
|---|---|
| **多租户隔离硬约束** | 所有数据表第一列必须是 `user_id TEXT NOT NULL`,联合索引必含 `user_id` 前缀 |
| **JSONB 不是逃避**,结构化字段优先 | 能拍平成列就拍平,只把"可能演化"的字段塞 JSONB |
| **写少读多** | 写路径只在 `truth_merger` 归档时触发(每章 1 次),读路径在每个 LLM 调用前触发(每章 10+ 次)。索引为读优化 |

### 1.2 三个 anti-pattern 必须避开

| Anti-pattern | 后果 | 我们的对策 |
|---|---|---|
| 一张大 `state` 表 JSONB 万能 | 查不动,改不动,索引帮不上 | 拆 6 张表,每张单一职责 |
| 每章存全量快照 | 100 章 100MB JSONB,vector embedding 暴涨 | Anchor + 增量(§4) |
| pgvector 无 `lists` 调优 | 100 万 facts 后查询退化为 O(N) | §7 给出 `lists` 公式 |

### 1.3 命名约定

- 表名: `snake_case`,复数(`facts` `hooks`)
- 列名: `snake_case`,布尔 `is_xxx` `has_xxx`
- 索引名: `idx_<table>_<cols>`
- 外键名: `fk_<from_table>_<to_table>`
- 约束名: `ck_<table>_<col>_<rule>`

---

## 2. 多租户与项目层次

```
user (Cookie qj_uid, 临时身份;M5 SSO 后切真实 user_id)
  └── project (一本书)
        └── chapter (一章)
              └── fact / snapshot / ledger_entry / hook_resolution
```

**强约束**:
- 所有表第一列 `user_id TEXT NOT NULL`,第二列 `project_id TEXT NOT NULL`
- 行级权限层(M5 SSO 之后)直接在所有 `WHERE` 加 `AND user_id = $current_uid`
- 当前 M3 阶段无 row-level security,在 Python 层强制带 `user_id`

---

## 3. 表 1: `facts`(事实库 · 核心)

### 3.1 用途

存储 LLM 从章节正文里抽取的所有"事实声明",供 audit RAG 检索做冲突检测(PRD-002 §4.5)。

### 3.2 DDL

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- 给 subject/fact 字段做模糊检索备用

CREATE TABLE facts (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         TEXT NOT NULL,
  project_id      TEXT NOT NULL,

  chapter         INT NOT NULL,
  category        TEXT NOT NULL,
  subject         TEXT NOT NULL,
  fact            TEXT NOT NULL,
  fact_norm       TEXT NOT NULL,                -- 归一化版本(去标点、去停用词)用于精确去重
  confidence      REAL NOT NULL DEFAULT 1.0,    -- LLM 抽取置信度 [0,1]
  source_span     TEXT,                         -- 原文片段(截 200 字内)便于追溯

  embedding       vector(1024),                 -- bge-large-zh-v1.5 或同维度模型

  superseded_by   UUID REFERENCES facts(id),    -- 被新事实覆盖时指向后继
  is_user_edited  BOOLEAN NOT NULL DEFAULT FALSE, -- 用户手动改过的事实(白名单不再触发冲突)

  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  CONSTRAINT ck_facts_category CHECK (category IN ('character', 'setting', 'item', 'place', 'rule', 'event', 'relation')),
  CONSTRAINT ck_facts_confidence CHECK (confidence >= 0 AND confidence <= 1),
  CONSTRAINT ck_facts_chapter CHECK (chapter >= 0)
);

-- 主访问模式 1: 按 project + subject 查相关事实(audit RAG 召回前的精确预过滤)
CREATE INDEX idx_facts_project_subject ON facts(user_id, project_id, subject);

-- 主访问模式 2: 时间范围查询(查"第 50 章前关于陈玄的所有 fact")
CREATE INDEX idx_facts_project_chapter ON facts(user_id, project_id, chapter);

-- 主访问模式 3: 向量相似度检索(RAG 主路径)
-- 注: ivfflat 的 lists 建议 = sqrt(预期行数);M3 上线时按 10000 facts 算约 100
CREATE INDEX idx_facts_embedding ON facts
  USING ivfflat (embedding vector_cosine_ops)
  WITH (lists = 100);

-- 辅助: 全文搜兜底,pgvector 不可用时降级
CREATE INDEX idx_facts_fact_trgm ON facts USING gin (fact gin_trgm_ops);

-- 辅助: 去重检查
CREATE UNIQUE INDEX uq_facts_norm ON facts(user_id, project_id, subject, fact_norm)
  WHERE superseded_by IS NULL;
```

### 3.3 category 取值语义

| 值 | 含义 | 示例 fact |
|---|---|---|
| `character` | 关于某角色的描述 | "苏寒目前凝气七层" |
| `setting` | 世界观/规则 | "青云宗外门月例是 3 颗灵石" |
| `item` | 物品/道具的属性 | "黑色断剑可被动吸收剑气,来源不明" |
| `place` | 地点的属性 | "外门演武场位于青云宗东南角" |
| `rule` | 系统/秩序的规则 | "外门生死战必须长老见证" |
| `event` | 已发生的事件 | "第 12 章,陈玄递战书给苏寒" |
| `relation` | 角色关系(不放进 character_snapshots 因为关系是有向的) | "苏寒与陈玄敌对,强度 0.9" |

### 3.4 superseded_by 用法

事实演化场景: 第 12 章 fact "苏寒凝气七层" → 第 25 章 fact "苏寒凝气九层"

写入第 25 章 fact 时:
1. 查同 subject+category 的最新 fact
2. 如果有,新 fact 的 `superseded_by` 设为新 ID,**老 fact 的 `superseded_by` 反向指向新 fact**(不是新 fact 指向老,而是老指向新,语义"我被它接替")
3. RAG 检索时默认只查 `WHERE superseded_by IS NULL`(取最新)
4. 冲突检测时反向追溯链(取整条演化轨迹)

### 3.5 是否会有 100 万行?

按 PRD-002 验收门槛:100 万字 / 400 章 / 中篇约 12 facts/章 = ~5000 facts/书。
单个 user 1 本书 5000 行,10 本书 5 万行,1000 用户级别才到百万。`lists=100` 撑到这个量没问题。

---

## 4. 表 2: `character_snapshots`(人物状态快照)

### 4.1 用途

PRD-002 §4.1 人物状态机,**全量稀疏 + 10 章 anchor** 策略。

### 4.2 DDL

```sql
CREATE TABLE character_snapshots (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         TEXT NOT NULL,
  project_id      TEXT NOT NULL,
  character_id    TEXT NOT NULL,                -- 业务 ID,如 "char_suhan"

  chapter         INT NOT NULL,
  is_anchor       BOOLEAN NOT NULL DEFAULT FALSE,
  -- anchor=true: 该章节存的是全量快照(每 10 章一次)
  -- anchor=false: 该章节存的是与上一个 anchor 的 diff (JSON Patch RFC 6902)

  snapshot        JSONB NOT NULL,
  -- 当 is_anchor=true 时是完整对象;is_anchor=false 时是 patch ops 数组

  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  CONSTRAINT uq_char_snap_unique UNIQUE (user_id, project_id, character_id, chapter)
);

-- 主访问模式 1: 取某角色某章节状态(合成 = anchor + 中间 patches)
CREATE INDEX idx_char_snap_lookup
  ON character_snapshots(user_id, project_id, character_id, chapter DESC);

-- 主访问模式 2: 取所有 anchor(用于 100 章后查询时快速定位最近 anchor)
CREATE INDEX idx_char_snap_anchors
  ON character_snapshots(user_id, project_id, character_id, chapter DESC)
  WHERE is_anchor = TRUE;
```

### 4.3 snapshot JSONB schema(锁定字段)

对应 PRD-002 §4.1.1。**Python 端用 Pydantic 强校验**,DB 端不加 CHECK(JSONB 校验性能差),应用层守门。

```typescript
// 类型描述(给 D3-D4 工程参考)
type CharacterSnapshot = {
  alive: boolean
  location: string
  powerLevel: string          // "凝气七层"
  powerLevelInt: number       // 7
  economy: { spiritStones: number, items: string[] }
  relations: Array<{ to: string, type: string, intensity: number, since: number }>
  skills: string[]
  knownSecrets: string[]
  languageStyle: string
  emotionState: string
  physicalState: string
  openCommitments: string[]
}
```

### 4.4 lockedAttributes 单独成表

角色锁定属性(`name` `gender` `languageStyleAnchor`)单独存,因为它们是项目级别 invariant,不随章节演化:

```sql
CREATE TABLE character_locked_attrs (
  user_id         TEXT NOT NULL,
  project_id      TEXT NOT NULL,
  character_id    TEXT NOT NULL,
  locked_attrs    JSONB NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (user_id, project_id, character_id)
);
```

PRD-002 §4.1.5 "锁定属性被改 → 标 blocker,自动回滚" 由应用层在写入前 diff 这张表实现。

---

## 5. 表 3-4: 经济流水

### 5.1 用途

PRD-002 §4.2.4 经济流水,每笔资源进出强制做账。

### 5.2 DDL

```sql
-- 资源余额快照(每章末当前状态)
CREATE TABLE economy_balances (
  user_id         TEXT NOT NULL,
  project_id      TEXT NOT NULL,
  character_id    TEXT NOT NULL,
  chapter         INT NOT NULL,
  balance         JSONB NOT NULL,
  -- {"spiritStones": 3, "lowGradeHerb": 5, ...}
  PRIMARY KEY (user_id, project_id, character_id, chapter)
);

-- 流水(每笔进出)
CREATE TABLE economy_ledger (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         TEXT NOT NULL,
  project_id      TEXT NOT NULL,
  character_id    TEXT NOT NULL,
  chapter         INT NOT NULL,
  delta           JSONB NOT NULL,
  -- {"spiritStones": -7}  (负为支出)
  reason          TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_economy_ledger_lookup
  ON economy_ledger(user_id, project_id, character_id, chapter);
```

### 5.3 为什么分两张表

- `economy_ledger` 是 append-only 流水,审计追溯用
- `economy_balances` 是每章末派生的余额快照,快速查"第 N 章主角口袋多少钱"
- 写入路径: truth_merger 归档时,先 append 流水,再用 `LAST(balance) + SUM(delta WHERE chapter=N)` 派生新 balance

---

## 6. 表 5-6: 伏笔生命周期

### 6.1 用途

PRD-002 §4.4 伏笔生命周期,6 状态机迁移 + 焦虑分。

### 6.2 DDL

```sql
CREATE TABLE hooks (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id               TEXT NOT NULL,
  project_id            TEXT NOT NULL,

  title                 TEXT NOT NULL,
  category              TEXT NOT NULL,
  status                TEXT NOT NULL DEFAULT 'open',
  introduced_at         INT NOT NULL,           -- 引入章节
  due_chapter           INT,                    -- 期望回收章节,NULL = 长线
  weight                TEXT NOT NULL,
  responsible_agent     TEXT NOT NULL DEFAULT 'planner',
  constraints           JSONB NOT NULL DEFAULT '[]'::jsonb,
  is_user_edited        BOOLEAN NOT NULL DEFAULT FALSE,

  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  CONSTRAINT ck_hooks_status CHECK (status IN ('open', 'hinted', 'partial', 'resolved', 'abandoned')),
  CONSTRAINT ck_hooks_category CHECK (category IN ('origin', 'grudge', 'promise', 'mystery', 'reward')),
  CONSTRAINT ck_hooks_weight CHECK (weight IN ('minor', 'major', 'core'))
);

CREATE INDEX idx_hooks_project_status
  ON hooks(user_id, project_id, status);

-- 给 Director 排计划时查 "本章必须涉及的 hooks"
CREATE INDEX idx_hooks_due
  ON hooks(user_id, project_id, due_chapter)
  WHERE status IN ('open', 'hinted', 'partial');


CREATE TABLE hook_resolutions (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         TEXT NOT NULL,
  project_id      TEXT NOT NULL,
  hook_id         UUID NOT NULL REFERENCES hooks(id) ON DELETE CASCADE,
  chapter         INT NOT NULL,
  resolution_type TEXT NOT NULL,                -- 'hint' | 'partial' | 'full' | 'abandon'
  summary         TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  CONSTRAINT ck_hook_res_type CHECK (resolution_type IN ('hint', 'partial', 'full', 'abandon'))
);

CREATE INDEX idx_hook_res_hook ON hook_resolutions(hook_id, chapter);
```

### 6.3 焦虑分(派生视图)

```sql
CREATE OR REPLACE VIEW v_hook_anxiety AS
SELECT
  user_id,
  project_id,
  -- overdue: 过了 due_chapter 还没 resolved
  COUNT(*) FILTER (WHERE status NOT IN ('resolved', 'abandoned')
                     AND due_chapter IS NOT NULL
                     AND due_chapter < (SELECT MAX(chapter) FROM economy_balances WHERE economy_balances.project_id = hooks.project_id))
    AS overdue_count,
  -- long_open_majors: weight=major 且 open 超过 20 章
  COUNT(*) FILTER (WHERE status = 'open' AND weight = 'major'
                     AND introduced_at + 20 < (SELECT MAX(chapter) FROM economy_balances WHERE economy_balances.project_id = hooks.project_id))
    AS long_open_majors,
  -- orphan_hints: 有 hint 但 8 章后没续动作
  COUNT(*) FILTER (
    WHERE status = 'hinted'
    AND (SELECT MAX(chapter) FROM hook_resolutions WHERE hook_resolutions.hook_id = hooks.id) + 8
        < (SELECT MAX(chapter) FROM economy_balances WHERE economy_balances.project_id = hooks.project_id)
  ) AS orphan_hints
FROM hooks
GROUP BY user_id, project_id;
```

应用层 anxiety 计算:`overdue × 10 + long_open × 3 + orphan × 5`

> 注:上面 view 用了 economy_balances 反查当前章节,M3 W7 工程化时改为传参,view 仅做形状参考。

---

## 7. 表 7: `power_cap` & 项目元数据

### 7.1 用途

PRD-002 §4.2 能力天花板,按 arc 锁上限。

### 7.2 DDL

```sql
CREATE TABLE project_meta (
  user_id         TEXT NOT NULL,
  project_id      TEXT NOT NULL,
  power_cap       JSONB NOT NULL DEFAULT '{}'::jsonb,
  -- {
  --   "version": "v3",
  --   "arcs": [{...}],
  --   "currentArcId": "arc_1",
  --   "globalRules": [...]
  -- }
  arc_index       JSONB NOT NULL DEFAULT '[]'::jsonb,
  -- 冗余索引: [{"arcId":"arc_1","chapters":[1,30],"capInt":9}, ...]
  -- 用 JSONB 而不是单独表,因为 arc 不超过 10 个,且写入路径低频
  current_arc_id  TEXT,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (user_id, project_id)
);
```

---

## 8. pgvector 向量配置

### 8.1 维度选择

| 选项 | 维度 | 中文质量 | 成本 |
|---|---|---|---|
| `bge-large-zh-v1.5` | 1024 | 好(中文专精) | 自部署免费 |
| `text-embedding-3-small` | 1536 | 一般 | OpenAI $0.02/M tok |
| `text-embedding-3-large` | 3072 | 好 | OpenAI $0.13/M tok |
| `qwen-text-embedding-v3` | 1024 | 好(国产) | 阿里云便宜 |

**选 1024 维**:对应 bge-large-zh-v1.5 或 qwen embedding-v3。中文质量好,国产合规(PRD-002 §7.4 数据出境约束),pgvector 索引性能可接受。

### 8.2 索引选择

```sql
CREATE INDEX idx_facts_embedding ON facts
  USING ivfflat (embedding vector_cosine_ops)
  WITH (lists = 100);
```

**`lists = 100`** 是 M3 上线时的初始值,SQRT(预期行数 ≈ 10000) ≈ 100。

| 数据量 | 建议 lists | 重建索引 |
|---|---|---|
| ≤ 10000 | 100 | M3 初始 |
| 10000 - 100000 | 316 (√100k) | 用户增长到中规模时 ALTER INDEX |
| 100000+ | 1000 | 切到 HNSW 索引 |

### 8.3 距离度量

`vector_cosine_ops` — 余弦相似度。embedding 模型输出归一化向量,余弦和点积等价但语义更清晰。

### 8.4 查询模式

```sql
-- 给定一个 fact embedding,查相似的 top-k 历史 fact
SELECT id, chapter, subject, fact, embedding <=> $1 AS distance
FROM facts
WHERE user_id = $2 AND project_id = $3
  AND subject = $4              -- 精确预过滤,大幅缩小扫描范围
  AND superseded_by IS NULL
ORDER BY embedding <=> $1
LIMIT 10;
```

性能预算(对应 PRD-002 §4.5.4):

| 全书章节 | facts 量 | 预期 P99 |
|---|---|---|
| ≤ 50 | ~600 | < 200ms |
| 50-200 | ~2400 | < 500ms |
| 200-400 | ~5000 | < 1s |

PRD-002 §4.5.4 给的 SLA 是 200-400 章 < 2s,我们目标 < 1s,留 1s 缓冲做应用层处理。

---

## 9. JSON state → Postgres 字段映射(D12 迁移)

| 现有 JSON state 字段 | 目标表 | 备注 |
|---|---|---|
| `state.project.id` | 所有表的 `project_id` | 主键骨干 |
| `state.characters.{key}` | `character_snapshots`(anchor) | 一次性写入最早 anchor |
| `state.characters.{key}.constraints` | `character_locked_attrs` | 转结构化 |
| `state.plan.readerPromise` etc | `project_meta.power_cap` 周边 | 复用 |
| `state.hooks[]` | `hooks` | 字段名映射 + status 取值修正 |
| `state.scores[]` | **不迁移** | scores 是 audit 输出,本身是派生数据 |
| `state.issues[]` | **不迁移** | issues 仍然在 state JSON 里(短期) |
| `state.trace[]` | **不迁移** | 调试日志,留在 state JSON 里 |
| `state.truthAfter` | `project_meta` 加 `truth_version_int` 列 | 数字化 |

**迁移原则**: 不破坏 state JSON,Postgres 是**补充**,等 M2 工程地基拆完才考虑切换主存储。

---

## 10. Python 客户端接口形状(D3 工程师直接参考)

```python
# app/store/fact_store.py
class FactStore:
    def __init__(self, pool: asyncpg.Pool, embedder: Callable[[str], list[float]]):
        ...

    async def upsert_facts(
        self,
        user_id: str,
        project_id: str,
        chapter: int,
        facts: list[FactInput],  # Pydantic model
    ) -> list[UUID]:
        """Truth-merger 归档时调用。逐条算 embedding + 插入 + 处理 supersedes。"""

    async def search_similar(
        self,
        user_id: str,
        project_id: str,
        query_fact: str,
        subject: str | None = None,
        top_k: int = 10,
    ) -> list[FactWithScore]:
        """Audit RAG 检索。"""

    async def get_subject_history(
        self,
        user_id: str,
        project_id: str,
        subject: str,
        upto_chapter: int,
    ) -> list[Fact]:
        """取某个 subject 的全部演化轨迹(供冲突检测追溯链)。"""

# app/store/character_store.py
class CharacterStore:
    async def write_snapshot(
        self,
        user_id: str,
        project_id: str,
        character_id: str,
        chapter: int,
        snapshot: CharacterSnapshot,
        force_anchor: bool = False,
    ) -> None:
        """归档时调用。is_anchor 由 chapter % 10 == 0 自动判定。"""

    async def get_snapshot_at(
        self,
        user_id: str,
        project_id: str,
        character_id: str,
        chapter: int,
    ) -> CharacterSnapshot:
        """合成查询: 取最近 anchor + 中间 patches → 应用得到目标章节快照。"""
```

---

## 11. 部署与运维要求(D2 实操)

### 11.1 Postgres 版本

- **PostgreSQL 16+**(支持 `gen_random_uuid()` without `pgcrypto`)
- **pgvector ≥ 0.7.0**(支持 HNSW,即使初期用 ivfflat)
- **pg_trgm**(全文搜兜底)

### 11.2 服务器侧部署形态

3 个选项,按运维成本排序:

| 方案 | 优 | 劣 |
|---|---|---|
| **A. Docker compose `postgres:16` + `pgvector/pgvector:pg16`** | 上线快、可复制 | 与现有 new-api 共享 Docker 网络要注意端口 |
| B. 系统 `apt install postgresql-16` + 手装 pgvector | 调优灵活 | 装 pgvector 要编译,容易踩坑 |
| C. 云托管 PG(阿里云 RDS) | 免运维 | 月费 + 数据从 LLM 服务跨网 |

**M3 W6 选 A**。和 `new-api-prod-postgres-1` 容器同一台机器,内网通信。

### 11.3 资源预算

- M3 内测期:< 100 用户 × 5 万 facts/用户 = 500 万 facts,**约 5GB**(含 embedding)
- 内存: shared_buffers 至少 1GB,work_mem 32MB
- 备份: pg_dump 每日 + WAL 归档(交给 M2 W5 on-call 演练)

### 11.4 连接管理

Python 应用层用 `asyncpg` + 连接池:
- pool min=2 max=10
- 超时 30s
- 准备好降级到只读模式(查询走读副本,写入失败时落 JSON 兜底)

---

## 12. 风险与对冲(本设计可能踩的坑)

| 风险 | 缓解 |
|---|---|
| `superseded_by` 链回溯递归查询慢 | 限制最长追溯 5 跳;超出标 "事实演化过多" 警告 |
| JSONB 字段散乱无校验 | Pydantic 在 Python 层强校验,DB CHECK 暂不加 |
| anchor + patch 合成读路径慢 | 加内存缓存(`functools.lru_cache(maxsize=1000)`)|
| vector embedding API 调用昂贵 | 批量 embedding(每章 12 facts 一次 API 调用);抽 facts 失败时降级写空向量 + 标 `embedding_pending=true`,异步补 |
| 多用户共享 Postgres,某用户量大拖累其他 | M3 阶段单实例,M4 起加资源 quota(每 user 总 facts 上限 50000) |

---

## 13. 与现有 server.py 的对接计划(D2 落地)

### 13.1 不动 server.py 主体

`server.py` 现在的 JSON StateStore **保留**。Postgres 只承载新引入的"连续性"数据,不替换 StateStore。

### 13.2 新加 Python 模块树

```
app/
├── store/
│   ├── __init__.py
│   ├── pg_pool.py            # asyncpg 连接池
│   ├── fact_store.py         # facts 表 CRUD
│   ├── character_store.py    # character_snapshots
│   ├── economy_store.py      # economy_ledger + balances
│   ├── hook_store.py         # hooks + resolutions
│   └── project_meta_store.py # project_meta
├── llm/
│   ├── fact_extractor.py     # D3-D4 主交付
│   └── embedder.py           # 调 bge / qwen embedding API
└── continuity/
    ├── __init__.py
    ├── character_machine.py  # D5
    ├── power_cap.py          # D6
    ├── economy_guard.py      # D7
    ├── hook_tracker.py       # D8
    └── audit_rag.py          # D9-D10
```

### 13.3 与现有 server.py 的接入点

仅 2 个改动点:

1. **truth_merger 归档时**(server.py 现有的 `/api/truth/settle` 等端点),调用 `app/continuity/*` 各模块的 `on_chapter_archived(state, chapter_num)` 钩子,落 Postgres
2. **audit 调用前**(server.py 现有的 `/api/audit/run` 端点),调用 `app/continuity/audit_rag.py:retrieve_relevant_facts()`,把召回的 facts 注入 audit 的 system prompt

其他所有 LLM 调用、UI 路径**完全不动**。这保证降级路径:Postgres 挂了,引擎跳过,原 demo 流程照跑(对应 PRD-002 §8.1)。

---

## 14. 验收(D1 末)

- [x] PRD-002 全文已读
- [x] 6 张核心表 DDL 落稿
- [x] pgvector 配置 + 索引方案确定
- [x] 多租户 user_id 列下推到每张表
- [x] JSON state 迁移字段映射表
- [x] Python 客户端接口形状
- [x] 部署形态选定(Docker compose A 方案)
- [x] 与 server.py 不破坏现有功能的接入路径
- [ ] **用户审核** ← 卡这里

---

## 15. 未在本文档解决的事项

| 议题 | 留待 |
|---|---|
| LLM 抽 fact 的具体 prompt 模板 | D3-D4 |
| Embedding 模型最终选型(bge vs qwen v3) | D2 末压测决定 |
| 多角色同时出场时 Pre-inject 字数预算分配 | D5 工程实施时 |
| 角色 ID 命名规范(slugify? UUID? 业务 key?) | D5 工程实施时,先按"`char_<pinyin>`"试 |
| 长篇 RAG 100+ 章性能压测 | D13 E2E 测试时一并跑 |
| HNSW 索引何时切换(替代 ivfflat) | M3 后期看真实查询 P99 决定 |

---

## 16. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-05-23 | D1 初稿。6 张表 DDL + pgvector 参数 + Python 客户端接口 + 部署形态。 |

---

> **本文档审通过 = D2 可以上 Postgres 了**。
> 没审通过的地方告诉我,我改。
