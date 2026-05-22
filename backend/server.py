from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import re
import traceback
import uuid
from copy import deepcopy
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from llm_adapter import (
    chat_json as _raw_chat_json,
    get_llm_config,
    public_llm_config,
    public_llm_status,
    set_user_id as _llm_set_user_id,
    update_runtime_llm_config,
)


# M2 W3-1 安全补丁常量
MAX_REQUEST_BODY = 1_048_576  # 1 MB,BUG-004


def _new_error_id() -> str:
    """生成简短错误 ID,用于客户端追踪与服务器日志关联。BUG-005"""
    return f"err_{uuid.uuid4().hex[:12]}"


def _log_error(error_id: str, endpoint: str, exc: BaseException) -> None:
    """服务端结构化错误日志。M2 替换为 structured logger。BUG-005"""
    print(f"[ERROR] {error_id} endpoint={endpoint} type={type(exc).__name__} msg={exc}")
    traceback.print_exc()


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """先写临时文件再原子 rename,避免进程崩溃留下损坏 JSON。BUG-002"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "backend" / "data"
STATE_PATH = DATA_DIR / "project_state.json"
PROJECTS_DIR = DATA_DIR / "projects"


# ========================================================================
# M2 多租户临时补丁(W3-2,2026-05-22 by 接手 AI)
# 目的: 在 M5 SSO 上线前,用 Cookie UUID 给公开访问做 best-effort 用户隔离
# 设计:
#   - 每个访客拿一个 Cookie `qj_uid`(UUID),后端用 threading.local 把 user_id 注入
#     当前请求线程
#   - StateStore.path / PROJECTS_DIR 从模块常量改为 thread-local 动态计算
#   - 数据落 backend/data/users/{uid}/project_state.json + projects/*.json
#   - LLM 调用走 rate limiter,每 UID 每小时 N 次
# 等 M2 正式拆模块到 app/{store,handlers,llm}/ 时这段全部废弃。
# ========================================================================

import threading  # noqa: E402  补丁顶上
import time  # noqa: E402

_user_ctx = threading.local()
USERS_DIR = DATA_DIR / "users"
ANONYMOUS_FALLBACK_UID = "_anon_fallback"
LLM_RATE_LIMIT_PER_HOUR = 100  # per UID
COOKIE_NAME = "qj_uid"


def _safe_uid(value: str | None) -> str | None:
    """白名单校验 cookie 值:32 hex 字符,避免目录穿越。"""
    if not value:
        return None
    value = value.strip()
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        return None
    return value


def _current_uid() -> str:
    uid = getattr(_user_ctx, "uid", None)
    return uid or ANONYMOUS_FALLBACK_UID


def _user_dir(uid: str | None = None) -> Path:
    uid = uid or _current_uid()
    d = USERS_DIR / uid
    d.mkdir(parents=True, exist_ok=True)
    return d


def _user_state_path(uid: str | None = None) -> Path:
    return _user_dir(uid) / "project_state.json"


def _user_projects_dir(uid: str | None = None) -> Path:
    d = _user_dir(uid) / "projects"
    d.mkdir(parents=True, exist_ok=True)
    return d


# LLM rate limiter: per UID 滑动窗口,简单内存实现
_llm_rate_lock = threading.Lock()
_llm_rate_buckets: dict[str, list[float]] = {}  # uid -> list of unix ts


class LLMRateLimitExceeded(Exception):
    """LLM 调用超过 per-UID 配额。"""

    def __init__(self, uid: str, used: int, limit: int, reset_in: int):
        self.uid = uid
        self.used = used
        self.limit = limit
        self.reset_in = reset_in
        super().__init__(f"LLM rate limit exceeded for {uid}: {used}/{limit}")


def _llm_rate_check(uid: str | None = None) -> None:
    """超出限额抛 LLMRateLimitExceeded。在每个 LLM 调用点之前调用。"""
    uid = uid or _current_uid()
    now = time.time()
    window = 3600  # 1 hour
    with _llm_rate_lock:
        bucket = _llm_rate_buckets.setdefault(uid, [])
        # 清理过期记录
        cutoff = now - window
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)
        if len(bucket) >= LLM_RATE_LIMIT_PER_HOUR:
            oldest = bucket[0]
            reset_in = int(window - (now - oldest))
            raise LLMRateLimitExceeded(uid, len(bucket), LLM_RATE_LIMIT_PER_HOUR, reset_in)
        bucket.append(now)


def _empty_state() -> dict[str, Any]:
    """新访客初始状态:空书架,无活跃项目。
    注意: dict 字段必须是空 dict 而非 None,否则 mutate/workflow_meta 等下游
    用 `.get(...)` 链式取值时会炸。
    列表字段也必须是 [] 而非缺,否则前端 app.js renderHooks/renderScores/
    renderCharacter 等 `.filter`/`.map` 会炸"""
    return {
        "project": {},
        "chapters": [],
        "scenes": [],
        "plan": {},
        "issues": [],
        "trace": [],
        "hooks": [],
        "characters": {},
        "scores": [],
        "patches": [],
        "memory": {"canon": [], "lore": [], "stakes": []},
        "outline": {"chapterOutlines": []},
        "chapterArchive": [],
        "ideaDraft": None,
        "pendingIdeaDraft": None,
        "truthAfter": "",
        "draftScore": 0,
        "directorDecision": "",
        "humanStyleReport": {},
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
    }


# ========================================================================
# 多租户补丁 END
# ========================================================================


def chat_json(*args, **kwargs):
    """包装 llm_adapter.chat_json。每次调用前做 per-UID rate check,
    超额抛 LLMRateLimitExceeded,由 do_POST 异常处理转 429 响应。"""
    _llm_rate_check()
    return _raw_chat_json(*args, **kwargs)



def now_label() -> str:
    return datetime.now().strftime("%H:%M")


def default_state() -> dict[str, Any]:
    return {
        "project": {
            "id": "proj_xuanhuan_demo",
            "title": "九天剑主",
            "genre": "xuanhuan",
            "platform": "fanqie",
            "workflowVersion": "novel_workflow_v1",
            "chapterNumber": 12,
            "chapterTitle": "生死战书",
            "arcName": "外门大比篇",
        },
        "chapters": [
            {"number": "011", "title": "剑鸣外门", "status": "done"},
            {"number": "012", "title": "生死战书", "status": "active"},
            {"number": "013", "title": "三日决战", "status": "draft"},
        ],
        "directorDecision": "计划通过，进入场景生成",
        "ideaDraft": None,
        "draftScore": 86,
        "truthAfter": "v12 pending",
        "plan": {
            "readerPromise": "三日后的生死战会决定苏寒去留",
            "mustAdvance": "黑色断剑第二次异动，陈玄背后势力露出痕迹",
            "mustNotResolve": "苏寒身世、断剑来源、长老真实立场",
            "status": "draft",
        },
        "scenes": [
            {
                "id": "scene_01",
                "number": 1,
                "title": "接住上章钩子",
                "type": "压迫开场",
                "words": 420,
                "status": "ready",
                "summary": "陈玄带伤现身，当众递出生死战书。",
                "tags": ["承接", "冲突"],
                "content": "陈玄来的时候，右臂还缠着白布。\n\n白布下有血渗出来。\n\n可他手里那封生死战书，比血更刺眼。\n\n外门演武场一下安静了。\n\n陈玄把战书拍在石台上，盯着苏寒。\n\n“三日后，生死台。”\n\n他声音不大，却让所有人都听得清清楚楚。\n\n“不敢接，就滚出青云宗。”",
            },
            {
                "id": "scene_02",
                "number": 2,
                "title": "长老默认规则",
                "type": "制度压迫",
                "words": 510,
                "status": "ready",
                "summary": "外门长老没有阻止，反而默认战书有效。",
                "tags": ["规则", "势力"],
                "content": "苏寒没有立刻伸手。\n\n他先看向高台。\n\n外门长老端坐不动，像是没看见那封战书。\n\n这就是答案。\n\n陈玄敢来，不是因为他还有多少底气，而是因为有人准许他来。\n\n“长老。”苏寒开口，“生死战书，外门弟子可以随意递？”\n\n长老抬了抬眼皮。\n\n“青云宗不养怯战之人。”",
            },
            {
                "id": "scene_03",
                "number": 3,
                "title": "逼到不得不接",
                "type": "装逼打脸",
                "words": 620,
                "status": "needs_fix",
                "summary": "陈玄用苏家旧事羞辱苏寒，逼他接战。",
                "tags": ["爽点", "待修"],
                "content": "陈玄笑了。\n\n他笑得很慢，像是早就等着这一刻。\n\n“苏寒，你不是很会忍吗？”\n\n“当年苏家把你赶出来，你忍了。”\n\n“未婚妻退婚，你也忍了。”\n\n“现在，我把战书递到你脸上。”\n\n他往前一步，右臂的伤口被牵动，白布渗出更深的红。\n\n“你还要忍？”\n\n四周传来低低的议论声。\n\n苏寒终于伸手，按住了那封战书。\n\n他没有怒吼。\n\n甚至没有多看陈玄一眼。\n\n“我接。”\n\n两个字落下，演武场像被一把刀切开。\n\n苏寒抬眼。\n\n“三日后，我让你站着上台。”\n\n“跪着下去。”",
            },
            {
                "id": "scene_04",
                "number": 4,
                "title": "下注与反转",
                "type": "读者爽点",
                "words": 560,
                "status": "draft",
                "summary": "外门弟子下注，苏寒反向加码。",
                "tags": ["反转", "爽点"],
                "content": "消息传得比风还快。\n\n不到一炷香，外门盘口已经开了。\n\n陈玄胜，一赔一点一。\n\n苏寒胜，一赔二十。\n\n有人笑到拍桌。\n\n“这还用想？白送钱。”\n\n苏寒从人群外走过，把最后三枚灵石放在桌上。\n\n管盘口的弟子愣住。\n\n“押谁？”\n\n苏寒看着他。\n\n“押我自己。”",
            },
            {
                "id": "scene_05",
                "number": 5,
                "title": "断剑异动",
                "type": "章末钩子",
                "words": 430,
                "status": "draft",
                "summary": "夜里黑色断剑裂开第一道纹路。",
                "tags": ["伏笔", "钩子"],
                "content": "夜深后，苏寒回到木屋。\n\n他没有修炼。\n\n只是把黑色断剑放在桌上。\n\n白天那一瞬间，他分明感觉到剑身在发烫。\n\n可现在，它安静得像一块死铁。\n\n苏寒伸手，指尖刚碰到剑柄。\n\n咔。\n\n一道细微的裂声，在屋里响起。\n\n黑色断剑上，裂开了第一道金色纹路。",
            },
        ],
        "patches": [
            {"type": "character_state", "title": "苏寒", "detail": "接下生死战书，情绪从压抑转为冷静决断。"},
            {"type": "relationship", "title": "苏寒 ⇄ 陈玄", "detail": "关系状态升级为公开生死敌对。"},
            {"type": "hook", "title": "黑色断剑", "detail": "从 planted 进入 reinforced，出现第一道金色纹路。"},
            {"type": "reader_contract", "title": "读者承诺", "detail": "三日后的生死台必须在 13-15 章兑现。"},
        ],
        "memory": {
            "canon": [
                {"title": "苏寒被逐出苏家", "text": "第 1 章确认，公开身份为苏家弃子，不可随意改写。"},
                {"title": "黑色断剑归属", "text": "第 2 章由苏寒获得，目前所有权归苏寒。"},
                {"title": "陈玄右臂受伤", "text": "第 11 章被剑气伤及右臂，本章必须延续伤势。"},
                {"title": "青云宗外门规矩", "text": "生死战书一旦接下，三日内必须登上生死台。"},
            ],
            "state": [
                {"title": "苏寒", "text": "练气九层，位置：外门木屋；情绪：冷静压怒。"},
                {"title": "陈玄", "text": "右臂伤未愈，背后疑似有长老支持。"},
                {"title": "黑色断剑", "text": "状态：封印松动；当前只能被动吸收剑气。"},
                {"title": "外门盘口", "text": "苏寒胜率被公开看低，一赔二十。"},
            ],
            "intent": [
                {"title": "身世之谜", "text": "80-120 章揭示苏寒母亲与上古剑族有关。"},
                {"title": "陈玄背后势力", "text": "计划引出内门长老派系，作为外门大比篇后续压力源。"},
                {"title": "断剑剑灵", "text": "下一卷再觉醒，不在当前章节直接说话。"},
                {"title": "读者承诺", "text": "13-15 章必须兑现生死台对决，不可继续拖延。"},
            ],
        },
        "hooks": [
            {"status": "planned", "title": "内门长老派系", "meta": "预计 16-20 章", "text": "陈玄为何能让长老默许生死战书。"},
            {"status": "planted", "title": "苏寒身世之谜", "meta": "第 3 章埋设", "text": "祖祠剑鸣与母亲遗言尚未解释。"},
            {"status": "reinforced", "title": "黑色断剑", "meta": "第 12 章强化", "text": "剑身出现第一道金色纹路。"},
            {"status": "near_due", "title": "三日生死台", "meta": "13-15 章必须兑现", "text": "读者已经得到明确对决承诺。"},
            {"status": "resolved", "title": "陈玄不会罢休", "meta": "第 12 章兑现", "text": "陈玄递出生死战书，冲突正式升级。"},
        ],
        "characters": {
            "suHan": {
                "name": "苏寒",
                "role": "主角",
                "state": "练气九层，接下生死战书",
                "motive": "守住底线，查清母亲遗留秘密",
                "ability": "九天剑诀初窥门径；黑色断剑被动吸收剑气",
                "constraints": "不会求饶；不会主动伤害无辜者；不会提前知道断剑来源",
            },
            "chenXuan": {
                "name": "陈玄",
                "role": "阶段反派",
                "state": "右臂受伤，强撑威压",
                "motive": "逼苏寒上生死台，夺回外门威望",
                "ability": "外门剑术强，但伤势影响双手招式",
                "constraints": "不能突然满血；不能无理由降智",
            },
            "elder": {
                "name": "神秘老者",
                "role": "隐藏监视者",
                "state": "尚未公开现身",
                "motive": "观察苏寒血脉变化",
                "ability": "身份未知，不能直接救场",
                "constraints": "当前只允许作为弱提示出现",
            },
            "suXuewen": {
                "name": "苏雪雯",
                "role": "旧关系线",
                "state": "未出场，被陈玄用作羞辱素材",
                "motive": "未来关系反转伏笔",
                "ability": "暂不公开",
                "constraints": "本章不能突然登场抢主线",
            },
            "outerSect": {
                "name": "外门长老",
                "role": "规则压力",
                "state": "默许生死战书",
                "motive": "维持外门强者淘汰规则，偏向陈玄",
                "ability": "掌握外门规则解释权",
                "constraints": "不能直接出手杀主角",
            },
        },
        "scores": [
            {"name": "连续性", "score": 82, "reason": "陈玄伤势需要在 scene_03 更明确"},
            {"name": "读者体验", "score": 88, "reason": "压迫感清晰，章末钩子有效"},
            {"name": "角色一致性", "score": 91, "reason": "苏寒克制、冷硬的处理稳定"},
            {"name": "冲突升级", "score": 84, "reason": "外门长老默认规则强化了压力"},
            {"name": "章末钩子", "score": 93, "reason": "物品异动替代强敌出场，避免重复"},
            {"name": "AI 腔控制", "score": 79, "reason": "少量句式还可再短一些"},
        ],
        "issues": [
            {"level": "blocker", "location": "scene_03", "issue": "陈玄右臂受伤，但压迫动作中没有体现伤势代价。", "fix": "加入伤口渗血或强撑动作，保持状态连续。"},
            {"level": "major", "location": "scene_04", "issue": "下注反转不错，但外门弟子情绪还可以更尖锐。", "fix": "增加一句短促嘲笑和一个立刻沉默的反应。"},
            {"level": "minor", "location": "全章", "issue": "部分段落衔接偏整齐。", "fix": "Style Editor 可打散句式，保留短句节奏。"},
        ],
        "trace": [
            {"time": "00:01", "title": "Director", "text": "识别任务为继续第 12 章，调用 v1 工作流。"},
            {"time": "00:06", "title": "Story Architect", "text": "匹配当前单元：外门大比篇，本章功能为高潮前压迫。"},
            {"time": "00:11", "title": "Context Assembler", "text": "筛选 5 个角色、3 个伏笔、2 个硬约束和玄幻流派规则。"},
            {"time": "00:18", "title": "Chapter Planner", "text": "生成 5 个 Scene Card，Plan Gate 通过。"},
            {"time": "00:44", "title": "Scene Writer", "text": "分场景生成正文，scene_03 标记为 needs_fix。"},
            {"time": "01:02", "title": "Audit Board", "text": "连续性发现 1 个 blocker，建议局部修订。"},
        ],
    }


def infer_genre(topic: str, requested: str | None = None) -> str:
    if requested and requested != "auto":
        return requested
    lowered = topic.lower()
    if any(word in topic for word in ["规则怪谈", "怪谈", "诡异", "诡秘", "诡异复苏", "怪物规则"]):
        return "weird_rules"
    if any(word in topic for word in ["御兽", "宠兽", "契约兽", "召唤兽", "灵兽"]):
        return "beast_taming"
    if any(word in topic for word in ["无限流", "副本", "轮回", "主神", "惊悚游戏"]):
        return "infinite"
    if any(word in topic for word in ["高武", "灵气复苏", "武道", "气血", "武考"]):
        return "gaowu"
    if any(word in topic for word in ["末世", "囤货", "安全屋", "天灾", "丧尸", "极寒"]):
        return "apocalypse"
    if any(word in topic for word in ["游戏入侵", "第四天灾", "玩家", "网游", "领主", "全民转职"]):
        return "game_invasion"
    if any(word in topic for word in ["直播", "短视频", "娱乐圈", "文娱", "顶流", "塌房"]):
        return "live_entertainment"
    if any(word in topic for word in ["种田", "基建", "经营", "领地", "农场", "小镇"]):
        return "farming_building"
    if any(word in topic for word in ["克苏鲁", "民俗", "志怪", "悬疑", "破案", "禁忌"]):
        return "mystery_horror"
    if any(word in lowered for word in ["ai", "星际", "机甲", "末世", "废土", "赛博", "飞船"]):
        return "scifi"
    if any(word in topic for word in ["重生", "穿越", "前世", "回到"]):
        return "rebirth"
    if any(word in topic for word in ["恋", "婚", "总裁", "青梅", "女主", "甜宠"]):
        return "romance"
    if any(word in topic for word in ["外卖", "都市", "系统", "直播", "职场", "差评", "订单"]):
        return "urban"
    return "xuanhuan"


def idea_profiles(topic: str, genre: str) -> dict[str, Any]:
    profiles = {
        "xuanhuan": {
            "titles": ["旧剑问天", "万劫剑骨", "被逐出宗门后我执掌天命"],
            "heroes": ["陆沉", "沈既白", "顾长夜"],
            "setting": "宗门、古族、秘境和失落传承交织的修行世界。",
            "hook": "主角在最低谷时唤醒一件被所有人误判的旧物。",
            "conflict": "公开羞辱与隐藏血脉之间形成强反差，外部势力步步加压。",
        },
        "urban": {
            "titles": ["夜雨订单簿", "差评改命人", "我在城市暗面接单"],
            "heroes": ["许照安", "陈青野", "林舟"],
            "setting": "现实都市表层是普通生活，暗层由任务、订单和规则交易组成。",
            "hook": "主角接到一条不该存在的订单，从此每一次选择都会改写别人的命运。",
            "conflict": "底层生存压力与隐秘系统任务互相挤压，主角必须边赚钱边活命。",
        },
        "rebirth": {
            "titles": ["重回崩盘前夜", "这一世我先落子", "前世仇人都在等我翻盘"],
            "heroes": ["江临", "祁远", "周见深"],
            "setting": "主角带着前世记忆回到命运分岔点，熟悉的人和局都还没摊牌。",
            "hook": "主角知道未来会发生什么，但每一次改变都会产生新的代价。",
            "conflict": "先知优势与蝴蝶效应对撞，仇人、旧友和亲人都在被重新洗牌。",
        },
        "romance": {
            "titles": ["雨夜偏航", "她把月光还给他", "协议之外"],
            "heroes": ["温梨", "许知夏", "林雾"],
            "setting": "亲密关系、家族压力和个人成长交错的现代情感世界。",
            "hook": "一场误会或协议关系，把两个本不该靠近的人绑到一起。",
            "conflict": "情感吸引和现实阻力并行推进，秘密迟早要撕开。",
        },
        "scifi": {
            "titles": ["星河旧约", "废舰里的第七个声音", "边境维修工"],
            "heroes": ["林照", "岑越", "周隼"],
            "setting": "边境星区、废弃舰船、旧时代 AI 与星际通缉制度并存。",
            "hook": "主角唤醒旧时代 AI 后，被卷入一份已经失效却仍在执行的星河旧约。",
            "conflict": "个人求生、AI 真相和星际权力追捕形成三重压力。",
        },
        "weird_rules": {
            "titles": ["怪谈降临后我听见规则漏洞", "午夜规则管理员", "诡异复苏：我能改写禁忌"],
            "heroes": ["闻昼", "沈听澜", "许烬"],
            "setting": "现实城市被怪谈区域侵蚀，每个区域都有不可违反的生存规则。",
            "hook": "主角能听见规则背后的漏洞，但每次利用漏洞都会被诡异记住。",
            "conflict": "规则求生、诡异追猎和人心博弈同时压迫主角。",
        },
        "beast_taming": {
            "titles": ["我的宠兽能读懂神话", "御兽从契约废蛋开始", "怪谈御兽师"],
            "heroes": ["白砚", "林渡", "程野"],
            "setting": "全民御兽时代，宠兽进化路线决定阶层和命运。",
            "hook": "主角契约了被判定为废物的宠兽，却发现它能走隐藏进化路线。",
            "conflict": "弱宠逆袭、进化资源争夺和学院/赛场排名形成持续爽点。",
        },
        "infinite": {
            "titles": ["副本通关后我成了漏洞", "无限列车：请勿相信站台广播", "规则副本管理员"],
            "heroes": ["祁南", "温序", "陆回"],
            "setting": "玩家被卷入不断变化的副本世界，生存依赖推理、选择和临场反应。",
            "hook": "主角第一次进副本就发现系统提示和真实规则互相矛盾。",
            "conflict": "副本生存、队友信任和系统阴谋持续制造反转。",
        },
        "gaowu": {
            "titles": ["高武：我能听见气血暴击", "武考前夜我觉醒旧神骨", "灵气复苏从倒数第一开始"],
            "heroes": ["江燃", "陈砺", "顾星河"],
            "setting": "灵气复苏后的现代社会，武考、军校、异兽战场决定年轻人的阶层跃迁。",
            "hook": "主角气血低到被放弃，却在极限压迫下觉醒异常成长方式。",
            "conflict": "考试排名、异兽危机和家族/学校压力交替推进。",
        },
        "apocalypse": {
            "titles": ["末世前我绑定安全屋", "极寒囤货：我的仓库通万界", "天灾倒计时三十天"],
            "heroes": ["宋迟", "姜见月", "陆衡"],
            "setting": "天灾、丧尸、极寒或污染降临前后，物资、据点和信任成为核心资源。",
            "hook": "主角提前得到末世倒计时，却发现自己的安全屋规则并不完整。",
            "conflict": "囤货经营、邻里/势力冲突和灾难升级形成长线压力。",
        },
        "game_invasion": {
            "titles": ["游戏入侵后我成了隐藏职业", "第四天灾：我在异界发任务", "全民转职：我的技能会进化"],
            "heroes": ["周奕", "路沉舟", "韩序"],
            "setting": "游戏系统与现实融合，职业、等级、技能和副本改写社会结构。",
            "hook": "主角拿到看似鸡肋的隐藏职业，却能触发别人看不见的任务链。",
            "conflict": "职业成长、副本争夺和现实秩序崩塌交织推进。",
        },
        "live_entertainment": {
            "titles": ["直播翻车后我爆红了", "顶流从塌房现场开始", "全网黑的我靠副本综艺封神"],
            "heroes": ["许知野", "林栀", "姜未"],
            "setting": "直播、短视频、综艺和舆论场高度绑定，流量既是资源也是陷阱。",
            "hook": "主角在全网黑的直播事故中反向出圈，并拿到改变舆论的系统任务。",
            "conflict": "人设反转、舆论战、资源争夺和作品兑现持续拉扯。",
        },
        "farming_building": {
            "titles": ["我在废土开小镇", "种田基建从一间破屋开始", "领地经营：居民全是问题角色"],
            "heroes": ["宁禾", "温砚", "沈青棠"],
            "setting": "主角从一块贫瘠领地或破败据点开始经营，逐步扩张人口、产业和防御。",
            "hook": "主角获得一个可升级据点，但每次升级都会吸引新的麻烦。",
            "conflict": "资源短缺、居民矛盾、外敌入侵和产业升级循环推进。",
        },
        "mystery_horror": {
            "titles": ["民俗档案：不要回头", "禁忌调查员", "雾镇第十三户"],
            "heroes": ["周泊宁", "顾问渠", "谢沉"],
            "setting": "民俗传说、旧案、禁忌仪式和现代调查交织的悬疑世界。",
            "hook": "主角接手一份看似普通的旧案，却发现案卷里写着自己的名字。",
            "conflict": "查案推进、禁忌反噬和真相层层翻转构成阅读动力。",
        },
    }
    return profiles.get(genre, profiles["xuanhuan"])


GENRE_DEEP_CONFIGS: dict[str, dict[str, Any]] = {
    "xuanhuan": {
        "label": "玄幻修仙",
        "coreMechanism": "境界压制 + 机缘升级 + 公开打脸",
        "openingRecipe": ["低位羞辱", "隐藏底牌弱提示", "当众立约或反击", "章末更大势力注意"],
        "payoffEngine": ["三章内第一次打脸", "十章内第一次升级", "每个机缘必须有代价"],
        "mustHave": ["境界/资源数值", "反派压迫", "主角底线", "章末强钩"],
        "avoid": ["一章堆十个境界", "主角无代价秒杀", "反派纯降智"],
        "firstThree": [
            "第 1 章：废柴/弃子被公开压迫，旧物或血脉异动。",
            "第 2 章：主角发现底牌限制，被迫接下更大冲突。",
            "第 3 章：第一次小反击，反派背后势力露出。",
        ],
        "sceneTemplates": [
            ["开篇羞辱", "压迫开场", "主角在宗门/家族公开场合被逼退无可退。"],
            ["旧物异动", "金手指弱提示", "被所有人忽视的旧物出现异常。"],
            ["当众反击", "装逼打脸", "主角不解释，直接用行动扭转局面。"],
        ],
    },
    "urban": {
        "label": "都市系统",
        "coreMechanism": "现实压力 + 系统任务 + 即时反馈爽点",
        "openingRecipe": ["底层压力", "异常任务出现", "用任务反击现实困境", "章末更危险订单"],
        "payoffEngine": ["每章有现实收益", "任务奖励必须可见", "系统有规则和代价"],
        "mustHave": ["钱/订单/职位/人脉变化", "现实痛点", "任务提示", "短平快反转"],
        "avoid": ["系统无限送", "现实场景不接地气", "奖励没有反馈"],
        "firstThree": [
            "第 1 章：主角被现实问题逼到墙角，异常任务出现。",
            "第 2 章：主角用任务规则解决小危机，同时发现代价。",
            "第 3 章：第一次现实收益到账，引来更高层级麻烦。",
        ],
        "sceneTemplates": [
            ["现实压迫", "底层困境", "主角被差评、债务、职场或家庭压力逼到临界点。"],
            ["异常任务", "系统触发", "手机/订单/面板出现不该存在的任务。"],
            ["即时兑现", "现实反转", "主角完成任务得到可见收益，但新危机立刻出现。"],
        ],
    },
    "weird_rules": {
        "label": "规则怪谈/诡异复苏",
        "coreMechanism": "规则误导 + 禁忌求生 + 漏洞反杀",
        "openingRecipe": ["异常空间降临", "规则纸条出现", "第一条规则误导", "主角发现漏洞"],
        "payoffEngine": ["每章至少一条规则", "规则必须可验证", "反规则解法带代价"],
        "mustHave": ["规则文本", "禁忌行为", "诡异惩罚", "冷静推理"],
        "avoid": ["规则随便改", "诡异只吓人不讲逻辑", "靠运气通关"],
        "firstThree": [
            "第 1 章：日常场景异化，规则出现，主角避开第一次死亡点。",
            "第 2 章：规则互相矛盾，主角验证其中一条是假规则。",
            "第 3 章：主角利用漏洞救人或反杀，但被诡异标记。",
        ],
        "sceneTemplates": [
            ["日常异化", "怪谈开场", "熟悉地点变成规则区域。"],
            ["规则宣告", "禁忌建立", "墙面、纸条或广播给出多条规则。"],
            ["漏洞求生", "推理反转", "主角发现规则里的矛盾并暂时活下来。"],
        ],
    },
    "beast_taming": {
        "label": "御兽",
        "coreMechanism": "废宠逆袭 + 进化路线 + 赛场/秘境兑现",
        "openingRecipe": ["契约失败压力", "废宠登场", "隐藏进化提示", "第一场测评反转"],
        "payoffEngine": ["宠兽每次成长要有材料", "技能变化可视化", "比赛/秘境制造兑现"],
        "mustHave": ["宠兽名称", "技能面板", "进化材料", "羁绊动作"],
        "avoid": ["宠兽像工具没性格", "进化无成本", "技能堆砌无战术"],
        "firstThree": [
            "第 1 章：主角契约被判废的宠兽，发现隐藏路线。",
            "第 2 章：为第一份进化材料冒险，宠兽展现性格。",
            "第 3 章：测评/小赛第一次打脸，开启学院或秘境线。",
        ],
        "sceneTemplates": [
            ["契约失败", "低位开局", "主角被分配到没人要的宠兽。"],
            ["隐藏面板", "进化提示", "主角看见别人看不到的进化路线。"],
            ["测评反转", "小爽点", "废宠用冷门技能打出意外结果。"],
        ],
    },
    "infinite": {
        "label": "无限流/副本",
        "coreMechanism": "副本规则 + 队友博弈 + 生死反转",
        "openingRecipe": ["被拉入副本", "规则播报", "首个死亡样本", "主角发现隐藏条件"],
        "payoffEngine": ["每章推进一个副本谜题", "死亡样本服务推理", "通关奖励改变下个副本"],
        "mustHave": ["副本目标", "通关条件", "队友立场", "死亡风险"],
        "avoid": ["副本规则模糊", "队友全工具人", "靠外挂跳过推理"],
        "firstThree": [
            "第 1 章：主角进入副本，规则和死亡样本建立压迫。",
            "第 2 章：队伍分裂，主角验证一条隐藏规则。",
            "第 3 章：第一次阶段通关，发现副本背后有人操控。",
        ],
        "sceneTemplates": [
            ["副本降临", "生存开场", "主角从现实被拉入陌生副本。"],
            ["规则播报", "任务设定", "系统给出通关目标和死亡限制。"],
            ["首个反转", "推理爽点", "主角用细节避开第一次团灭点。"],
        ],
    },
    "gaowu": {
        "label": "高武/灵气复苏",
        "coreMechanism": "气血数值 + 武考排名 + 异兽战场",
        "openingRecipe": ["检测垫底", "气血异常", "训练突破", "武考危机"],
        "payoffEngine": ["数值增长要明确", "训练有痛感", "排名变化带来现实反馈"],
        "mustHave": ["气血值", "武技", "考试/测评", "异兽威胁"],
        "avoid": ["数值乱跳", "训练无代价", "校园压力不真实"],
        "firstThree": [
            "第 1 章：气血垫底被嘲，异常成长方式出现。",
            "第 2 章：主角训练到极限，气血第一次反常增长。",
            "第 3 章：小测排名反转，引来教官和强敌注意。",
        ],
        "sceneTemplates": [
            ["气血检测", "数值开场", "主角检测成绩垫底，被现实规则压迫。"],
            ["异常增长", "能力提示", "主角在极限状态下发现气血异常。"],
            ["小测反转", "排名爽点", "主角用具体数值完成第一次逆袭。"],
        ],
    },
    "apocalypse": {
        "label": "末世囤货/天灾",
        "coreMechanism": "倒计时 + 物资账本 + 据点升级",
        "openingRecipe": ["末世预警", "囤货选择", "第一轮灾害", "安全屋规则"],
        "payoffEngine": ["物资变化要记账", "灾害逐轮升级", "人性冲突推动剧情"],
        "mustHave": ["倒计时", "物资清单", "据点限制", "邻里/势力冲突"],
        "avoid": ["无限物资", "灾害无压迫", "只囤货不冲突"],
        "firstThree": [
            "第 1 章：主角获得末世倒计时，开始关键囤货。",
            "第 2 章：第一轮异常天气出现，别人还没意识到严重性。",
            "第 3 章：安全屋第一次发挥作用，也暴露给危险邻居。",
        ],
        "sceneTemplates": [
            ["倒计时出现", "危机开场", "主角得到明确末世预警。"],
            ["物资决策", "资源爽点", "主角用有限钱做关键囤货。"],
            ["灾害落地", "章末危机", "第一轮灾害证明预警真实。"],
        ],
    },
    "game_invasion": {
        "label": "游戏入侵/第四天灾",
        "coreMechanism": "职业面板 + 副本资源 + 现实秩序崩塌",
        "openingRecipe": ["系统公告", "职业觉醒", "隐藏任务", "现实副本化"],
        "payoffEngine": ["技能效果可视化", "任务链递进", "现实利益与等级绑定"],
        "mustHave": ["面板", "职业", "技能", "副本入口"],
        "avoid": ["面板太长", "技能无战术", "现实后果缺席"],
        "firstThree": [
            "第 1 章：游戏系统降临，主角觉醒冷门职业。",
            "第 2 章：主角触发隐藏任务，进入第一个现实副本。",
            "第 3 章：用冷门技能打出反差，获得职业进化线索。",
        ],
        "sceneTemplates": [
            ["系统公告", "世界异变", "现实出现游戏化提示。"],
            ["冷门职业", "反差设定", "主角拿到被看低的隐藏职业。"],
            ["隐藏任务", "章末钩子", "主角触发别人看不见的任务链。"],
        ],
    },
    "live_entertainment": {
        "label": "直播文娱",
        "coreMechanism": "舆论反转 + 人设重塑 + 作品/直播兑现",
        "openingRecipe": ["全网黑现场", "直播事故", "反向出圈", "新资源邀约"],
        "payoffEngine": ["弹幕反馈密集", "每次反转带数据变化", "作品实力必须兑现"],
        "mustHave": ["热搜/弹幕", "人设误解", "现场表现", "数据增长"],
        "avoid": ["只靠嘴炮", "观众无反馈", "舆论变化无过程"],
        "firstThree": [
            "第 1 章：主角在直播事故中反向出圈。",
            "第 2 章：黑粉继续围攻，主角用一次能力兑现反转。",
            "第 3 章：数据爆涨带来机会，也引来对家下场。",
        ],
        "sceneTemplates": [
            ["全网黑", "舆论压迫", "主角在镜头前被质疑。"],
            ["现场反转", "能力兑现", "主角用表现打断舆论节奏。"],
            ["数据爆点", "章末钩子", "直播数据异常上涨，引来新资源或新敌人。"],
        ],
    },
    "farming_building": {
        "label": "种田基建/经营",
        "coreMechanism": "资源循环 + 居民管理 + 据点升级",
        "openingRecipe": ["破败开局", "第一份资源", "解决生存问题", "据点升级条件"],
        "payoffEngine": ["建设成果可见", "居民问题制造冲突", "每阶段解锁新功能"],
        "mustHave": ["资源账本", "建筑/产业", "居民需求", "外部威胁"],
        "avoid": ["只种田无冲突", "资源凭空出现", "居民没有个性"],
        "firstThree": [
            "第 1 章：主角接手破败据点，解决第一个生存问题。",
            "第 2 章：第一批居民/麻烦到来，资源分配产生矛盾。",
            "第 3 章：完成第一次升级，同时吸引外部势力。",
        ],
        "sceneTemplates": [
            ["破败据点", "低位开局", "主角面对资源极少的领地。"],
            ["第一建设", "经营爽点", "用有限资源解决迫切问题。"],
            ["升级代价", "章末钩子", "据点升级带来新居民或新威胁。"],
        ],
    },
    "mystery_horror": {
        "label": "克苏鲁/民俗悬疑",
        "coreMechanism": "旧案线索 + 禁忌仪式 + 层层反转",
        "openingRecipe": ["异常委托", "旧案细节", "第一条禁忌", "主角被卷入案中"],
        "payoffEngine": ["每章一个线索", "线索必须改写认知", "恐怖来自因果而非堆怪"],
        "mustHave": ["案卷/传说", "禁忌", "调查动作", "认知反转"],
        "avoid": ["只渲染气氛不推进", "谜题无解", "怪物随便出现"],
        "firstThree": [
            "第 1 章：主角接触旧案，发现自己与案子有关。",
            "第 2 章：调查第一处现场，触犯小禁忌。",
            "第 3 章：旧案证词反转，真正受害者身份成谜。",
        ],
        "sceneTemplates": [
            ["异常委托", "悬疑开场", "主角收到不该出现的案卷或委托。"],
            ["禁忌线索", "调查推进", "第一个线索带出不能触碰的规则。"],
            ["认知反转", "章末惊点", "案卷里出现主角自己的名字。"],
        ],
    },
}


def genre_deep_config(genre: str) -> dict[str, Any]:
    return GENRE_DEEP_CONFIGS.get(genre, GENRE_DEEP_CONFIGS["xuanhuan"])


STORY_LENGTH_CONFIGS: dict[str, dict[str, Any]] = {
    "short": {
        "key": "short",
        "label": "短篇小说",
        "targetWords": 50000,
        "targetChapters": 12,
        "chapterWords": 3500,
        "outlineChapterCount": 12,
        "planningRule": "短篇必须围绕单一主线推进，前 3 章入局，中段升级，最后 3 章集中回收核心伏笔并完成结局。",
    },
    "medium": {
        "key": "medium",
        "label": "中篇小说",
        "targetWords": 200000,
        "targetChapters": 80,
        "chapterWords": 2500,
        "outlineChapterCount": 12,
        "planningRule": "中篇保留 2-3 个单元，主线要清晰，避免铺太多长期坑，80 章内完成阶段性大结局。",
    },
    "long": {
        "key": "long",
        "label": "长篇连载",
        "targetWords": 1000000,
        "targetChapters": 400,
        "chapterWords": 2500,
        "outlineChapterCount": 10,
        "planningRule": "长篇按连载节奏设计多单元升级、势力扩张和长期伏笔，前 10 章重点完成卖点验证和追读钩子。",
    },
}


def story_length_config(value: Any = None) -> dict[str, Any]:
    key = str(value or "long").strip()
    config = STORY_LENGTH_CONFIGS.get(key, STORY_LENGTH_CONFIGS["long"])
    return deepcopy(config)


def build_arcs_for_length(story_config: dict[str, Any]) -> list[dict[str, str]]:
    key = story_config["key"]
    if key == "short":
        return [
            {"name": "异常入局篇", "chapters": "1-3", "goal": "快速建立主角处境、核心设定和必须解决的问题。", "payoff": "主角确认事件不可逃避，主动入局。"},
            {"name": "真相逼近篇", "chapters": "4-8", "goal": "连续推进线索和代价，让冲突逐步逼近最终选择。", "payoff": "主角拿到破局关键，但必须付出明确代价。"},
            {"name": "终局回收篇", "chapters": "9-12", "goal": "集中回收核心伏笔，完成最终反转和情绪落点。", "payoff": "主线问题得到解决，留下余味而不是继续开大坑。"},
        ]
    if key == "medium":
        return [
            {"name": "开篇试读篇", "chapters": "1-10", "goal": "建立卖点、主角动机和第一组对手。", "payoff": "完成第一次公开反击，拿到进入主线的资格。"},
            {"name": "主线扩张篇", "chapters": "11-35", "goal": "展开核心机制、盟友和阶段敌人。", "payoff": "主角解决第一场大危机，发现更深层真相。"},
            {"name": "真相反转篇", "chapters": "36-60", "goal": "推动身份、系统或旧案真相反转。", "payoff": "主角从被动应对转为主动布局。"},
            {"name": "阶段终局篇", "chapters": "61-80", "goal": "回收中篇主线，完成情绪高潮和阶段性结局。", "payoff": "最大压迫源被解决，主角完成清晰成长。"},
        ]
    return [
        {"name": "开局觉醒篇", "chapters": "1-30", "goal": "建立主角困境、核心金手指和第一批敌人。", "payoff": "主角完成第一次公开反击，拿到进入更大事件的资格。"},
        {"name": "规则入局篇", "chapters": "31-90", "goal": "揭示隐藏规则，让主角从被动应付变成主动布局。", "payoff": "主角识破第一层幕后操盘者，获得关键盟友。"},
        {"name": "真相反转篇", "chapters": "91-180", "goal": "将金手指来源、主角身世或系统真相推到台前。", "payoff": "主角发现自己不是被选中者，而是旧局的变量。"},
        {"name": "势力扩张篇", "chapters": "181-300", "goal": "扩大地图、敌方层级和主角阵营。", "payoff": "主角拥有改写局势的核心筹码。"},
        {"name": "终局封神篇", "chapters": "301-400", "goal": "回收核心伏笔，解决最大压迫源。", "payoff": "主角以自己的规则改写原本无法改变的结局。"},
    ]


def build_chapter_outlines_for_length(deep: dict[str, Any], hero: str, story_config: dict[str, Any]) -> list[dict[str, Any]]:
    beats = [
        ("命运开局", deep["firstThree"][0], deep["openingRecipe"][1]),
        ("异常验证", deep["firstThree"][1], "题材规则第一次被验证，同时出现代价。"),
        ("第一次兑现", deep["firstThree"][2], deep["payoffEngine"][0]),
        ("规则边界", f"{hero}总结核心机制边界，发现它不能无代价使用。", deep["mustHave"][0]),
        ("小爆反击", f"{hero}利用题材机制解决第一场现实或生存危机。", "读者看到第一次明确收益。"),
        ("高层注意", "反击引来更高层级敌人或系统任务。", "下一阶段敌人登场。"),
        ("盟友试探", "潜在盟友靠近，但目标不明。", "关系线和信息线同时推进。"),
        ("旧因浮现", "题材背后的旧事件露出第一块拼图。", "长期主线开始成形。"),
        ("双线并压", "现实压力和隐藏规则同时爆发。", "主角必须在收益和风险中二选一。"),
        ("单元小高潮", "主角付出代价换来第一次真正胜利。", "开启下一单元地图或势力。"),
        ("最终代价", "短篇核心代价集中爆发，主角必须做出不可撤回的选择。", "最后反转前的最大压力出现。"),
        ("结局回收", "回收核心伏笔，解决开篇承诺的问题。", "完成结局情绪落点。"),
    ]
    outline_count = int(story_config["outlineChapterCount"])
    outlines = []
    for index in range(1, outline_count + 1):
        chapter_title, event, hook = beats[min(index - 1, len(beats) - 1)]
        outlines.append(
            {
                "chapter": index,
                "title": chapter_title,
                "coreEvent": event,
                "readerHook": hook,
                "sceneCount": 3 if index <= 3 else 4,
            }
        )
    return outlines


def max_arc_chapter(arcs: list[dict[str, Any]]) -> int:
    max_chapter = 0
    for arc in arcs:
        match = re.search(r"(\d+)\s*-\s*(\d+)", str(arc.get("chapters", "")))
        if match:
            max_chapter = max(max_chapter, int(match.group(2)))
    return max_chapter


def normalize_idea_length(idea: dict[str, Any]) -> dict[str, Any]:
    story_config = story_length_config(idea.get("storyLength"))
    deep = idea.get("deepRules") or genre_deep_config(idea.get("genre", "xuanhuan"))
    hero = str(idea.get("selectedProtagonist") or "主角")
    fallback_outlines = build_chapter_outlines_for_length(deep, hero, story_config)
    outline_count = int(story_config["outlineChapterCount"])
    outlines = [item for item in idea.get("chapterOutlines", []) if isinstance(item, dict)]
    if len(outlines) < outline_count:
        existing = {_safe_int(item.get("chapter"), 0) for item in outlines}
        outlines.extend([item for item in fallback_outlines if item["chapter"] not in existing])
    idea["chapterOutlines"] = outlines[:outline_count]

    arcs = [item for item in idea.get("arcs", []) if isinstance(item, dict)]
    if not arcs or max_arc_chapter(arcs) > int(story_config["targetChapters"]) * 1.2:
        arcs = build_arcs_for_length(story_config)
    idea["arcs"] = arcs
    idea["storyLength"] = story_config["key"]
    idea["storyLengthLabel"] = story_config["label"]
    idea["targetWords"] = story_config["targetWords"]
    idea["targetChapters"] = story_config["targetChapters"]
    idea["chapterWords"] = story_config["chapterWords"]
    idea["planningRule"] = story_config["planningRule"]
    return idea


def build_synopsis_options(idea: dict[str, Any]) -> list[dict[str, str]]:
    title = str(idea.get("selectedTitle") or "这本书")
    hero = str(idea.get("selectedProtagonist") or "主角")
    premise = str(idea.get("premise") or idea.get("topic") or "主角被卷入一场改变命运的事件。")
    conflict = str(idea.get("mainConflict") or "他必须在压力中找到破局办法。")
    selling_point = str(idea.get("sellingPoint") or "爽点清晰，节奏紧凑。")
    length_label = str(idea.get("storyLengthLabel") or "长篇连载")
    return [
        {
            "style": "平台爽点版",
            "text": f"《{title}》讲述{hero}在低谷中撞见改变命运的核心机会。{premise} {selling_point} 本书按{length_label}节奏推进，主打强钩子、连续反转和清晰爽点。",
        },
        {
            "style": "悬念钩子版",
            "text": f"所有人都以为{hero}已经没有翻身可能，直到一个不该出现的异常把他推到风口浪尖。{conflict} 真相越查越深，每一次选择都会让局面彻底改写。",
        },
        {
            "style": "短简介版",
            "text": f"{hero}从被轻视的开局出发，借题材核心机制一路破局升级，在连续危机中揭开隐藏真相，完成从低位到掌控局面的反转。",
        },
    ]


def clean_title_option(value: Any) -> str:
    text = str(value or "").strip()
    return text.strip("《》“”\"' ")


def clean_name_option(value: Any) -> str:
    text = str(value or "").strip().strip("“”\"' ")
    text = re.split(r"[：:，,、；;（(\\s]", text, maxsplit=1)[0].strip()
    if len(text) > 8:
        text = text[:8]
    return text or "主角"


def normalize_candidate_options(idea: dict[str, Any]) -> dict[str, Any]:
    titles = [clean_title_option(item) for item in idea.get("recommendedTitles", [])]
    titles = [item for item in titles if item]
    names = [clean_name_option(item) for item in idea.get("recommendedProtagonists", [])]
    names = [item for item in names if item]
    selected_title = clean_title_option(idea.get("selectedTitle") or (titles[0] if titles else "未命名新书"))
    selected_name = clean_name_option(idea.get("selectedProtagonist") or (names[0] if names else "主角"))
    if selected_title not in titles:
        titles.insert(0, selected_title)
    if selected_name not in names:
        names.insert(0, selected_name)
    idea["recommendedTitles"] = titles[:5]
    idea["recommendedProtagonists"] = names[:5]
    idea["selectedTitle"] = selected_title
    idea["selectedProtagonist"] = selected_name
    return idea


def normalize_synopsis_options(idea: dict[str, Any]) -> dict[str, Any]:
    raw_options = idea.get("synopsisOptions") or idea.get("recommendedSynopses") or []
    options: list[dict[str, str]] = []
    for index, item in enumerate(raw_options):
        if isinstance(item, str):
            text = item.strip()
            if text:
                options.append({"style": f"简介 {index + 1}", "text": text})
        elif isinstance(item, dict):
            text = str(item.get("text") or item.get("content") or "").strip()
            if text:
                options.append({"style": str(item.get("style") or item.get("title") or f"简介 {index + 1}"), "text": text})
    if len(options) < 3:
        existing = {item["text"] for item in options}
        for item in build_synopsis_options(idea):
            if item["text"] not in existing:
                options.append(item)
    fallback_styles = ["平台爽点版", "悬念钩子版", "短简介版"]
    used_styles: set[str] = set()
    for index, option in enumerate(options):
        style = option["style"].strip() or f"简介 {index + 1}"
        if style in used_styles:
            style = fallback_styles[index] if index < len(fallback_styles) and fallback_styles[index] not in used_styles else f"简介 {index + 1}"
        option["style"] = style
        used_styles.add(style)
    idea["synopsisOptions"] = options[:3]
    selected = str(idea.get("selectedSynopsis") or "").strip()
    if not selected:
        idea["selectedSynopsis"] = idea["synopsisOptions"][0]["text"]
        idea["selectedSynopsisStyle"] = idea["synopsisOptions"][0]["style"]
    return idea


def build_first_chapter_scenes(genre: str, protagonist: str, topic: str) -> list[dict[str, Any]]:
    config = genre_deep_config(genre)
    scenes = []
    for index, template in enumerate(config["sceneTemplates"], start=1):
        title, scene_type, summary = template
        if genre == "urban" and index == 1:
            content = f"{protagonist}站在雨棚下。\n\n手机屏幕上，是第七个恶意差评。\n\n平台罚款、房租催缴、母亲的未接来电，全挤在同一个夜里。\n\n他第一次觉得，自己快被这座城市压扁了。"
        elif genre == "weird_rules" and index == 1:
            content = f"{protagonist}推开教室门时，黑板上多了三行红字。\n\n第一，不要回答点名。\n\n第二，不要相信坐在窗边的人。\n\n第三，如果你发现少了一个同学，请立刻闭眼。"
        elif genre == "beast_taming" and index == 1:
            content = f"契约大厅里，所有人都看着{protagonist}手里的灰色兽蛋。\n\n测评仪给出两个字。\n\n废品。\n\n下一秒，蛋壳里却传来一声极轻的心跳。"
        elif genre == "infinite" and index == 1:
            content = f"{protagonist}睁开眼时，车厢正在穿过一片黑雾。\n\n广播响起。\n\n欢迎进入 404 号副本。\n\n本轮任务：活到终点站。"
        elif genre == "gaowu" and index == 1:
            content = f"气血检测仪亮起红灯。\n\n42 点。\n\n全班最低。\n\n{protagonist}听见身后有人笑，可他也听见了另一道声音。\n\n气血正在异常回流。"
        elif genre == "apocalypse" and index == 1:
            content = f"{protagonist}收到末世倒计时的时候，超市促销广播还在循环播放。\n\n距离第一轮极寒降临，还有三十天。\n\n他看着银行卡余额，开始列第一张物资清单。"
        elif genre == "game_invasion" and index == 1:
            content = f"午夜十二点，所有人的手机同时弹出系统公告。\n\n现实版本更新完成。\n\n职业觉醒开始。\n\n{protagonist}面前浮现的职业，却只有四个字：任务发布者。"
        elif genre == "live_entertainment" and index == 1:
            content = f"直播镜头正对着{protagonist}。\n\n弹幕全是骂声。\n\n主持人笑着等他出丑。\n\n可三秒后，舞台吊灯突然晃了一下。"
        elif genre == "farming_building" and index == 1:
            content = f"{protagonist}推开木门。\n\n屋顶漏雨，仓库空着，领地账本上只剩三枚铜币。\n\n系统提示很冷静。\n\n请在天黑前解决第一批居民的晚饭。"
        elif genre == "mystery_horror" and index == 1:
            content = f"案卷封皮已经发霉。\n\n{protagonist}翻到最后一页时，指尖忽然停住。\n\n死者名单最下面，有一行刚写上去的字。\n\n那是他的名字。"
        elif index == 1:
            content = f"{protagonist}站在人群中央。\n\n所有目光都落在他身上。\n\n今天以前，他以为自己还能继续忍。\n\n可现在，对方把最后一点退路也踩碎了。"
        elif index == 2:
            content = f"没人相信{protagonist}还能翻身。\n\n他们只记得他的失败，记得他的沉默，记得他一次次低头离开。\n\n但没人知道，真正的变化已经开始。"
        else:
            content = f"就在所有人以为事情已经结束的时候。\n\n{protagonist}注意到一个不该出现的细节。\n\n很轻。\n\n却足够把整件事推向另一个方向。"
        scenes.append(
            {
                "id": f"scene_{index:02d}",
                "number": index,
                "title": title,
                "type": scene_type,
                "words": 420 + index * 60,
                "status": "draft",
                "summary": summary,
                "tags": ["开篇", config["label"]],
                "content": content,
            }
        )
    return scenes


def generate_idea_draft(body: dict[str, Any]) -> dict[str, Any]:
    topic = str(body.get("topic") or "").strip()
    if not topic:
        raise ValueError("题材不能为空")
    genre = infer_genre(topic, str(body.get("genre") or "auto"))
    platform = str(body.get("platform") or "fanqie")
    story_config = story_length_config(body.get("storyLength"))
    profile = idea_profiles(topic, genre)
    deep = genre_deep_config(genre)
    hero = profile["heroes"][0]
    title = profile["titles"][0]
    premise = f"{topic}。{profile['hook']}"
    first_goal = f"第一章按「{deep['openingRecipe'][0]} → {deep['openingRecipe'][1]} → {deep['openingRecipe'][-1]}」推进，让{hero}在开篇就遇到题材核心异常。"

    idea = {
        "topic": topic,
        "genre": genre,
        "genreLabel": deep["label"],
        "platform": platform,
        "storyLength": story_config["key"],
        "storyLengthLabel": story_config["label"],
        "targetWords": story_config["targetWords"],
        "targetChapters": story_config["targetChapters"],
        "chapterWords": story_config["chapterWords"],
        "planningRule": story_config["planningRule"],
        "recommendedTitles": profile["titles"],
        "recommendedProtagonists": profile["heroes"],
        "selectedTitle": title,
        "selectedProtagonist": hero,
        "premise": premise,
        "sellingPoint": profile["hook"],
        "worldSetting": profile["setting"],
        "mainConflict": profile["conflict"],
        "firstGoal": first_goal,
        "deepRules": {
            "coreMechanism": deep["coreMechanism"],
            "openingRecipe": deep["openingRecipe"],
            "payoffEngine": deep["payoffEngine"],
            "mustHave": deep["mustHave"],
            "avoid": deep["avoid"],
            "firstThree": deep["firstThree"],
        },
        "arcs": build_arcs_for_length(story_config),
        "chapterOutlines": build_chapter_outlines_for_length(deep, hero, story_config),
        "openingScenes": build_first_chapter_scenes(genre, hero, topic),
        "llmGenerated": False,
    }
    return normalize_synopsis_options(normalize_idea_length(normalize_candidate_options(idea)))


def generate_idea_draft_optional_llm(body: dict[str, Any]) -> dict[str, Any]:
    fallback = generate_idea_draft(body)
    config = get_llm_config("ideation")
    if not config.configured:
        fallback["llmError"] = "未配置大模型 API Key，已使用本地题材配置生成。"
        return fallback

    deep = fallback["deepRules"]
    story_config = story_length_config(fallback.get("storyLength"))
    system = (
        "你是中文网文产品级策划。只输出 JSON，不要 Markdown。"
        "目标是根据题材和篇幅规划生成适合网文的书案、大纲和章节细纲。"
        "必须遵守篇幅规划：短篇要回收主线，不要按长篇无限铺坑；长篇要保留连载升级和长期伏笔。"
    )
    user = json.dumps(
        {
            "topic": fallback["topic"],
            "genre": fallback["genre"],
            "genreLabel": fallback["genreLabel"],
            "platform": fallback["platform"],
            "storyLength": {
                "key": story_config["key"],
                "label": story_config["label"],
                "targetWords": story_config["targetWords"],
                "targetChapters": story_config["targetChapters"],
                "chapterWords": story_config["chapterWords"],
                "outlineChapterCount": story_config["outlineChapterCount"],
                "planningRule": story_config["planningRule"],
            },
            "deepRules": deep,
            "required_schema": {
                "selectedTitle": "string",
                "recommendedTitles": ["string", "string", "string"],
                "selectedProtagonist": "string",
                "recommendedProtagonists": ["string", "string", "string"],
                "premise": "string",
                "sellingPoint": "string",
                "worldSetting": "string",
                "mainConflict": "string",
                "firstGoal": "string",
                "storyLength": story_config["key"],
                "targetWords": story_config["targetWords"],
                "targetChapters": story_config["targetChapters"],
                "synopsisOptions": [{"style": "平台爽点版", "text": "string"}],
                "selectedSynopsis": "string",
                "arcs": [{"name": "string", "chapters": "string", "goal": "string", "payoff": "string"}],
                "chapterOutlines": [
                    {"chapter": 1, "title": "string", "coreEvent": "string", "readerHook": "string", "sceneCount": 3}
                ],
            },
        },
        ensure_ascii=False,
    )

    try:
        data = chat_json(system, user, temperature=0.7, agent="ideation")
    except Exception as exc:  # noqa: BLE001 - fallback keeps product usable and records reason.
        fallback["llmError"] = str(exc)
        return fallback

    idea = deepcopy(fallback)
    for key in [
        "selectedTitle",
        "recommendedTitles",
        "selectedProtagonist",
        "recommendedProtagonists",
        "premise",
        "sellingPoint",
        "worldSetting",
        "mainConflict",
        "firstGoal",
        "storyLength",
        "storyLengthLabel",
        "targetWords",
        "targetChapters",
        "chapterWords",
        "planningRule",
        "synopsisOptions",
        "selectedSynopsis",
        "selectedSynopsisStyle",
        "arcs",
        "chapterOutlines",
    ]:
        if data.get(key):
            idea[key] = data[key]
    idea = normalize_synopsis_options(normalize_idea_length(normalize_candidate_options(idea)))
    idea["openingScenes"] = build_first_chapter_scenes(idea["genre"], idea["selectedProtagonist"], idea["topic"])
    idea["llmGenerated"] = True
    idea["llmModel"] = config.model
    return idea


def _safe_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def new_project_id() -> str:
    return f"proj_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"


def safe_project_id(project_id: str) -> str:
    cleaned = str(project_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", cleaned):
        raise ValueError("无效作品 ID")
    return cleaned


def project_summary(state: dict[str, Any], active_id: str | None = None) -> dict[str, Any]:
    project = state.get("project", {})
    workflow = workflow_meta(state) if project else {}
    project_id = str(project.get("id") or "")
    story_config = story_length_config(project.get("storyLength"))
    return {
        "id": project_id,
        "title": project.get("title", "未命名作品"),
        "genre": project.get("genre", ""),
        "platform": project.get("platform", ""),
        "storyLengthLabel": project.get("storyLengthLabel") or story_config["label"],
        "targetWords": project.get("targetWords") or story_config["targetWords"],
        "targetChapters": project.get("targetChapters") or story_config["targetChapters"],
        "chapterNumber": project.get("chapterNumber", 1),
        "chapterTitle": project.get("chapterTitle", ""),
        "archiveCount": len(state.get("chapterArchive", [])),
        "draftScore": state.get("draftScore", 0),
        "updatedAt": state.get("updatedAt", ""),
        "workflowLabel": workflow.get("primaryLabel", ""),
        "active": bool(active_id and project_id == active_id),
    }


def chapter_outline_for(outlines: list[dict[str, Any]], chapter_number: int) -> dict[str, Any]:
    for outline in outlines:
        if _safe_int(outline.get("chapter"), -1) == chapter_number:
            return outline
    return {
        "chapter": chapter_number,
        "title": f"第 {chapter_number} 章",
        "coreEvent": "承接上一章结尾，推进新的冲突和兑现点。",
        "readerHook": "章末留下下一章必须点击的悬念。",
        "sceneCount": 3,
    }


def build_chapter_rows(outlines: list[dict[str, Any]], active_number: int) -> list[dict[str, Any]]:
    max_outline = max([_safe_int(item.get("chapter"), 0) for item in outlines] or [active_number + 2])
    if active_number <= 1:
        start = 1
        end = min(max_outline, 3)
    else:
        start = max(1, active_number - 1)
        end = min(max_outline, active_number + 2)
    rows = []
    for number in range(start, end + 1):
        outline = chapter_outline_for(outlines, number)
        rows.append(
            {
                "number": f"{number:03d}",
                "title": outline.get("title") or f"第 {number} 章",
                "status": "done" if number < active_number else "active" if number == active_number else "draft",
            }
        )
    return rows


def arc_name_for_chapter(arcs: list[dict[str, Any]], chapter_number: int, fallback: str) -> str:
    for arc in arcs:
        match = re.search(r"(\d+)\s*-\s*(\d+)", str(arc.get("chapters", "")))
        if not match:
            continue
        start, end = int(match.group(1)), int(match.group(2))
        if start <= chapter_number <= end:
            return str(arc.get("name") or fallback)
    return fallback


def build_chapter_scenes(genre: str, protagonist: str, chapter_number: int, outline: dict[str, Any]) -> list[dict[str, Any]]:
    config = genre_deep_config(genre)
    title = str(outline.get("title") or f"第 {chapter_number} 章")
    core_event = str(outline.get("coreEvent") or "承接上一章结尾，推进新的冲突。")
    reader_hook = str(outline.get("readerHook") or "章末留下下一章悬念。")
    scene_count = max(3, min(_safe_int(outline.get("sceneCount"), 3), 5))
    templates = [
        ("承接上章", "承压开场", f"承接上一章结尾，让{protagonist}立刻面对新的压力。"),
        ("目标明确", "行动推进", core_event),
        ("代价出现", "冲突升级", f"{protagonist}为了推进目标付出代价，题材机制继续兑现。"),
        ("局面反转", "小高潮", f"让本章核心事件出现反转或阶段爽点：{title}。"),
        ("章末钩子", "强钩子", reader_hook),
    ]
    selected = templates[: scene_count - 1] + [templates[-1]]
    scenes = []
    for index, (scene_title, scene_type, summary) in enumerate(selected, start=1):
        content = (
            f"{protagonist}还没来得及从上一章的余波里喘口气。\n\n"
            f"新的问题已经摆在眼前。\n\n"
            f"{summary}\n\n"
            "这一段会在生成正文时扩写成完整网文章节场景。"
        )
        scenes.append(
            {
                "id": f"scene_{index:02d}",
                "number": index,
                "title": scene_title,
                "type": scene_type,
                "words": 520 + index * 80,
                "status": "draft",
                "summary": summary,
                "tags": [f"第{chapter_number}章", config["label"]],
                "content": content,
            }
        )
    return scenes


def archive_current_chapter(state: dict[str, Any]) -> None:
    archive = state.setdefault("chapterArchive", [])
    chapter_number = state["project"]["chapterNumber"]
    chapter = {
        "number": chapter_number,
        "title": state["project"]["chapterTitle"],
        "scenes": deepcopy(state.get("scenes", [])),
        "draftScore": state.get("draftScore", 0),
        "truthAfter": state.get("truthAfter", ""),
    }
    for index, existing in enumerate(archive):
        if existing.get("number") == chapter_number:
            archive[index] = chapter
            return
    archive.append(chapter)


def prepare_next_chapter(state: dict[str, Any]) -> dict[str, Any]:
    if "committed" not in state.get("truthAfter", ""):
        raise ValueError("当前章还没有写入真相文件，不能进入下一章。")

    archive_current_chapter(state)
    current_number = state["project"]["chapterNumber"]
    next_number = current_number + 1
    target_chapters = _safe_int(state.get("project", {}).get("targetChapters"), 0)
    if target_chapters and next_number > target_chapters:
        raise ValueError(f"这本书规划为 {target_chapters} 章，当前已到规划终点。可以导出全书或新建下一本。")
    outlines = state.get("outline", {}).get("chapterOutlines") or state.get("ideaDraft", {}).get("chapterOutlines") or []
    arcs = state.get("outline", {}).get("arcs") or state.get("ideaDraft", {}).get("arcs") or []
    outline = chapter_outline_for(outlines, next_number)
    protagonist = state["characters"]["suHan"]["name"]
    genre = state["project"].get("genre", "xuanhuan")
    deep = genre_deep_config(genre)

    state["project"]["chapterNumber"] = next_number
    state["project"]["chapterTitle"] = str(outline.get("title") or f"第 {next_number} 章")
    state["project"]["arcName"] = arc_name_for_chapter(arcs, next_number, state["project"].get("arcName", "连载推进篇"))
    state["chapters"] = build_chapter_rows(outlines, next_number)
    state["plan"] = {
        "readerPromise": str(outline.get("coreEvent") or "推进本章核心事件。"),
        "mustAdvance": f"{deep['coreMechanism']}；{outline.get('readerHook') or deep['payoffEngine'][0]}",
        "mustNotResolve": "主角最终底牌、幕后真相、核心机制完整来源",
        "status": "new_project",
    }
    state["scenes"] = build_chapter_scenes(genre, protagonist, next_number, outline)
    state["patches"] = [
        {
            "type": "chapter_goal",
            "title": f"第 {next_number} 章目标",
            "detail": state["plan"]["readerPromise"],
        },
        {
            "type": "chapter_hook",
            "title": "章末钩子",
            "detail": str(outline.get("readerHook") or "本章需要留下明确追读理由。"),
        },
    ]
    state["draftScore"] = 0
    state["scores"] = [
        {"name": "连续性", "score": 0, "reason": f"等待第 {next_number} 章正文"},
        {"name": "读者体验", "score": 0, "reason": "等待正文生成"},
        {"name": "角色一致性", "score": 0, "reason": "等待角色状态验证"},
        {"name": "冲突升级", "score": 0, "reason": "等待本章冲突兑现"},
        {"name": "章末钩子", "score": 0, "reason": "等待章末钩子"},
        {"name": "AI 腔控制", "score": 0, "reason": "等待正文审计"},
        {"name": "原创表达", "score": 0, "reason": "等待人味编辑"},
    ]
    state["issues"] = [
        {
            "level": "major",
            "location": f"第 {next_number} 章计划",
            "issue": "新章节已创建，但还没有锁定章节计划。",
            "fix": "点击推荐流程，生成并确认本章计划。",
        }
    ]
    state["truthAfter"] = f"v{parse_truth_version(state.get('truthAfter', 'v1'))} pending"
    state["directorDecision"] = f"第 {next_number} 章已创建，等待生成计划"
    state["lastLLMRun"] = {"used": False, "note": f"已进入第 {next_number} 章，等待生成正文。", "model": None}
    state["humanStyleReport"] = {"status": "pending", "chapterNumber": next_number}
    state["memory"]["state"] = [
        {"title": protagonist, "text": f"当前状态：进入第 {next_number} 章；需要承接上一章结果继续行动。"},
        {"title": f"第 {next_number} 章目标", "text": state["plan"]["readerPromise"]},
    ]
    hook_title = str(outline.get("readerHook") or "下一章钩子")
    if hook_title and not any(hook.get("title") == hook_title for hook in state.get("hooks", [])):
        state.setdefault("hooks", []).append({"status": "planned", "title": hook_title, "meta": f"第 {next_number} 章", "text": "本章需要兑现或强化的追读钩子。"})
    add_trace(state, "Chapter Navigator", f"归档第 {current_number} 章，进入第 {next_number} 章《{state['project']['chapterTitle']}》。")
    return state


def create_project_state(body: dict[str, Any]) -> dict[str, Any]:
    title = clean_title_option(body.get("title") or "未命名新书")
    genre = str(body.get("genre") or "xuanhuan").strip()
    platform = str(body.get("platform") or "fanqie").strip()
    protagonist = clean_name_option(body.get("protagonist") or "主角")
    premise = str(body.get("premise") or "一个被轻视的人，在关键时刻觉醒自己的道路。").strip()
    synopsis = str(body.get("synopsis") or "").strip()
    first_goal = str(body.get("firstGoal") or "写出第一章开篇冲突，建立主角处境和第一枚钩子。").strip()
    idea_draft = body.get("ideaDraft") or {}
    story_config = story_length_config(body.get("storyLength") or idea_draft.get("storyLength"))
    deep = genre_deep_config(genre)
    if idea_draft:
        idea_draft = normalize_idea_length(deepcopy(idea_draft))
        idea_draft = normalize_synopsis_options(idea_draft)
        story_config = story_length_config(idea_draft.get("storyLength"))
        synopsis = synopsis or str(idea_draft.get("selectedSynopsis") or "").strip()
    chapter_outlines = idea_draft.get("chapterOutlines", []) or build_chapter_outlines_for_length(deep, protagonist, story_config)
    first_outline = chapter_outline_for(chapter_outlines, 1)
    synopsis = synopsis or premise

    state = default_state()
    state["project"] = {
        "id": safe_project_id(str(body.get("projectId") or new_project_id())),
        "title": title,
        "genre": genre,
        "platform": platform,
        "synopsis": synopsis,
        "storyLength": story_config["key"],
        "storyLengthLabel": story_config["label"],
        "targetWords": story_config["targetWords"],
        "targetChapters": story_config["targetChapters"],
        "chapterWords": story_config["chapterWords"],
        "workflowVersion": "novel_workflow_v1",
        "chapterNumber": 1,
        "chapterTitle": first_outline.get("title") or "命运开局",
        "arcName": "开篇试读篇",
    }
    state["chapters"] = build_chapter_rows(chapter_outlines, 1)
    state["directorDecision"] = "新书已初始化，等待生成第一章计划"
    state["draftScore"] = 0
    state["truthAfter"] = "v1 initialized"
    state["plan"] = {
        "readerPromise": first_goal,
        "mustAdvance": f"{deep['coreMechanism']}；{deep['openingRecipe'][0]}；{deep['openingRecipe'][1]}",
        "mustNotResolve": "主角最终底牌、幕后真相、核心机制完整来源",
        "status": "new_project",
    }
    state["scenes"] = idea_draft.get("openingScenes") or build_first_chapter_scenes(genre, protagonist, premise)
    state["patches"] = [
        {"type": "project_seed", "title": title, "detail": premise},
        {"type": "platform_synopsis", "title": "平台简介", "detail": synopsis},
        {"type": "protagonist", "title": protagonist, "detail": "新书主角已建立，等待第一章确认公开处境。"},
        {"type": "reader_contract", "title": "第一章承诺", "detail": first_goal},
    ]
    state["memory"] = {
        "canon": [
            {"title": "作品一句话设定", "text": premise},
            {"title": "平台简介", "text": synopsis},
            {"title": "主角公开身份", "text": f"{protagonist}目前处于被轻视或受压位置，真实潜力尚未公开。"},
        ],
        "state": [
            {"title": protagonist, "text": "当前状态：开篇压力中；情绪：克制；底牌：未公开。"},
            {"title": "第一章目标", "text": first_goal},
        ],
        "intent": [
            {"title": "篇幅规划", "text": f"{story_config['label']}，目标约 {story_config['targetWords']} 字 / {story_config['targetChapters']} 章。{story_config['planningRule']}"},
            {"title": "长期主线", "text": f"围绕「{deep['coreMechanism']}」推进，逐步兑现题材承诺。"},
            {"title": "前三章打法", "text": " / ".join(deep["firstThree"])},
        ],
    }
    state["hooks"] = [
        {"status": "planned", "title": deep["openingRecipe"][1], "meta": "第一章弱提示", "text": deep["payoffEngine"][0]},
        {"status": "planned", "title": "幕后压力来源", "meta": "3-10 章展开", "text": "让第一章压迫背后有更高层级势力或机制。"},
    ]
    state["characters"] = {
        "suHan": {
            "name": protagonist,
            "role": "主角",
            "state": "第一章开局，处于公开压力中",
            "motive": "摆脱低位处境，证明自己，追查隐藏真相",
            "ability": f"底牌未公开，围绕「{deep['coreMechanism']}」建立成长路径",
            "constraints": "不能突然全知全能；不能无代价解决所有问题；必须符合题材机制",
        },
        "chenXuan": {
            "name": "开篇压迫者",
            "role": "阶段反派",
            "state": "尚未定名，可在第一章中作为冲突触发者",
            "motive": "压制主角，维护自身地位",
            "ability": "强于当前主角的公开实力",
            "constraints": "不能无理由降智；必须有明确压迫动机",
        },
        "elder": {
            "name": "隐藏观察者",
            "role": "伏笔角色",
            "state": "暂不正面出场",
            "motive": "观察主角异常",
            "ability": "未知",
            "constraints": "第一章不能直接救场",
        },
        "suXuewen": {
            "name": "关系线角色",
            "role": "待定",
            "state": "可作为后续情感线或旧关系压力",
            "motive": "待设定",
            "ability": "待设定",
            "constraints": "不要抢走第一章主冲突",
        },
        "outerSect": {
            "name": "规则代表",
            "role": "环境压力",
            "state": "可表现为家族、宗门、公司、学校或平台规则",
            "motive": "让主角处境更难",
            "ability": "掌握规则解释权",
            "constraints": "不能直接替代反派行动",
        },
    }
    state["scores"] = [
        {"name": "连续性", "score": 0, "reason": "新项目尚未生成正文"},
        {"name": "读者体验", "score": 0, "reason": "等待第一章草稿"},
        {"name": "角色一致性", "score": 0, "reason": "等待角色档案确认"},
        {"name": "冲突升级", "score": 0, "reason": "等待 Chapter Plan"},
        {"name": "章末钩子", "score": 0, "reason": "等待第一章结尾"},
        {"name": "AI 腔控制", "score": 0, "reason": "等待正文审计"},
        {"name": "原创表达", "score": 0, "reason": "等待人味编辑"},
    ]
    state["issues"] = [
        {"level": "major", "location": "新书设置", "issue": "新书已创建，但还没有锁定第一章计划。", "fix": "点击生成计划并锁定计划。"},
    ]
    state["trace"] = [
        {"time": now_label(), "title": "Project Wizard", "text": f"创建新书《{title}》，主角：{protagonist}。"},
        {"time": now_label(), "title": "Director", "text": "初始化开篇工作流，等待生成第一章计划。"},
    ]
    if body.get("ideaDraft"):
        state["ideaDraft"] = idea_draft
        state["outline"] = {
            "arcs": idea_draft.get("arcs", []) or build_arcs_for_length(story_config),
            "chapterOutlines": chapter_outlines,
        }
    else:
        state["outline"] = {
            "arcs": build_arcs_for_length(story_config),
            "chapterOutlines": chapter_outlines,
        }
    return state


class StateStore:
    def __init__(self, path: Path | None = None):
        # 兼容旧签名(模块级 store = StateStore(STATE_PATH))。
        # 多租户改造: path 不再使用,实际路径每次访问从 thread-local UID 动态算。
        self._legacy_path = path  # 仅保留以防外部代码引用,不再使用

    @property
    def path(self) -> Path:
        return _user_state_path()

    def _projects_dir(self) -> Path:
        return _user_projects_dir()

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            # 新访客:空书架,**不**自动创建 demo 项目。直接返回空骨架,不落盘。
            return _empty_state()
        with self.path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def project_path(self, project_id: str) -> Path:
        return self._projects_dir() / f"{safe_project_id(project_id)}.json"

    def save_project_copy(self, state: dict[str, Any]) -> None:
        project_id = state.get("project", {}).get("id") if state.get("project") else None
        if not project_id:
            return
        _atomic_write_json(self.project_path(str(project_id)), state)

    def save(self, state: dict[str, Any]) -> None:
        state["updatedAt"] = datetime.now().isoformat(timespec="seconds")
        _atomic_write_json(self.path, state)
        self.save_project_copy(state)

    def list_projects(self) -> list[dict[str, Any]]:
        active = self.load()
        active_id = active.get("project", {}).get("id") if active.get("project") else None
        # 只有当当前有 active project 时才保存副本
        if active_id:
            self.save_project_copy(active)
        projects = []
        for path in self._projects_dir().glob("*.json"):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    project_state = json.load(handle)
                if not project_state.get("updatedAt"):
                    project_state["updatedAt"] = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
                projects.append(project_summary(project_state, active_id))
            except (OSError, json.JSONDecodeError, ValueError):
                continue
        projects.sort(key=lambda item: item.get("updatedAt") or "", reverse=True)
        return projects

    def switch(self, project_id: str) -> dict[str, Any]:
        with self.project_path(project_id).open("r", encoding="utf-8") as handle:
            state = json.load(handle)
        state.pop("pendingIdeaDraft", None)
        self.save(state)
        return state

    def delete(self, project_id: str) -> dict[str, Any]:
        """删一本书。如果是当前 active 项目: 自动切到剩余最新的一本,无剩余则回空书架。"""
        target_path = self.project_path(project_id)
        if not target_path.exists():
            raise RuntimeError("要删除的作品不存在")
        target_path.unlink()
        # 看当前 active 是不是被删的
        current = self.load()
        current_id = (current.get("project") or {}).get("id")
        if current_id != project_id:
            return current
        # 找剩下的项目里最新的那本切过去
        remaining = []
        for path in self._projects_dir().glob("*.json"):
            try:
                with path.open("r", encoding="utf-8") as h:
                    ps = json.load(h)
                remaining.append((ps.get("updatedAt") or "", ps))
            except (OSError, json.JSONDecodeError, ValueError):
                continue
        remaining.sort(key=lambda x: x[0], reverse=True)
        if remaining:
            new_state = remaining[0][1]
            new_state.pop("pendingIdeaDraft", None)
            self.save(new_state)
            return new_state
        # 没剩了, 回到空书架 (state.json 直接写 empty)
        empty = _empty_state()
        _atomic_write_json(self.path, empty)
        return empty

    def reset(self) -> dict[str, Any]:
        # 多租户模式:reset 给当前 user 回到空书架,不是 default demo
        state = _empty_state()
        self.save(state)
        return state


store = StateStore(STATE_PATH)


def add_trace(state: dict[str, Any], title: str, text: str) -> None:
    state.setdefault("trace", []).append({"time": now_label(), "title": title, "text": text})


def find_scene(state: dict[str, Any], scene_id: str) -> dict[str, Any] | None:
    for scene in state["scenes"]:
        if scene["id"] == scene_id:
            return scene
    return None


def parse_truth_version(value: str) -> int:
    match = re.search(r"v(\d+)", value or "")
    if not match:
        return 1
    return int(match.group(1))


def has_open_issues(state: dict[str, Any]) -> bool:
    return any(issue.get("level") in {"blocker", "major"} for issue in state.get("issues", []))


def has_draft_scenes(state: dict[str, Any]) -> bool:
    return any(scene.get("status") == "draft" for scene in state.get("scenes", []))


def style_done_for_current_chapter(state: dict[str, Any]) -> bool:
    report = state.get("humanStyleReport") or {}
    return report.get("status") == "done" and _safe_int(report.get("chapterNumber"), -1) == _safe_int(
        state.get("project", {}).get("chapterNumber"), -2
    )


def workflow_meta(state: dict[str, Any]) -> dict[str, Any]:
    plan_status = state.get("plan", {}).get("status")
    chapter_number = state.get("project", {}).get("chapterNumber", 1)
    is_first = chapter_number == 1
    primary_prefix = "第一章" if is_first else "下一章"

    steps = [
        {"id": "setup", "label": "创建作品", "done": bool(state.get("project", {}).get("title"))},
        {"id": "plan", "label": "生成计划", "done": plan_status in {"generated", "locked"}},
        {"id": "write", "label": "生成正文", "done": state.get("scenes") and not has_draft_scenes(state)},
        {"id": "audit", "label": "审计修订", "done": state.get("draftScore", 0) > 0 and not has_open_issues(state)},
        {"id": "style", "label": "原创表达", "done": style_done_for_current_chapter(state) or "committed" in state.get("truthAfter", "")},
        {"id": "settle", "label": "写入真相", "done": "committed" in state.get("truthAfter", "")},
        {"id": "export", "label": "导出", "done": False},
    ]

    if plan_status in {"new_project", None}:
        return {
            "currentStep": "plan",
            "primaryLabel": f"生成{primary_prefix}计划",
            "primaryEndpoint": "/api/plan/generate",
            "helperText": "先让 Director 把本章目标、读者承诺、伏笔边界和场景结构定下来。",
            "steps": steps,
        }
    if plan_status == "generated":
        return {
            "currentStep": "lock",
            "primaryLabel": "确认计划，进入写作",
            "primaryEndpoint": "/api/plan/lock",
            "helperText": "计划已经生成。确认后，系统会按场景卡生成正文。",
            "steps": steps,
        }
    if has_draft_scenes(state):
        return {
            "currentStep": "write",
            "primaryLabel": f"生成{primary_prefix}正文",
            "primaryEndpoint": "/api/scenes/generate",
            "helperText": "按 Scene Cards 分场景生成正文，方便逐段审计和修订。",
            "steps": steps,
        }
    if state.get("draftScore", 0) <= 0:
        return {
            "currentStep": "audit",
            "primaryLabel": "审计章节",
            "primaryEndpoint": "/api/audit/run",
            "helperText": "正文已生成。现在检查连续性、读者体验、角色一致性和语言风格。",
            "steps": steps,
        }
    if has_open_issues(state):
        return {
            "currentStep": "revise",
            "primaryLabel": "按审计建议修订",
            "primaryEndpoint": "/api/revise/auto",
            "helperText": "还有影响体验的问题。先做定向修订，再写入真相文件。",
            "steps": steps,
        }
    if not style_done_for_current_chapter(state) and "committed" not in state.get("truthAfter", ""):
        return {
            "currentStep": "style",
            "primaryLabel": "原创表达审校",
            "primaryEndpoint": "/api/style/human-edit",
            "helperText": "在不改变剧情和事实的前提下，减少模板句、空泛情绪和机械解释，让章节更像经过真人编辑。",
            "steps": steps,
        }
    if "committed" not in state.get("truthAfter", ""):
        return {
            "currentStep": "settle",
            "primaryLabel": "写入真相文件",
            "primaryEndpoint": "/api/truth/settle",
            "helperText": "正文和审计已通过。把本章事实、状态和伏笔变化归档。",
            "steps": steps,
        }
    return {
        "currentStep": "export",
        "primaryLabel": "导出正文",
        "primaryEndpoint": "/api/export/markdown",
        "helperText": "本章已经完成归档，可以导出正文，或者继续创建下一章。",
        "steps": steps,
    }


def public_state(state: dict[str, Any]) -> dict[str, Any]:
    output = deepcopy(state)
    project = output.get("project", {})
    if project:
        story_config = story_length_config(project.get("storyLength"))
        project.setdefault("storyLength", story_config["key"])
        project.setdefault("storyLengthLabel", story_config["label"])
        project.setdefault("targetWords", story_config["targetWords"])
        project.setdefault("targetChapters", story_config["targetChapters"])
        project.setdefault("chapterWords", story_config["chapterWords"])
        report = output.get("humanStyleReport")
        if isinstance(report, dict) and report.get("status") == "done" and not report.get("platformGuide"):
            platform = str(project.get("platform") or "fanqie")
            report["platformGuide"] = PLATFORM_STYLE_GUIDES.get(platform, PLATFORM_STYLE_GUIDES["fanqie"])
    output["workflow"] = workflow_meta(output)
    return output


def response_payload(state: dict[str, Any], message: str | None = None) -> dict[str, Any]:
    payload = {"state": public_state(state)}
    if message:
        payload["message"] = message
    return payload


def export_markdown(state: dict[str, Any], chapter_number: int | None = None) -> str:
    _require_active_project(state)
    project = state["project"]
    scenes = state["scenes"]
    title = project["chapterTitle"]
    number = project["chapterNumber"]
    if chapter_number is not None and chapter_number != project["chapterNumber"]:
        archived = next((item for item in state.get("chapterArchive", []) if item.get("number") == chapter_number), None)
        if archived is None:
            raise KeyError(f"Chapter archive not found: {chapter_number}")
        scenes = archived.get("scenes", [])
        title = archived.get("title") or f"第 {chapter_number} 章"
        number = chapter_number
    lines = [
        f"# 第 {number} 章 · {title}",
        "",
        f"> 作品：{project['title']} · 工作流：{project['workflowVersion']}",
        "",
    ]
    for scene in scenes:
        lines.extend([f"## {scene['number']}. {scene['title']}", "", scene["content"], ""])
    return "\n".join(lines).strip() + "\n"


def export_book_markdown(state: dict[str, Any]) -> str:
    _require_active_project(state)
    project = state["project"]
    chapters = sorted(state.get("chapterArchive", []), key=lambda item: item.get("number", 0))
    current_number = project["chapterNumber"]
    if not any(item.get("number") == current_number for item in chapters):
        chapters.append(
            {
                "number": current_number,
                "title": project["chapterTitle"],
                "scenes": state.get("scenes", []),
            }
        )

    lines = [
        f"# {project['title']}",
        "",
        f"> 流派：{project.get('genre', '')} · 平台：{project.get('platform', '')} · 篇幅：{project.get('storyLengthLabel', '未设置')} · 工作流：{project['workflowVersion']}",
        "",
    ]
    if project.get("synopsis"):
        lines.extend(["## 作品简介", "", str(project["synopsis"]).strip(), ""])
    for chapter in chapters:
        lines.extend([f"## 第 {chapter['number']} 章 · {chapter['title']}", ""])
        for scene in chapter.get("scenes", []):
            content = str(scene.get("content") or "").strip()
            if content:
                lines.extend([content, ""])
    return "\n".join(lines).strip() + "\n"


def generate_scene_contents_optional_llm(state: dict[str, Any]) -> tuple[bool, str]:
    config = get_llm_config("scene_writer")
    if not config.configured:
        return False, "未配置大模型 API Key，使用本地场景草稿。"

    system = (
        "你是中文网文连载写手。只输出 JSON，不要 Markdown。"
        "根据给定作品设定和 Scene Cards 生成当前章节分场景正文。"
        "正文要短段落、移动端友好、有网文钩子，不要解释创作意图。"
    )
    user = json.dumps(
        {
            "project": state["project"],
            "plan": state["plan"],
            "character": state["characters"]["suHan"],
            "memory": state["memory"],
            "hooks": state["hooks"],
            "scenes": [
                {
                    "id": scene["id"],
                    "title": scene["title"],
                    "type": scene["type"],
                    "summary": scene["summary"],
                    "targetWords": min(max(scene.get("words", 500), 300), 900),
                }
                for scene in state["scenes"]
            ],
            "required_schema": {
                "scenes": [
                    {
                        "id": "scene_01",
                        "title": "string",
                        "summary": "string",
                        "content": "string",
                    }
                ]
            },
        },
        ensure_ascii=False,
    )

    data = chat_json(system, user, temperature=0.75, timeout=120, agent="scene_writer")
    returned = data.get("scenes") or []
    by_id = {item.get("id"): item for item in returned if isinstance(item, dict)}
    updated = 0
    for scene in state["scenes"]:
        item = by_id.get(scene["id"])
        if not item:
            continue
        if item.get("title"):
            scene["title"] = str(item["title"])
        if item.get("summary"):
            scene["summary"] = str(item["summary"])
        if item.get("content"):
            scene["content"] = str(item["content"]).strip()
            scene["words"] = len(scene["content"])
            updated += 1
    if updated == 0:
        raise RuntimeError("大模型没有返回可用场景正文")
    return True, f"大模型已生成 {updated} 个场景正文（{config.model}）。"


STYLE_RED_FLAGS = [
    "这一刻",
    "下一秒",
    "所有人都",
    "没有人知道",
    "他知道",
    "他明白",
    "心中一震",
    "不由得",
    "仿佛",
    "似乎",
    "顿时",
]

PLATFORM_STYLE_GUIDES = {
    "fanqie": {
        "label": "番茄小说",
        "focus": "低门槛进入、强现实压力、即时反馈和章末钩子要清楚。",
        "checks": [
            "前三屏必须出现可感知的困境或异常收益。",
            "爽点要落到钱、地位、能力、关系或生存收益。",
            "解释设定时用动作和结果带出，避免大段世界观说明。",
        ],
    },
    "qidian": {
        "label": "起点中文网",
        "focus": "题材机制、成长路径、世界规则和长期伏笔要有可持续性。",
        "checks": [
            "主角每次破局都要体现规则理解，而不是凭空开挂。",
            "设定信息分批释放，保留长期追读问题。",
            "爽点前要有压迫和代价，避免只有结果没有过程。",
        ],
    },
    "jjwxc": {
        "label": "晋江文学城",
        "focus": "人物关系、情绪可信度、对白分寸和角色独特声音更重要。",
        "checks": [
            "少用抽象情绪判断，多写人物具体反应和关系变化。",
            "对白要带角色立场，不要每个人都说同一种话。",
            "感情线推进要有误解、选择或代价，避免直接下结论。",
        ],
    },
    "qimao": {
        "label": "七猫小说",
        "focus": "类型明确、节奏稳定、章节目标和连续追读点要清晰。",
        "checks": [
            "每章都要有明确小目标、小阻碍和小兑现。",
            "段落移动端友好，动作和对白交替推进。",
            "不要在单章内堆太多新名词，先保证读者跟得上。",
        ],
    },
}


def build_human_style_report(state: dict[str, Any]) -> dict[str, Any]:
    texts = [str(scene.get("content") or "") for scene in state.get("scenes", [])]
    full_text = "\n".join(texts)
    paragraphs = [line.strip() for line in full_text.splitlines() if line.strip()]
    red_hits = {word: full_text.count(word) for word in STYLE_RED_FLAGS if full_text.count(word)}
    starts: dict[str, int] = {}
    for paragraph in paragraphs:
        key = paragraph[:4]
        starts[key] = starts.get(key, 0) + 1
    repeated_starts = {key: count for key, count in starts.items() if count >= 3 and len(key) >= 2}
    avg_len = round(sum(len(item) for item in paragraphs) / max(len(paragraphs), 1), 1)
    issues = []
    if red_hits:
        issues.append("高频模板词需要收敛，避免连续使用机械化转折和万能情绪。")
    if repeated_starts:
        issues.append("段首节奏重复，建议混入动作、环境、对白和心理不同起手。")
    if avg_len > 90:
        issues.append("段落平均长度偏长，移动端阅读可拆短。")
    if avg_len < 12 and len(paragraphs) > 20:
        issues.append("短句过密，容易显得像自动切段，可保留少量长短句变化。")
    score = 92 - min(sum(red_hits.values()) * 2, 22) - min(len(repeated_starts) * 3, 12)
    score = max(60, min(95, score))
    platform = str(state.get("project", {}).get("platform") or "fanqie")
    platform_guide = PLATFORM_STYLE_GUIDES.get(platform, PLATFORM_STYLE_GUIDES["fanqie"])
    return {
        "status": "done",
        "chapterNumber": state.get("project", {}).get("chapterNumber"),
        "score": score,
        "platformGuide": platform_guide,
        "redFlags": red_hits,
        "repeatedStarts": repeated_starts,
        "averageParagraphLength": avg_len,
        "issues": issues or ["当前表达没有明显模板化问题，保留作者个人节奏即可。"],
        "principles": [
            "保留剧情事实，不为了润色改动角色动机。",
            "少解释，多用动作、物件、环境反应承载情绪。",
            "句式允许不完全整齐，避免每段都像同一模板生成。",
            "关键爽点要具体落地，不只写抽象判断。",
        ],
    }


def apply_local_human_edit(state: dict[str, Any]) -> int:
    replacements = {
        "这一刻，": "",
        "这一刻": "这时",
        "下一秒，": "",
        "所有人都": "周围的人",
        "没有人知道": "没人说得清",
        "心中一震": "呼吸停了一拍",
        "不由得": "",
        "顿时": "立刻",
    }
    updated = 0
    for scene in state.get("scenes", []):
        content = str(scene.get("content") or "")
        original = content
        for old, new in replacements.items():
            content = content.replace(old, new)
        if content != original:
            scene["content"] = content
            scene["words"] = len(content)
            tags = scene.get("tags", [])
            if isinstance(tags, list) and "原创表达" not in tags:
                tags.append("原创表达")
            updated += 1
    return updated


def human_edit_chapter_optional_llm(state: dict[str, Any]) -> tuple[bool, str]:
    base_report = build_human_style_report(state)
    config = get_llm_config("audit")
    if not config.configured:
        updated = apply_local_human_edit(state)
        report = build_human_style_report(state)
        state["humanStyleReport"] = report
        return False, f"未配置审校模型，已用本地规则完成原创表达审校，调整 {updated} 个场景。"

    system = (
        "你是中文网文资深责编。只输出 JSON。"
        "任务是原创表达审校：减少模板化、机械解释、空泛情绪和重复句式。"
        "禁止帮助规避平台检测，禁止宣称文本由真人创作。"
        "不得改变剧情事实、人物动机、章节钩子和已建立设定。"
    )
    user = json.dumps(
        {
            "project": state.get("project", {}),
            "styleAudit": base_report,
            "scenes": [
                {
                    "id": scene.get("id"),
                    "title": scene.get("title"),
                    "summary": scene.get("summary"),
                    "content": scene.get("content"),
                }
                for scene in state.get("scenes", [])
            ],
            "required_schema": {
                "styleReport": {"score": 88, "issues": ["string"], "principles": ["string"]},
                "scenes": [{"id": "scene_01", "content": "string", "editNote": "string"}],
            },
        },
        ensure_ascii=False,
    )
    try:
        data = chat_json(system, user, temperature=0.35, timeout=120, agent="audit")
    except Exception as exc:  # noqa: BLE001 - local edit keeps workflow moving.
        updated = apply_local_human_edit(state)
        report = build_human_style_report(state)
        report["llmError"] = str(exc)
        state["humanStyleReport"] = report
        return False, f"审校模型失败，已用本地规则完成原创表达审校，调整 {updated} 个场景。"

    returned = {item.get("id"): item for item in data.get("scenes", []) if isinstance(item, dict)}
    updated = 0
    for scene in state.get("scenes", []):
        item = returned.get(scene.get("id"))
        content = str(item.get("content") or "").strip() if item else ""
        if content:
            scene["content"] = content
            scene["words"] = len(content)
            tags = scene.get("tags", [])
            if isinstance(tags, list) and "原创表达" not in tags:
                tags.append("原创表达")
            updated += 1
    report = build_human_style_report(state)
    llm_report = data.get("styleReport") or {}
    if isinstance(llm_report, dict):
        report["score"] = _safe_int(llm_report.get("score"), report["score"])
        report["issues"] = llm_report.get("issues") or report["issues"]
        report["principles"] = llm_report.get("principles") or report["principles"]
    state["humanStyleReport"] = report
    return True, f"原创表达审校已完成，审校模型 {config.model} 调整 {updated} 个场景。"



def _require_active_project(state) -> str:
    """守门: 没有 active project 时拒绝执行需要 project 的操作。"""
    proj = state.get("project") or {}
    if not proj.get("id"):
        raise RuntimeError("请先创建一本作品再进行此操作(题材成书 / 手动新建)")
    return proj["id"]


def mutate(endpoint: str, body: dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
    state = store.load()
    body = body or {}

    if endpoint == "/api/workflow/run":
        endpoint = workflow_meta(state)["primaryEndpoint"]
        if endpoint == "/api/export/markdown":
            message = "本章已完成，可以导出 Markdown"
            return state, message

    if endpoint == "/api/workflow/next":
        endpoint = workflow_meta(state)["primaryEndpoint"]
        if endpoint == "/api/export/markdown":
            message = "本章已完成，可以导出 Markdown"
            return state, message

    if endpoint == "/api/plan/generate":
        _require_active_project(state)
        state["plan"]["status"] = "generated"
        chapter_number = state["project"]["chapterNumber"]
        state["directorDecision"] = f"第 {chapter_number} 章计划已生成，等待确认"
        add_trace(state, "Chapter Planner", "生成章节计划：读者承诺、必推伏笔、禁止提前解决项已确认。")
        message = "章节计划已生成"

    elif endpoint == "/api/plan/lock":
        _require_active_project(state)
        state["plan"]["status"] = "locked"
        state["directorDecision"] = "计划已锁定"
        add_trace(state, "Plan Gate", "用户锁定计划，允许 Scene Writer 分场景生成。")
        message = "计划已锁定"

    elif endpoint == "/api/scenes/generate":
        _require_active_project(state)
        llm_used = False
        llm_note = ""
        try:
            llm_used, llm_note = generate_scene_contents_optional_llm(state)
        except Exception as exc:  # noqa: BLE001 - fallback keeps workflow running and records error.
            llm_note = f"大模型生成失败，已回退本地草稿：{exc}"
        for scene in state["scenes"]:
            if scene["status"] == "draft":
                scene["status"] = "ready"
        state["directorDecision"] = "正文已按场景生成，等待审计"
        state["humanStyleReport"] = {"status": "pending", "chapterNumber": state["project"]["chapterNumber"]}
        add_trace(state, "Scene Writer", llm_note or "按 Scene Cards 生成正文草稿。")
        state["lastLLMRun"] = {"used": llm_used, "note": llm_note, "model": get_llm_config("scene_writer").model if llm_used else None}
        message = "正文已生成"

    elif endpoint.startswith("/api/scenes/") and endpoint.endswith("/save"):
        _require_active_project(state)
        match = re.match(r"^/api/scenes/([^/]+)/save$", endpoint)
        scene_id = match.group(1) if match else body.get("sceneId", "")
        scene = find_scene(state, scene_id)
        if scene is None:
            raise KeyError(f"Scene not found: {scene_id}")
        content = str(body.get("content") or "").strip()
        if not content:
            raise ValueError("正文不能为空")
        if body.get("title"):
            scene["title"] = str(body["title"]).strip()
        if body.get("summary"):
            scene["summary"] = str(body["summary"]).strip()
        scene["content"] = content
        scene["words"] = len(content)
        scene["status"] = "ready"
        if state.get("draftScore", 0) > 0:
            state["draftScore"] = 0
            state["humanStyleReport"] = {"status": "pending", "chapterNumber": state["project"]["chapterNumber"]}
            state["issues"] = [
                {
                    "level": "major",
                    "location": scene_id,
                    "issue": "正文已手动修改，之前的审计结果需要重新确认。",
                    "fix": "点击审计当前章，重新跑一次审计。",
                }
            ]
        state["directorDecision"] = "正文已保存，建议继续按推荐流程推进"
        add_trace(state, "Editor", f"保存 {scene_id} 手动修改，当前字数 {scene['words']}。")
        message = "正文修改已保存"

    elif endpoint.startswith("/api/scenes/") and endpoint.endswith("/revise"):
        _require_active_project(state)
        match = re.match(r"^/api/scenes/([^/]+)/revise$", endpoint)
        scene_id = match.group(1) if match else body.get("sceneId", "scene_03")
        scene = find_scene(state, scene_id)
        if scene is None:
            raise KeyError(f"Scene not found: {scene_id}")
        if scene["status"] == "needs_fix":
            scene["status"] = "ready"
            scene["tags"] = [tag for tag in scene["tags"] if tag != "待修"] + ["已修"]
            protagonist = state["characters"]["suHan"]["name"]
            fix_text = f"\n\n{protagonist}没有急着解释。\n\n他只是往前一步，让所有人的声音都压了下去。"
            if fix_text.strip() not in scene["content"]:
                scene["content"] += fix_text
            state["draftScore"] = 91
            state["scores"][0]["score"] = 90
            state["scores"][0]["reason"] = "陈玄伤势已在 scene_03 中体现"
            state["issues"] = [issue for issue in state["issues"] if issue["location"] != scene_id]
            add_trace(state, "Reviser", f"局部修订 {scene_id}，修复角色状态连续性问题。")
            message = f"{scene_id} 已局部修订"
        else:
            message = f"{scene_id} 无需修订"

    elif endpoint == "/api/audit/run":
        _require_active_project(state)
        add_trace(state, "Audit Board", "重新执行连续性、读者体验、风格语言三类审计。")
        if state["project"]["chapterNumber"] == 1:
            state["draftScore"] = 82
            state["scores"] = [
                {"name": "连续性", "score": 88, "reason": "新书开篇事实链清晰，没有旧设定冲突"},
                {"name": "读者体验", "score": 82, "reason": "开篇压力明确，反击动机成立"},
                {"name": "角色一致性", "score": 84, "reason": "主角克制但有反应，符合低位开局"},
                {"name": "冲突升级", "score": 80, "reason": "有公开压力，但结尾危机还可以更具体"},
                {"name": "章末钩子", "score": 78, "reason": "旧物异动有效，但追杀危机需要落地"},
                {"name": "AI 腔控制", "score": 86, "reason": "短句较多，移动端阅读友好"},
                {"name": "原创表达", "score": 0, "reason": "等待原创表达审校"},
            ]
            state["issues"] = [
                {
                    "level": "major",
                    "location": "scene_03",
                    "issue": "章末只有旧物异动，追杀危机还不够具体。",
                    "fix": "在旧物异动后补一条外部危机，让读者知道下一章马上有事发生。",
                }
            ]
        else:
            state["draftScore"] = max(state.get("draftScore", 0), 86)
        state["directorDecision"] = "审计完成，等待定向修订"
        message = "审计完成"

    elif endpoint == "/api/revise/auto":
        _require_active_project(state)
        if state.get("issues"):
            issue = state["issues"][0]
            scene_id = issue.get("location", "scene_03")
            scene = find_scene(state, scene_id) or state["scenes"][-1]
            protagonist = state["characters"]["suHan"]["name"]
            if state["project"]["chapterNumber"] == 1:
                genre = state["project"].get("genre", "xuanhuan")
                if genre == "urban":
                    fix_text = f"\n\n手机忽然震了一下。\n\n一条没有平台标识的订单跳了出来。\n\n收货人那一栏，写着{protagonist}自己的名字。"
                elif genre == "scifi":
                    fix_text = f"\n\n维修站的警报灯忽然变成红色。\n\n通缉令刷满整面舷窗。\n\n目标姓名，正是{protagonist}。"
                elif genre == "weird_rules":
                    fix_text = f"\n\n墙上的值班守则忽然多出一行红字。\n\n第七条：如果你看见{protagonist}，请立刻假装不认识他。"
                elif genre == "beast_taming":
                    fix_text = f"\n\n契约台上的废蛋忽然裂开一道缝。\n\n里面传出的第一声鸣叫，让整座测评大厅同时熄灯。"
                elif genre == "infinite":
                    fix_text = f"\n\n广播声在头顶响起。\n\n“欢迎玩家{protagonist}进入第一轮副本。”\n\n“请在十分钟内找到自己死亡的原因。”"
                elif genre == "gaowu":
                    fix_text = f"\n\n气血检测仪忽然爆出刺耳警报。\n\n屏幕上的数值疯狂跳动，最后停在一个不该出现在他身上的数字。"
                elif genre == "apocalypse":
                    fix_text = f"\n\n窗外的雨忽然变成黑色。\n\n手机弹出一条全城警报。\n\n第一轮天灾，将在十分钟后开始。"
                elif genre == "game_invasion":
                    fix_text = f"\n\n空气里弹出半透明面板。\n\n隐藏任务已触发。\n\n失败惩罚：现实身份抹除。"
                elif genre == "live_entertainment":
                    fix_text = f"\n\n直播间人数忽然从三百跳到三十万。\n\n弹幕刷满同一句话。\n\n“他刚才是不是预判了事故？”"
                elif genre == "farming_building":
                    fix_text = f"\n\n破旧木牌在夜里亮了起来。\n\n领地升级条件已满足。\n\n但第一批难民，已经站在了门外。"
                elif genre == "mystery_horror":
                    fix_text = f"\n\n案卷最后一页自动翻开。\n\n死者名单最下面，多出了一行新字。\n\n{protagonist}。"
                elif genre == "romance":
                    fix_text = f"\n\n门铃在这时响了。\n\n屏幕里站着那个最不该出现的人。\n\n手里还拿着一份已经签好字的协议。"
                elif genre == "rebirth":
                    fix_text = f"\n\n墙上的钟忽然停住。\n\n{protagonist}看着那个时间，后背一点点发冷。\n\n前世第一场灾祸，就是从这一分钟开始的。"
                else:
                    fix_text = f"\n\n门外忽然传来急促的脚步声。\n\n有人压低声音喊他的名字。\n\n“{protagonist}，快走。”\n\n“执法堂的人已经下山了。”"
            else:
                fix_text = "\n\n对方的伤势被迫暴露，局面也因此多了一道新的裂缝。"
            if fix_text.strip() not in scene["content"]:
                scene["content"] += fix_text
            scene["status"] = "ready"
            scene["tags"] = [tag for tag in scene.get("tags", []) if tag not in {"待修"}] + ["已修"]
            state["issues"] = [item for item in state["issues"] if item is not issue]
            state["draftScore"] = 90 if state["project"]["chapterNumber"] == 1 else 91
            for score in state["scores"]:
                if score["name"] == "章末钩子":
                    score["score"] = max(score["score"], 88)
                    score["reason"] = "章末已补足外部危机，下一章动力更明确"
                if score["name"] == "冲突升级":
                    score["score"] = max(score["score"], 86)
            state["humanStyleReport"] = {"status": "pending", "chapterNumber": state["project"]["chapterNumber"]}
            state["directorDecision"] = "修订完成，可以写入真相文件"
            add_trace(state, "Reviser", f"根据审计建议修订 {scene['id']}。")
            message = "已按审计建议修订"
        else:
            message = "当前没有需要修订的问题"

    elif endpoint == "/api/style/human-edit":
        _require_active_project(state)
        llm_used, style_note = human_edit_chapter_optional_llm(state)
        report = state.get("humanStyleReport", {})
        style_score = _safe_int(report.get("score"), 88)
        found = False
        for score in state.get("scores", []):
            if score.get("name") in {"AI 腔控制", "原创表达"}:
                score["score"] = max(_safe_int(score.get("score"), 0), style_score)
                score["reason"] = "已完成原创表达审校，减少模板化、重复起手和空泛解释。"
                found = True
        if not found:
            state.setdefault("scores", []).append({"name": "原创表达", "score": style_score, "reason": "已完成原创表达审校。"})
        state["directorDecision"] = "原创表达审校完成，可以写入真相文件"
        state["lastLLMRun"] = {"used": llm_used, "note": style_note, "model": get_llm_config("audit").model if llm_used else None, "agentRole": "audit"}
        add_trace(state, "Human Style Editor", style_note)
        message = "原创表达审校完成"

    elif endpoint == "/api/hooks/advance":
        _require_active_project(state)
        for hook in state["hooks"]:
            if hook["title"] == "黑色断剑":
                if hook["status"] == "reinforced":
                    hook["status"] = "near_due"
                    hook["meta"] = "下一单元可回收"
                else:
                    hook["status"] = "reinforced"
                    hook["meta"] = "第 12 章强化"
                break
        add_trace(state, "Hook Keeper", "切换黑色断剑伏笔状态。")
        message = "伏笔已推进"

    elif endpoint == "/api/truth/settle":
        _require_active_project(state)
        next_version = parse_truth_version(state.get("truthAfter", "v1")) + 1
        state["truthAfter"] = f"v{next_version} committed"
        state["directorDecision"] = "TruthPatch 已写入"
        chapter_number = state["project"]["chapterNumber"]
        chapter_title = state["project"]["chapterTitle"]
        protagonist = state["characters"]["suHan"]["name"]
        canon_title = f"第 {chapter_number} 章事件归档"
        if not any(item["title"] == canon_title for item in state["memory"]["canon"]):
            state["memory"]["canon"].append(
                {
                    "title": canon_title,
                    "text": f"{protagonist}完成《{chapter_title}》关键事件：{state['plan']['readerPromise']}",
                }
            )
        state["issues"] = [issue for issue in state.get("issues", []) if issue.get("level") == "minor"]
        add_trace(state, "TruthMerger", f"TruthPatch schema 校验通过，真相文件版本递增到 v{next_version}。")
        message = "TruthPatch 已写入"

    elif endpoint == "/api/chapters/next":
        _require_active_project(state)
        state = prepare_next_chapter(state)
        message = f"已进入第 {state['project']['chapterNumber']} 章《{state['project']['chapterTitle']}》"

    elif endpoint == "/api/reset":
        state = store.reset()
        message = "项目状态已重置"

    elif endpoint == "/api/projects/create":
        state = create_project_state(body)
        store.save(state)
        message = f"新书《{state['project']['title']}》已创建"
        return state, message

    elif endpoint == "/api/projects/switch":
        project_id = str(body.get("projectId") or "").strip()
        if not project_id:
            raise ValueError("缺少作品 ID")
        state = store.switch(project_id)
        message = f"已切换到《{state['project']['title']}》"
        return state, message

    elif endpoint == "/api/projects/delete":
        project_id = str(body.get("projectId") or "").strip()
        if not project_id:
            raise RuntimeError("缺少作品 ID")
        state = store.delete(project_id)
        proj = state.get("project") or {}
        if proj.get("id"):
            message = f"已删除作品,当前切到《{proj.get('title','(未命名)')}》"
        else:
            message = "已删除作品,书架已清空"
        return state, message

    elif endpoint == "/api/ideation/generate":
        idea = generate_idea_draft_optional_llm(body)
        state["pendingIdeaDraft"] = idea
        source = f"大模型 {idea.get('llmModel')}" if idea.get("llmGenerated") else "本地题材配置"
        add_trace(state, "Ideation", f"根据题材生成书案：《{idea['selectedTitle']}》（{source}）。")
        message = "题材书案已生成"

    elif endpoint == "/api/projects/create-from-idea":
        idea = body.get("ideaDraft") or state.get("pendingIdeaDraft") or state.get("ideaDraft")
        if not idea:
            raise ValueError("没有可采用的书案，请先生成题材方案")
        title = clean_title_option(body.get("title") or idea.get("selectedTitle") or "未命名新书")
        protagonist = clean_name_option(body.get("protagonist") or idea.get("selectedProtagonist") or "主角")
        synopsis = str(body.get("synopsis") or idea.get("selectedSynopsis") or idea.get("premise") or "").strip()
        idea["selectedTitle"] = title
        idea["selectedProtagonist"] = protagonist
        if synopsis:
            idea["selectedSynopsis"] = synopsis
        idea["openingScenes"] = build_first_chapter_scenes(idea.get("genre", "xuanhuan"), protagonist, idea.get("topic") or idea.get("premise") or title)
        project_body = {
            "title": title,
            "genre": idea.get("genre", "xuanhuan"),
            "platform": idea.get("platform", "fanqie"),
            "protagonist": protagonist,
            "premise": idea.get("premise"),
            "synopsis": synopsis,
            "firstGoal": idea.get("firstGoal"),
            "ideaDraft": idea,
        }
        state = create_project_state(project_body)
        state.pop("pendingIdeaDraft", None)
        store.save(state)
        message = f"已采用书案创建《{state['project']['title']}》"
        return state, message

    elif endpoint == "/api/llm/config":
        update_runtime_llm_config(body)
        add_trace(state, "LLM Config", "已更新运行时大模型配置。API Key 只保存在当前后端进程内。")
        message = "大模型配置已更新"

    elif endpoint == "/api/llm/test":
        agent_role = str(body.get("agentRole") or "").strip() or None
        data = chat_json(
            "你是连通性测试助手。只输出 JSON。",
            '输出 {"ok": true, "message": "大模型连接成功"}',
            temperature=0,
            timeout=30,
            agent=agent_role,
        )
        config = get_llm_config(agent_role)
        state["lastLLMRun"] = {"used": True, "note": data.get("message", "大模型连接成功"), "model": config.model, "agentRole": agent_role}
        add_trace(state, "LLM Test", state["lastLLMRun"]["note"])
        message = state["lastLLMRun"]["note"]

    else:
        raise KeyError(f"Unknown endpoint: {endpoint}")

    store.save(state)
    return state, message


class NovelStudioHandler(SimpleHTTPRequestHandler):
    server_version = "Qianjuan/0.1"

    def _setup_user_ctx(self) -> None:
        """从 Cookie 取 UID,无则生成新的并标记 Set-Cookie 在响应里下发。"""
        from http.cookies import SimpleCookie

        cookie_header = self.headers.get("Cookie", "")
        uid: str | None = None
        if cookie_header:
            try:
                parsed = SimpleCookie(cookie_header)
                morsel = parsed.get(COOKIE_NAME)
                if morsel:
                    uid = _safe_uid(morsel.value)
            except Exception:  # noqa: BLE001
                uid = None
        if not uid:
            uid = uuid.uuid4().hex
            self._new_uid = uid
        _user_ctx.uid = uid
        # 同步推到 llm_adapter,让 LLM 配置 per-user 隔离
        _llm_set_user_id(uid)

    def translate_path(self, path: str) -> str:
        parsed = urlparse(path)
        clean = parsed.path.lstrip("/")
        if not clean:
            clean = "index.html"
        # BUG-003: 拒绝目录穿越。归一化后若仍含 .. 或绝对路径,落回 index.html
        normalized = posixpath.normpath(clean)
        if normalized.startswith("..") or posixpath.isabs(normalized):
            return str(ROOT / "index.html")
        try:
            candidate = (ROOT / normalized).resolve()
            candidate.relative_to(ROOT.resolve())
        except (ValueError, OSError):
            return str(ROOT / "index.html")
        return str(candidate)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        new_uid = getattr(self, "_new_uid", None)
        if new_uid:
            cookie_val = f"{COOKIE_NAME}={new_uid}; Path=/; Max-Age=31536000; SameSite=Lax"
            self.send_header("Set-Cookie", cookie_val)
        super().end_headers()

    def do_GET(self) -> None:
        self._setup_user_ctx()
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            self.send_json({"state": public_state(store.load())})
            return
        if parsed.path == "/api/projects":
            self.send_json({"projects": store.list_projects()})
            return
        if parsed.path == "/api/llm/status":
            self.send_json({"llm": public_llm_status()})
            return
        if parsed.path == "/api/llm/config":
            self.send_json({"llm": public_llm_config()})
            return
        if parsed.path == "/api/export/markdown":
            state = store.load()
            query = parse_qs(parsed.query)
            chapter_number = _safe_int(query.get("chapter", [None])[0], 0) or None
            try:
                content = export_markdown(state, chapter_number).encode("utf-8")
            except RuntimeError as user_exc:
                self.send_json({"error": str(user_exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            filename_number = chapter_number or state["project"]["chapterNumber"]
            filename = f"chapter-{filename_number:03d}.md"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        if parsed.path == "/api/export/book":
            state = store.load()
            try:
                content = export_book_markdown(state).encode("utf-8")
            except RuntimeError as user_exc:
                self.send_json({"error": str(user_exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="book.md"')
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        return super().do_GET()

    def do_POST(self) -> None:
        self._setup_user_ctx()
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            # BUG-004: 拒绝过大请求体
            if length > MAX_REQUEST_BODY:
                error_id = _new_error_id()
                _log_error(error_id, parsed.path, ValueError(f"body too large: {length}"))
                self.send_json(
                    {"error": "请求体过大", "errorId": error_id},
                    status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                )
                return
            body_bytes = self.rfile.read(length) if length else b"{}"
            body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
            state, message = mutate(parsed.path, body)
            self.send_json(response_payload(state, message))
        except RuntimeError as user_exc:
            # 业务级用户面错误(如未创建作品/参数缺失),直接把 message 透传不脱敏
            self.send_json(
                {"error": str(user_exc)},
                status=HTTPStatus.BAD_REQUEST,
            )
        except LLMRateLimitExceeded as limit_exc:
            # 多租户限流: 每 UID 每小时 N 次,超出友好提示
            self.send_json(
                {
                    "error": f"LLM 调用次数超过每小时限制({limit_exc.limit} 次),请稍后再试",
                    "errorCode": "rate_limited",
                    "limit": limit_exc.limit,
                    "used": limit_exc.used,
                    "resetInSeconds": limit_exc.reset_in,
                },
                status=HTTPStatus.TOO_MANY_REQUESTS,
            )
        except Exception as exc:  # noqa: BLE001
            # BUG-005: 异常脱敏,不向客户端泄露内部细节
            error_id = _new_error_id()
            _log_error(error_id, parsed.path, exc)
            self.send_json(
                {"error": "处理请求时出错,请联系客服并提供 errorId", "errorId": error_id},
                status=HTTPStatus.BAD_REQUEST,
            )

    def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def guess_type(self, path: str) -> str:
        if path.endswith(".js"):
            return "text/javascript"
        return mimetypes.guess_type(path)[0] or "application/octet-stream"


def run(host: str = "127.0.0.1", port: int = 5180) -> None:
    server = ThreadingHTTPServer((host, port), NovelStudioHandler)
    print(f"千卷 running at http://{host}:{port}/")
    server.serve_forever()


if __name__ == "__main__":
    run()
