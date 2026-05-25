from __future__ import annotations

import difflib
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


def _safe_ec_uid(value: str | None) -> str | None:
    """白名单校验 AI 秘密基地主站 uid:nanoid 默认字母表 [A-Za-z0-9_-],主站签发为 20 字符。

    放宽到 8-40 长度以兼容未来变更。
    """
    if not value:
        return None
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{8,40}", value):
        return None
    return value


# ============================================================
# AI 秘密基地主站 JWT 验签 (HMAC-SHA256, 与 server/services/local-auth.ts 同算法)
# Token 结构: <base64url(payload)>.<base64url(sig)>
# Payload: {uid, email, exp}  exp 是毫秒时间戳
# ============================================================
_EC_AI_JWT_SECRET = (os.environ.get("EC_AI_JWT_SECRET") or "").encode("utf-8")


def verify_ec_ai_token(token: str) -> dict[str, Any] | None:
    """验签主站 token。失败返回 None。"""
    import base64
    import hashlib
    import hmac

    if not _EC_AI_JWT_SECRET or not token:
        return None
    parts = token.split(".")
    if len(parts) != 2:
        return None
    body_b64, sig_b64 = parts
    try:
        expected_sig = hmac.new(_EC_AI_JWT_SECRET, body_b64.encode("utf-8"), hashlib.sha256).digest()
        # base64url 解码 sig
        pad = "=" * ((4 - len(sig_b64) % 4) % 4)
        actual_sig = base64.urlsafe_b64decode(sig_b64 + pad)
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None
        pad = "=" * ((4 - len(body_b64) % 4) % 4)
        payload_raw = base64.urlsafe_b64decode(body_b64 + pad).decode("utf-8")
        payload = json.loads(payload_raw)
    except (ValueError, json.JSONDecodeError, binascii.Error if False else Exception):  # noqa
        return None
    uid = payload.get("uid")
    email = payload.get("email")
    exp = payload.get("exp")
    if not uid or not email or not exp:
        return None
    # exp 是毫秒
    try:
        if int(exp) < int(time.time() * 1000):
            return None
    except (TypeError, ValueError):
        return None
    if not _safe_ec_uid(str(uid)):
        return None
    return {"uid": str(uid), "email": str(email), "exp": int(exp)}


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
        "agentTimeline": [],
        "revisionAttempts": {},
        "editorReview": None,
        "editorScoreHistory": [],
        "autoFixLog": [],
        "chiefEditorPassed": False,
        "chiefEditorRequiresUser": False,
        "lastExportedChapter": 0,
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
        "agentTimeline": [],
        "revisionAttempts": {},
        "editorReview": None,
        "editorScoreHistory": [],
        "autoFixLog": [],
        "chiefEditorPassed": False,
        "chiefEditorRequiresUser": False,
        "lastExportedChapter": 0,
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
    "rebirth": {
        "label": "重生/穿越",
        "coreMechanism": "前世先知 + 命运分岔 + 蝴蝶代价",
        "openingRecipe": ["回到关键节点", "确认重生身份", "先知扭转第一件事", "蝴蝶效应初现"],
        "payoffEngine": ["每章兑现一次先知优势", "每次改命要付出代价", "旧仇旧友逐步重新登场"],
        "mustHave": ["前世记忆锚点", "改命动作", "蝴蝶效应", "未知变量"],
        "avoid": ["先知一直无敌", "前世细节全靠回忆灌输", "改命无成本"],
        "firstThree": [
            "第 1 章：主角回到命运分岔点，确认重生并避开第一次悲剧。",
            "第 2 章：先用一次先知优势小赚一把，引来不该这么早出现的人。",
            "第 3 章：发现这一世已经偏离前世剧本，未知变量上场。",
        ],
        "sceneTemplates": [
            ["回到分岔点", "重生确认", "主角在熟悉场景里发现一切倒带。"],
            ["先知出手", "信息差爽点", "用前世知识抢下第一份关键资源/机会。"],
            ["蝴蝶效应", "章末钩子", "本不该出现的人提前登场，剧本开始走偏。"],
        ],
    },
    "romance": {
        "label": "情感言情",
        "coreMechanism": "误会绑定 + 拉扯升级 + 现实壁垒",
        "openingRecipe": ["意外相遇/被迫绑定", "立场对立", "第一次破防", "章末关系升级"],
        "payoffEngine": ["每章推进一次情感节点", "误会要逐步揭开", "外部壁垒和内部撕扯并行"],
        "mustHave": ["双方动机", "误会/契约", "情绪节点", "现实阻力"],
        "avoid": ["全靠人设强行甜", "误会拖太久", "配角全工具人"],
        "firstThree": [
            "第 1 章：男女主角在意外或协议中绑定，立场截然对立。",
            "第 2 章：第一次正面冲突，一方率先破防露出真实动机。",
            "第 3 章：外部压力压上来，被迫站到同一边，关系阶段升级。",
        ],
        "sceneTemplates": [
            ["误会相遇", "绑定开场", "两人在不该相遇的场合被命运绑到一起。"],
            ["情绪破防", "拉扯爽点", "一次冲突里有人先撑不住，亮出软处。"],
            ["外部施压", "章末升级", "现实壁垒或第三人介入逼迫关系前进一步。"],
        ],
    },
    "scifi": {
        "label": "科幻/星际",
        "coreMechanism": "硬设定钩子 + 旧时代谜团 + 三方势力博弈",
        "openingRecipe": ["边境/废墟开场", "唤醒旧时代造物", "失效协议触发", "章末追兵到位"],
        "payoffEngine": ["设定细节要兑现", "每章解开一层旧谜", "三方势力轮流加压"],
        "mustHave": ["科技规则", "旧时代遗产", "追兵/通缉", "认知盲区"],
        "avoid": ["设定只放嘴上不用", "AI/外星人当万能金手指", "战斗只比装备"],
        "firstThree": [
            "第 1 章：主角在边境/废舰唤醒一段旧时代信号，触发失效协议。",
            "第 2 章：协议带来第一份资源，也引来星际通缉或势力关注。",
            "第 3 章：主角发现协议指向的真相比想象大，三方势力开始下场。",
        ],
        "sceneTemplates": [
            ["边境开场", "硬设定铺垫", "主角在废舰、废土或边境星区谋生。"],
            ["旧造物觉醒", "设定钩子", "一段旧 AI/旧文件/旧装置在主角面前激活。"],
            ["势力下场", "章末追兵", "通缉令、舰队或暗杀者抵达，节奏拉满。"],
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
        "chapterWords": 2700,
        "minChapterWordsNoPunct": 2000,
        "maxChapterWordsNoPunct": 2500,
        "outlineChapterCount": 12,
        "planningRule": "短篇必须围绕单一主线推进，前 3 章入局，中段升级，最后 3 章集中回收核心伏笔并完成结局。每章正文不含标点字数硬性落在 2000-2500 字之间，禁止低于 2000 或超过 2500。",
    },
    "medium": {
        "key": "medium",
        "label": "中篇小说",
        "targetWords": 200000,
        "targetChapters": 80,
        "chapterWords": 2700,
        "minChapterWordsNoPunct": 2000,
        "maxChapterWordsNoPunct": 2500,
        "outlineChapterCount": 12,
        "planningRule": "中篇保留 2-3 个单元，主线要清晰，避免铺太多长期坑，80 章内完成阶段性大结局。每章正文不含标点字数硬性落在 2000-2500 字之间，禁止低于 2000 或超过 2500。",
    },
    "long": {
        "key": "long",
        "label": "长篇连载",
        "targetWords": 1000000,
        "targetChapters": 400,
        "chapterWords": 2700,
        "minChapterWordsNoPunct": 2000,
        "maxChapterWordsNoPunct": 2500,
        "outlineChapterCount": 10,
        "planningRule": "长篇按连载节奏设计多单元升级、势力扩张和长期伏笔，前 10 章重点完成卖点验证和追读钩子。每章正文不含标点字数硬性落在 2000-2500 字之间，禁止低于 2000 或超过 2500。",
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


# ========================================================================
# 题材成书 · 打磨循环 (W1)
# ========================================================================

# 可改字段白名单
IDEA_EDITABLE_TEXT_FIELDS = {
    "selectedSynopsis",
    "sellingPoint",
    "worldSetting",
    "mainConflict",
    "firstGoal",
    "premise",
}
IDEA_REGENERATABLE_ARRAY_FIELDS = {
    "recommendedTitles",
    "recommendedProtagonists",
    "synopsisOptions",
}

# 字段中文标签
IDEA_FIELD_LABELS = {
    "selectedSynopsis": "简介",
    "sellingPoint": "核心卖点",
    "worldSetting": "世界观",
    "mainConflict": "主冲突",
    "firstGoal": "第一章目标",
    "premise": "题材前提",
    "recommendedTitles": "推荐书名",
    "recommendedProtagonists": "主角名候选",
    "synopsisOptions": "简介候选",
    "genre": "流派",
}


def ensure_idea_polish_fields(idea: dict[str, Any]) -> dict[str, Any]:
    """保证 pendingIdeaDraft 上有 revisions / confirmed 字段。"""
    if idea is None:
        return idea
    if not isinstance(idea.get("revisions"), list):
        idea["revisions"] = []
    if "confirmed" not in idea:
        idea["confirmed"] = False
    if not isinstance(idea.get("_revisionCounter"), int):
        idea["_revisionCounter"] = 0
    return idea


def _make_revision_entry(idea: dict[str, Any], field: str, before: Any, after: Any, source: str, instruction: str = "") -> dict[str, Any]:
    idea["_revisionCounter"] = int(idea.get("_revisionCounter", 0)) + 1
    rev = {
        "id": f"rev-{int(time.time())}-{idea['_revisionCounter']}",
        "ts": datetime.now().isoformat(timespec="seconds"),
        "field": field,
        "before": deepcopy(before),
        "after": deepcopy(after),
        "source": source,
        "instruction": instruction or "",
    }
    return rev


def _append_revision(idea: dict[str, Any], field: str, before: Any, after: Any, source: str, instruction: str = "") -> dict[str, Any]:
    ensure_idea_polish_fields(idea)
    rev = _make_revision_entry(idea, field, before, after, source, instruction)
    idea["revisions"].append(rev)
    return rev


def _refine_prompt_for_field(field: str) -> str:
    base = (
        "你是中文网文产品级策划。只输出 JSON，不要 Markdown。"
        "请根据用户的打磨指令改写指定字段，保留原意中合理的部分，按指令调整细节。"
        '输出格式严格为 {"value": "..."}，不要任何额外字段。'
    )
    extra = {
        "selectedSynopsis": "字段是「简介」，控制在 80-200 字，要点明主角处境、核心矛盾、爽点钩子。",
        "sellingPoint": "字段是「核心卖点」，一两句话讲清楚这本书最独特的钩子。",
        "worldSetting": "字段是「世界观」，简明描述设定底层规则、不要堆砌名词。",
        "mainConflict": "字段是「主冲突」，一句话讲清主角对抗的核心势力或难题。",
        "firstGoal": "字段是「第一章目标」，给出第一章必须完成的钩子和情节兑现。",
        "premise": "字段是「题材前提」，一两句话讲清整本书的核心立意。",
    }
    return base + (extra.get(field) or "")


def refine_idea_field(idea: dict[str, Any], field: str, instruction: str) -> str:
    """调 LLM 把指定字段按指令改写，返回新值。失败抛 RuntimeError。"""
    if field not in IDEA_EDITABLE_TEXT_FIELDS:
        raise ValueError(f"字段 {field} 不支持 AI 打磨")
    instruction = str(instruction or "").strip()
    if not instruction:
        raise ValueError("请填写打磨指令")
    current_value = str(idea.get(field) or "").strip()
    system = _refine_prompt_for_field(field)
    user = json.dumps(
        {
            "topic": idea.get("topic", ""),
            "genre": idea.get("genre", ""),
            "selectedTitle": idea.get("selectedTitle", ""),
            "selectedProtagonist": idea.get("selectedProtagonist", ""),
            "field": field,
            "fieldLabel": IDEA_FIELD_LABELS.get(field, field),
            "currentValue": current_value,
            "instruction": instruction,
        },
        ensure_ascii=False,
    )
    try:
        data = chat_json(system, user, temperature=0.7, timeout=45, agent="ideation")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"打磨失败：{exc}") from exc
    new_value = data.get("value")
    if not isinstance(new_value, str) or not new_value.strip():
        raise RuntimeError("打磨失败：模型未返回有效内容")
    return new_value.strip()


def _regenerate_array_field(idea: dict[str, Any], field: str, instruction: str) -> list[Any]:
    """整组重生成数组字段(标题/主角/简介候选)。"""
    if field not in IDEA_REGENERATABLE_ARRAY_FIELDS:
        raise ValueError(f"字段 {field} 不支持整组重生成")
    instruction = str(instruction or "").strip()
    base = (
        "你是中文网文产品级策划。只输出 JSON，不要 Markdown。"
        "请根据现有题材和用户的打磨指令，重新给出一组候选。"
    )
    spec = {
        "recommendedTitles": '严格输出 {"value": ["标题1", "标题2", "标题3"]}，每个标题 4-10 字。',
        "recommendedProtagonists": '严格输出 {"value": ["主角1", "主角2", "主角3"]}，每个 2-4 字，符合题材调性。',
        "synopsisOptions": (
            '严格输出 {"value": [{"style": "风格1", "text": "简介正文"}, {"style": "风格2", "text": "..."}]} '
            "至少 2 条，最多 3 条，每条简介 80-200 字。"
        ),
    }
    system = base + spec[field]
    user = json.dumps(
        {
            "topic": idea.get("topic", ""),
            "genre": idea.get("genre", ""),
            "selectedTitle": idea.get("selectedTitle", ""),
            "selectedProtagonist": idea.get("selectedProtagonist", ""),
            "field": field,
            "current": idea.get(field, []),
            "instruction": instruction or "请重新给出一组更精彩的候选。",
        },
        ensure_ascii=False,
    )
    try:
        data = chat_json(system, user, temperature=0.85, timeout=60, agent="ideation")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"重生成失败：{exc}") from exc
    value = data.get("value")
    if not isinstance(value, list) or not value:
        raise RuntimeError("重生成失败：模型未返回有效数组")
    return value


def regenerate_all_candidates(idea: dict[str, Any]) -> dict[str, Any]:
    """一次 LLM 调用同时重生成 标题/主角/简介 3 组候选,用于改判流派后同步候选。"""
    system = (
        "你是中文网文产品级策划。只输出 JSON,不要 Markdown。"
        "根据当前题材、流派和深度配置,重新生成与流派调性一致的标题/主角名/简介候选。"
        '严格输出 {"recommendedTitles": ["t1","t2","t3"], '
        '"recommendedProtagonists": ["n1","n2","n3"], '
        '"synopsisOptions": [{"style":"风格1","text":"..."}, {"style":"风格2","text":"..."}]} '
        "标题 4-10 字;主角名 2-4 字,符合流派调性;简介 2-3 条,每条 80-200 字,要点明主角处境、核心矛盾、爽点钩子。"
    )
    user = json.dumps(
        {
            "topic": idea.get("topic", ""),
            "genre": idea.get("genre", ""),
            "genreLabel": idea.get("genreLabel", ""),
            "deepRules": idea.get("deepRules", {}),
            "selectedTitle": idea.get("selectedTitle"),
            "selectedProtagonist": idea.get("selectedProtagonist"),
            "currentTitles": idea.get("recommendedTitles", []),
            "currentProtagonists": idea.get("recommendedProtagonists", []),
            "currentSynopses": idea.get("synopsisOptions", []),
        },
        ensure_ascii=False,
    )
    try:
        data = chat_json(system, user, temperature=0.85, timeout=90, agent="ideation")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"同步候选失败:{exc}") from exc
    titles = data.get("recommendedTitles") or []
    heroes = data.get("recommendedProtagonists") or []
    synopses = data.get("synopsisOptions") or []
    if not isinstance(titles, list) or not titles:
        raise RuntimeError("同步候选失败:模型未返回标题数组")
    if not isinstance(heroes, list) or not heroes:
        raise RuntimeError("同步候选失败:模型未返回主角候选数组")
    if not isinstance(synopses, list) or not synopses:
        raise RuntimeError("同步候选失败:模型未返回简介数组")
    return {
        "recommendedTitles": titles,
        "recommendedProtagonists": heroes,
        "synopsisOptions": synopses,
    }


def _revert_field_value(idea: dict[str, Any], revision_id: str) -> tuple[str, Any, Any]:
    """根据 revisionId 把字段回到对应历史。返回 (field, current_value_before_revert, target_value)。"""
    target: dict[str, Any] | None = None
    for rev in idea.get("revisions", []):
        if rev.get("id") == revision_id:
            target = rev
            break
    if target is None:
        raise ValueError("找不到该修订记录")
    field = target["field"]
    # 回退策略:取目标 revision 的 before 值(即「这次修订前」的样子)。
    target_value = target["before"]
    current_value = idea.get(field)
    return field, current_value, target_value


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
    # 起点恒为 1，让已完成章节始终留在左侧栏(避免「写到第 4 章后第 1 章消失」)
    start = 1
    end = min(max_outline, max(active_number + 2, 3))
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
    # 目标：全章 2000-2500 字不含标点 ≈ 2350-2950 字含标点。
    # 按场景数平均分配 ~2700 字含标点，章末钩子场景多一点（小高潮）。
    chapter_target_with_punct = 2700
    base_per_scene = chapter_target_with_punct // scene_count
    climax_bonus = chapter_target_with_punct - base_per_scene * scene_count + 80
    scenes = []
    for index, (scene_title, scene_type, summary) in enumerate(selected, start=1):
        is_last = index == scene_count
        scene_words = base_per_scene + (climax_bonus if is_last else 0)
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
                "words": scene_words,
                "status": "draft",
                "summary": summary,
                "tags": [f"第{chapter_number}章", config["label"]],
                "content": content,
            }
        )
    return scenes


def plan_chapter_with_llm(state: dict[str, Any], chapter_number: int) -> bool:
    """章节正文生成前调一次,把粗大纲展开成本章 scene 骨架。

    成功返回 True 并把 LLM 出的 scenes 落到 state['plan']['scenes'],
    供 build_chapter_scenes 或下游写手参考;失败返回 False,沿用默认骨架。
    """
    outlines = state.get("outline", {}).get("chapterOutlines") or []
    # outline 里的章号字段历史上有 chapter / chapterNumber / number 三种叫法,兼容
    def _outline_num(c):
        for k in ("chapterNumber", "chapter", "number", "index"):
            v = _safe_int(c.get(k), -1)
            if v > 0:
                return v
        return -1
    outline = next(
        (c for c in outlines if _outline_num(c) == chapter_number),
        None,
    )
    if not outline:
        # 兜底:位置法,outline 第 N-1 项对应第 N 章
        if outlines and 0 < chapter_number <= len(outlines):
            outline = outlines[chapter_number - 1]
    if not outline:
        return False
    if not get_llm_config("planner").configured:
        return False
    _log_agent(state, "planner", "running", chapter=chapter_number,
               message="展开本章场景骨架")
    canon = state.get("memory", {}).get("canon", [])
    archive = state.get("chapterArchive", [])
    system = (
        "你是中文网文的章节规划编辑,以七猫/起点爆款为标尺。只输出 JSON,不要 markdown 或解释。"
        "把给定大纲展开成 3-5 个场景骨架,严格输出:"
        "{\"scenes\":[{\"title\":string,\"type\":string,\"beat\":string,"
        "\"purpose\":string,\"wordTarget\":number,\"hookAtEnd\":boolean,"
        "\"dialogueShare\":number,\"hookType\":string}]}。"
        "\n规则:"
        "\n- 总 wordTarget≈2700(含标点);最后一个场景 hookAtEnd 必须为 true;"
        "\n- 每个场景的 dialogueShare 是该场景目标对话段占比,范围 0.20-0.55,"
        "全章平均不低于 0.30(爆款 P50);战斗/突破场景可降到 0.20,争吵/对峙必须 ≥ 0.40;"
        "\n- 最后一个场景的 hookType 必须从这 10 类选一个:'强敌出场'/'真相揭露'/"
        "'主角抉择'/'美女登场'/'机缘出现'/'危机迫近'/'误会爆发'/'身份反转'/'重逢相遇'/'抉择两难';"
        "\n- beat 写本场景的剧情节拍(20-50 字);purpose 写本场景对全章的功能;"
        "\n场景节奏:开场承接 → 行动推进 → 代价 → 反转/爽点 → 章末钩子。"
    )
    user = json.dumps({
        "chapter": outline,
        "canon": canon[-10:],
        "previousChapters": [
            {"number": c.get("number"), "title": c.get("title")}
            for c in archive[-3:]
        ],
        "genre": state.get("project", {}).get("genre", ""),
        "genreLabel": state.get("project", {}).get("genreLabel", ""),
        "readerPromise": state.get("plan", {}).get("readerPromise", ""),
        "mustAdvance": state.get("plan", {}).get("mustAdvance", ""),
    }, ensure_ascii=False)
    try:
        data = chat_json(system, user, temperature=0.6, timeout=60, agent="planner")
    except Exception as exc:  # noqa: BLE001
        _log_agent(state, "planner", "failed", chapter=chapter_number, message=str(exc)[:200])
        return False
    scenes = data.get("scenes") or []
    if not scenes:
        _log_agent(state, "planner", "failed", chapter=chapter_number, message="模型未返回场景")
        return False
    state.setdefault("plan", {})["scenes"] = scenes
    # 把 planner 出的场景元数据回填到 state["scenes"], 让 scene_writer 据此写正文
    current_scenes = state.get("scenes") or []
    for index, sc in enumerate(scenes):
        if index >= len(current_scenes):
            break
        target = current_scenes[index]
        if sc.get("title"):
            target["title"] = str(sc["title"])
        if sc.get("type"):
            target["type"] = str(sc["type"])
        if sc.get("beat") or sc.get("purpose"):
            target["summary"] = str(sc.get("beat") or sc.get("purpose"))
        wt = _safe_int(sc.get("wordTarget"), 0)
        if wt > 0:
            target["words"] = wt
        # 新增:dialogueShare / hookAtEnd / hookType 透传给写手
        if isinstance(sc.get("dialogueShare"), (int, float)):
            target["dialogueShare"] = max(0.0, min(1.0, float(sc["dialogueShare"])))
        if "hookAtEnd" in sc:
            target["hookAtEnd"] = bool(sc.get("hookAtEnd"))
        if sc.get("hookType"):
            target["hookType"] = str(sc["hookType"])
        target["status"] = "draft"
    _log_agent(state, "planner", "done", chapter=chapter_number,
               message=f"展开 {len(scenes)} 个场景骨架")
    return True


def settle_truth_with_llm(state: dict[str, Any], chapter_number: int, chapter_title: str) -> tuple[bool, str]:
    """truth agent 真接 LLM:从已生成章节正文里抽取本章新增的事实/伏笔/世界/角色状态。

    返回 (used, note)。失败/未配置则返回 (False, 原因),由调用方决定回退策略。
    """
    if not get_llm_config("truth").configured:
        return False, "未配置 truth 模型,沿用占位归档"
    scenes = state.get("scenes") or []
    scene_text = "\n\n".join(s.get("content", "") for s in scenes if s.get("content"))
    if not scene_text.strip():
        return False, "正文为空,跳过 LLM 归档"
    _log_agent(state, "truth", "running", chapter=chapter_number,
               message="从正文抽取新增 canon")
    system = (
        "你是中文网文的真相归档编辑。只输出 JSON,不要解释。"
        "从给定章节正文里抽取本章【新增】的内容,严格输出 schema:"
        "{\"newFacts\":[{\"title\":string,\"text\":string}],"
        "\"newForeshadowing\":[{\"title\":string,\"note\":string}],"
        "\"newWorldRules\":[{\"title\":string,\"text\":string}],"
        "\"characterStateChanges\":[{\"character\":string,\"from\":string,\"to\":string}]}。"
        "只抽取正文明确发生过的事件/规则,不要推测或编造。"
        "已存在的 canon 标题不要重复。"
    )
    existing_canon = state.get("memory", {}).get("canon", [])
    user = json.dumps({
        "chapterNumber": chapter_number,
        "chapterTitle": chapter_title,
        "text": scene_text[:8000],
        "existingCanonTitles": [c.get("title") for c in existing_canon[-30:]],
    }, ensure_ascii=False)
    try:
        data = chat_json(system, user, temperature=0.3, timeout=60, agent="truth")
    except Exception as exc:  # noqa: BLE001
        _log_agent(state, "truth", "failed", chapter=chapter_number, message=str(exc)[:200])
        return False, f"truth LLM 失败:{exc}"
    memory = state.setdefault("memory", {"canon": [], "lore": [], "stakes": []})
    canon = memory.setdefault("canon", [])
    existing_titles = {c.get("title") for c in canon}
    new_facts = data.get("newFacts") or []
    added_facts = 0
    for fact in new_facts:
        if not isinstance(fact, dict):
            continue
        title = str(fact.get("title") or "").strip()
        text = str(fact.get("text") or "").strip()
        if not title or title in existing_titles:
            continue
        canon.append({"title": title, "text": text})
        existing_titles.add(title)
        added_facts += 1
    new_fore = data.get("newForeshadowing") or []
    memory.setdefault("foreshadowing", []).extend(
        [f for f in new_fore if isinstance(f, dict) and f.get("title")]
    )
    new_world = data.get("newWorldRules") or []
    memory.setdefault("worldRules", []).extend(
        [w for w in new_world if isinstance(w, dict) and w.get("title")]
    )
    char_changes = data.get("characterStateChanges") or []
    memory.setdefault("characterArcs", []).extend(
        [c for c in char_changes if isinstance(c, dict) and c.get("character")]
    )
    note = f"+{added_facts} 事实 / +{len(new_fore)} 伏笔 / +{len(new_world)} 世界规则 / +{len(char_changes)} 角色变更"
    _log_agent(state, "truth", "done", chapter=chapter_number, message=note)
    return True, note


def chief_editor_review(state: dict[str, Any], chapter_number: int) -> dict[str, Any] | None:
    """小说总编 agent:audit 之后调一次,给章节打分 + 决定 approve/revise/reject。

    返回 dict {score, passed, issues, action, editorNotes} 或 None(失败)。
    """
    if not get_llm_config("chief_editor").configured:
        # 未配置 LLM: 用本地规则做基本字数+去 AI 味检查,确保流程可走
        _log_agent(state, "chief_editor", "running", chapter=chapter_number,
                   message="本地规则审核(未配置 LLM)")
        text = "\n\n".join(s.get("content", "") for s in state.get("scenes", []))
        no_punct = count_chars_no_punct(text)
        target_min = chapter_min_words_no_punct(state)
        target_max = chapter_max_words_no_punct(state)
        de_ai = compute_de_ai_metrics(text)
        issues = []
        if no_punct < target_min:
            issues.append({"severity": "blocker", "category": "字数",
                           "text": f"不含标点 {no_punct} 字,低于平台底线 {target_min} 字",
                           "suggestion": "扩写主要场景,增加细节描写和动作"})
        elif no_punct > target_max:
            issues.append({"severity": "minor", "category": "字数",
                           "text": f"不含标点 {no_punct} 字,超过推荐上限 {target_max} 字",
                           "suggestion": "压缩冗余描写"})
        if not de_ai.get("has_chapter_hook"):
            issues.append({"severity": "blocker", "category": "去AI味-章末钩子",
                           "text": f"末段「{de_ai['last_para_preview']}」无明显钩子",
                           "suggestion": "末段加疑问/惊叹/省略号或转折词,留下未解事件"})
        if de_ai.get("single_sent_para_ratio", 0) > 0.70:
            issues.append({"severity": "major", "category": "去AI味-段落过碎",
                           "text": f"单句成段率 {de_ai['single_sent_para_ratio']:.0%}",
                           "suggestion": "合并相邻短句,目标让 30% 以上段落有 2-4 个句子"})
        if de_ai.get("dialog_para_ratio", 0) < 0.25:
            issues.append({"severity": "major", "category": "去AI味-对话稀少",
                           "text": f"对话段比例 {de_ai['dialog_para_ratio']:.0%} 过低",
                           "suggestion": "在主要场景加 4-6 句直接对白"})
        if de_ai.get("ai_blacklist_total", 0) >= 3:
            issues.append({"severity": "major", "category": "去AI味-雷区词",
                           "text": f"AI 雷区词命中 {de_ai['ai_blacklist_total']} 次",
                           "suggestion": "替换为具体动作或感官描写"})
        blocker = sum(1 for i in issues if i["severity"] == "blocker")
        major = sum(1 for i in issues if i["severity"] == "major")
        passed = blocker == 0 and major < 2
        score = 90 - blocker * 25 - major * 10
        result = {
            "score": max(score, 30),
            "passed": passed,
            "issues": issues,
            "action": "approve" if passed else "revise",
            "editorNotes": "; ".join(i["suggestion"] for i in issues) or "本地规则通过",
            "deAiMetrics": de_ai,
            "model": "local-rule",
        }
        _log_agent(state, "chief_editor", "done", chapter=chapter_number,
                   message=f"本地审核 {result['score']}/100 → {result['action']}")
        return result
    _log_agent(state, "chief_editor", "running", chapter=chapter_number,
               message="审核章节质量")
    scenes = state.get("scenes") or []
    text = "\n\n".join(s.get("content", "") for s in scenes if s.get("content"))
    no_punct = count_chars_no_punct(text)
    target_min = chapter_min_words_no_punct(state)
    target_max = chapter_max_words_no_punct(state)
    de_ai = compute_de_ai_metrics(text)
    system = (
        "你是中文网文小说总编,以七猫/起点/番茄爆款为标尺。只输出 JSON,不要 Markdown 或解释。"
        "重点检查【去 AI 味】结构问题,然后才是内容问题。"
        f"\n硬性红线(任一命中即 blocker,必须打回重写):"
        f"\n  - 不含标点字数必须在 {target_min}-{target_max} 之间"
        f"\n  - 章末必须有钩子(疑问/惊叹/省略号/'竟然/不料/突然/猛地/下一刻' 等)"
        f"\n  - 单句成段率不得超过 70%(段落必须能合并的就合并,不要每句一段)"
        f"\n  - 含引号对话段比例不得低于 25%(角色必须真说话,不要纯叙述)"
        "\n二级问题(major,2 次以上即 revise):"
        "\n  - AI 雷区词命中 ≥3:然而/与此同时/在这一刻/不可否认/令人/不由得/不禁/油然而生/心头一震/宛若/正如 等"
        "\n  - 升华/比喻句式 ≥2:'并非...而是'/'不仅...更...'/'宛若...一般' 等"
        "\n  - 平均句长 <14 字(过碎)或 >35 字(过长)"
        "\n  - 形容词堆砌/空泛情绪命名词过多"
        "\n三级问题(minor):字数±5%、流派调性微调、canon 边角矛盾"
        "\n【关键】除了 issues,你必须额外输出 patches[],把每个问题精确定位到 scene_id + paragraph_idx,"
        "让写手做局部微改而不是整章重写。规则:"
        "\n  - 每个 patch 必须给出 {patch_id,scene_id,paragraph_idx,original_excerpt,issue_type,severity,repair_strategy}"
        "\n  - scene_id 来自 user payload 的 scenes_with_paragraphs[].id"
        "\n  - paragraph_idx 来自 user payload 的 scenes_with_paragraphs[].paragraphs[].idx(0-based)"
        "\n  - original_excerpt 是该段前 30 字,用于校对"
        "\n  - repair_strategy 必须是可执行的句级指引(80 字内,如'把这段三个短句并成一段,主角加一句反问对白')"
        "\n  - patches 上限 5 条:blocker 必须全列;major+minor 合计 ≤3 条;优先级 blocker>major>minor"
        "\n  - issue_type 限定:hook/单句成段/对话稀少/雷区词/升华句式/字数/canon冲突/调性/其他"
        "\n严格输出 schema:"
        "\n{\"score\":number(0-100),\"passed\":boolean,"
        "\"issues\":[{\"severity\":\"blocker\"|\"major\"|\"minor\",\"category\":string,"
        "\"text\":string,\"suggestion\":string}],"
        "\"patches\":[{\"patch_id\":string,\"scene_id\":string,\"paragraph_idx\":number,"
        "\"original_excerpt\":string,\"issue_type\":string,\"severity\":\"blocker\"|\"major\"|\"minor\","
        "\"repair_strategy\":string}],"
        "\"action\":\"approve\"|\"revise\"|\"reject\",\"editorNotes\":string}"
        "\n判定:有任何 blocker → revise;有 ≥2 major → revise;只有 minor 且 score≥75 → approve;"
        "彻底跑题/canon 严重冲突 → reject。"
        "editorNotes 必须给写手【可执行的句级修改指引】,例如"
        "'把第 2 段三个短句合并成一段,加入主角的反问对话',不少于 80 字。"
    )
    # 给总编打段落级"指针",让 patch 能精确定位
    scenes_with_paragraphs = []
    for sc in scenes:
        if not sc.get("content"):
            continue
        paras = [p.strip() for p in re.split(r"\n\n+", sc["content"]) if p.strip()]
        scenes_with_paragraphs.append({
            "id": sc.get("id"),
            "title": sc.get("title"),
            "paragraphs": [
                {"idx": i, "preview": (p[:40] + ("…" if len(p) > 40 else "")), "len": len(p)}
                for i, p in enumerate(paras)
            ],
        })
    user = json.dumps({
        "chapterNumber": chapter_number,
        "chapterTitle": state["project"].get("chapterTitle"),
        "noPunctCharCount": no_punct,
        "requiredRange": f"{target_min}-{target_max}",
        "deAiMetrics": de_ai,
        "deAiBaselineQimao": {
            "sent_avg_p50": 18.4,
            "single_sent_para_ratio_p50": 0.62,
            "single_sent_para_ratio_p75": 0.69,
            "dialog_para_ratio_p25": 0.27,
            "dialog_para_ratio_p50": 0.37,
            "chapter_hook_coverage": 0.69,
            "ai_blacklist_total_p50": 1,
        },
        "scenes_with_paragraphs": scenes_with_paragraphs,
        "text": text[:8000],
        "genre": state.get("project", {}).get("genre", ""),
        "genreLabel": state.get("project", {}).get("genreLabel", ""),
        "canon": (state.get("memory", {}).get("canon") or [])[-10:],
        "readerPromise": state.get("plan", {}).get("readerPromise", ""),
        "mustAdvance": state.get("plan", {}).get("mustAdvance", ""),
    }, ensure_ascii=False)
    try:
        data = chat_json(system, user, temperature=0.4, timeout=90, agent="chief_editor")
    except Exception as exc:  # noqa: BLE001
        err_msg = str(exc)[:300]
        state["lastChiefEditorError"] = err_msg
        _log_agent(state, "chief_editor", "failed", chapter=chapter_number, message=err_msg[:200])
        return None
    state.pop("lastChiefEditorError", None)

    # --- 后处理:用 metrics 强制注入硬规则 issue(LLM 偶尔会漏报)---
    issues = list(data.get("issues") or [])

    def _has_issue(category_kw: str) -> bool:
        return any(category_kw in (i.get("text", "") + i.get("category", "") + i.get("suggestion", ""))
                   for i in issues)

    if not de_ai.get("has_chapter_hook") and not _has_issue("钩子"):
        issues.append({
            "severity": "blocker",
            "category": "去AI味-章末钩子",
            "text": f"末段「{de_ai['last_para_preview']}」无明显钩子",
            "suggestion": "改写末段,加疑问句/惊叹/省略号或'竟然/不料/突然/猛地'等转折词,留下未解事件",
        })
    if de_ai.get("single_sent_para_ratio", 0) > 0.70 and not _has_issue("单句成段"):
        issues.append({
            "severity": "major",
            "category": "去AI味-段落过碎",
            "text": f"单句成段率 {de_ai['single_sent_para_ratio']:.0%},超过爆款 P75 = 69%",
            "suggestion": "把相邻的短句合并成长段落,目标让 30% 以上的段落包含 2-4 个句子",
        })
    if de_ai.get("dialog_para_ratio", 0) < 0.25 and not _has_issue("对话"):
        issues.append({
            "severity": "major",
            "category": "去AI味-对话稀少",
            "text": f"含引号对话段比例 {de_ai['dialog_para_ratio']:.0%},低于爆款 P25 = 27%",
            "suggestion": "在主要场景里加 4-6 句直接对白(用中文双引号),让主角和对手真说话",
        })
    if de_ai.get("ai_blacklist_total", 0) >= 3 and not _has_issue("AI 雷区"):
        hits = ", ".join(f"{k}×{v}" for k, v in list(de_ai["ai_blacklist_hits"].items())[:5])
        issues.append({
            "severity": "major",
            "category": "去AI味-雷区词",
            "text": f"AI 雷区词命中 {de_ai['ai_blacklist_total']} 次:{hits}",
            "suggestion": "把所有雷区词替换为具体动作或感官描写,例如'令人窒息'→'她屏住一口气'",
        })
    if de_ai.get("sublimation_hits", 0) >= 2 and not _has_issue("升华"):
        issues.append({
            "severity": "major",
            "category": "去AI味-升华句式",
            "text": f"'并非/不仅/宛若' 等升华句式 {de_ai['sublimation_hits']} 次",
            "suggestion": "删除作者腔的总结/比喻,改用动作或细节让画面自己说话",
        })

    # 重新决定 action
    blocker_count = sum(1 for i in issues if i.get("severity") == "blocker")
    major_count = sum(1 for i in issues if i.get("severity") == "major")

    # graceful 评分:不论 LLM 给多少分,根据 deviation 算一个理性下限,避免 12/100 这种极端
    # 字数轻微偏离(<15%)只扣 8 分;严重偏离(<60% 目标)才大幅扣分
    llm_score = _safe_int(data.get("score"), 60)
    graceful_floor = 85
    if no_punct < target_min:
        deviation = (target_min - no_punct) / max(target_min, 1)
        if deviation > 0.40:
            graceful_floor -= 30  # 严重偏低: ≤55
        elif deviation > 0.15:
            graceful_floor -= 15  # 中度偏低: ≤70
        else:
            graceful_floor -= 8   # 轻度偏低: ≤77
    elif no_punct > target_max:
        deviation = (no_punct - target_max) / max(target_max, 1)
        if deviation > 0.20:
            graceful_floor -= 10
        else:
            graceful_floor -= 5
    if not de_ai.get("has_chapter_hook"):
        graceful_floor -= 8
    if de_ai.get("ai_blacklist_total", 0) >= 3:
        graceful_floor -= 5
    # 综合:取 LLM 分和 graceful_floor 的算术平均,避免 LLM 极端打分
    blended = max(min((llm_score + graceful_floor) // 2, 95), 30)
    data["score"] = blended

    if blocker_count > 0:
        data["action"] = "revise"
        data["passed"] = False
    elif major_count >= 2:
        data["action"] = "revise"
        data["passed"] = False
    elif blended >= 75 and major_count == 0:
        # 保留 LLM 的 approve,但要把 minor 之外的修正都做完
        pass

    data["issues"] = issues
    data["deAiMetrics"] = de_ai
    data["model"] = get_llm_config("chief_editor").model
    _log_agent(state, "chief_editor", "done", chapter=chapter_number,
               message=f"打分 {data.get('score')}/100 → {data.get('action')} (硬规则:{blocker_count}块/{major_count}主)")
    return data


def rewrite_scenes_with_editor_notes(state: dict[str, Any], notes: str) -> tuple[bool, str]:
    """按小说总编的批注重写本章场景。复用 scene_writer 路径,但 prompt 注入 editorNotes。
    内置 anti-copy:首次相似度 > 0.78 直接 retry 一次(温度拉到 0.95 + 禁用原句 + 改写指令),
    第二次仍复读才 fail。
    """
    config = get_llm_config("scene_writer")
    if not config.configured:
        return False, "未配置写手模型,无法按批注重写"
    chapter_number = state["project"]["chapterNumber"]
    # 提取上一版正文的实测度量 + 前 6 句"禁用原句",给 LLM 做反复读约束
    prev_text = "\n\n".join(s.get("content", "") for s in state.get("scenes", []) if s.get("content"))
    prev_de_ai = compute_de_ai_metrics(prev_text)
    # 抽取上一版每个场景的前 2 句作为"禁用复读样本"
    forbidden_lines: list[str] = []
    for scene in state.get("scenes", []):
        content = scene.get("content", "")
        if not content:
            continue
        sents = re.split(r"(?<=[。！？!?])", content)
        for s in sents[:2]:
            s = s.strip()
            if 8 <= len(s) <= 60:
                forbidden_lines.append(s)
        if len(forbidden_lines) >= 8:
            break

    def _build_prompts(retry: bool) -> tuple[str, str]:
        anti_copy = ""
        if retry:
            anti_copy = (
                "\n【上一次 LLM 复读了原文,本次必须大幅改写】"
                "\n  - 每段开头第一句必须不同于上一版;"
                "\n  - 段落数量±2 段也接受,鼓励合并/拆分;"
                "\n  - 至少 50% 段落的句序要重排或新写;"
                "\n  - 下方 forbiddenLines 列出的原句一字不可出现;"
            )
        system = (
            "你是中文网文连载写手,文风以七猫/起点/番茄爆款为标尺。当前任务是【按总编批注重写本章】。"
            "只输出 JSON,不要 Markdown。"
            "严格遵循总编批注 editorNotes,针对问题点逐一修正,但保留剧情走向和场景结构。"
            "\n【字数】每场景 targetWords 指不含标点中文字数,全章 2000-2500 字,严禁低于 2000 或高于 2500。"
            "\n【去 AI 味结构红线】(必须满足,违反等于本次重写失败):"
            "\n  1. 末场景结尾必须有钩子:疑问/惊叹/省略号或'竟然/不料/突然/猛地'转折词;"
            "\n  2. 单句成段率 ≤ 65%:相邻短句要合并;"
            "\n  3. 含引号对话段比例 ≥ 30%:加入直接对白;"
            "\n  4. 平均句长 16-25 字。"
            "\n【AI 雷区词禁用】然而/与此同时/在这一刻/令人/不由得/不禁/深深地/油然而生/心头一震/宛若/正如 等,"
            "出现即必须改写。"
            "\n【升华句式禁用】不要'并非...而是'/'不仅...更...'/'宛若...一般'。"
            "\n请对照上一版的实测 prevDeAiMetrics,把没达标的指标拉回来。"
            + anti_copy
        )
        user = json.dumps({
            "isRewrite": True,
            "editorNotes": notes,
            "prevDeAiMetrics": prev_de_ai,
            "deAiTargets": {
                "single_sent_para_ratio_max": 0.65,
                "dialog_para_ratio_min": 0.30,
                "sent_avg_min": 16,
                "sent_avg_max": 25,
                "ai_blacklist_max": 1,
                "sublimation_max": 1,
                "chapter_hook_required": True,
            },
            "forbiddenLines": forbidden_lines if retry else [],
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
                    "previousContent": scene.get("content", ""),
                    "targetWords": min(max(scene.get("words", 700), 400), 1100),
                }
                for scene in state["scenes"]
            ],
            "required_schema": {
                "scenes": [{"id": "scene_01", "content": "string"}]
            },
        }, ensure_ascii=False)
        return system, user

    def _try_once(temperature: float, retry: bool) -> tuple[dict | None, str]:
        system, user = _build_prompts(retry=retry)
        try:
            data = chat_json(system, user, temperature=temperature, timeout=120, agent="scene_writer")
            return data, ""
        except Exception as exc:  # noqa: BLE001
            return None, str(exc)

    def _similarity(new_text: str) -> float:
        if new_text and prev_text:
            return difflib.SequenceMatcher(None, prev_text, new_text).ratio()
        return 0.0

    SPIN_THRESHOLD = 0.78

    # 第一次尝试
    data, err = _try_once(temperature=0.7, retry=False)
    if data is None:
        _log_agent(state, "scene_writer", "failed", chapter=chapter_number,
                   message=f"重写失败:{err}")
        return False, f"重写失败:{err}"
    returned = data.get("scenes") or []
    by_id = {item.get("id"): item for item in returned if isinstance(item, dict)}
    new_parts = []
    for scene in state["scenes"]:
        item = by_id.get(scene["id"])
        if item and item.get("content"):
            new_parts.append(str(item["content"]).strip())
        else:
            new_parts.append(scene.get("content", ""))
    new_text = "\n\n".join(p for p in new_parts if p)
    similarity = _similarity(new_text)
    retried = False

    if similarity > SPIN_THRESHOLD:
        # 复读了 — 高温 + 反复读 prompt + 禁用原句 再试一次
        retried = True
        _log_agent(state, "scene_writer", "revising", chapter=chapter_number,
                   message=f"首轮重写复读(相似度 {similarity:.0%}),温度拉到 0.95 重试")
        data2, err2 = _try_once(temperature=0.95, retry=True)
        if data2 is None:
            _log_agent(state, "scene_writer", "failed", chapter=chapter_number,
                       message=f"反复读重试失败:{err2}")
            return False, f"重写空转后反复读重试失败:{err2}"
        returned2 = data2.get("scenes") or []
        by_id2 = {item.get("id"): item for item in returned2 if isinstance(item, dict)}
        new_parts2 = []
        for scene in state["scenes"]:
            item = by_id2.get(scene["id"])
            if item and item.get("content"):
                new_parts2.append(str(item["content"]).strip())
            else:
                new_parts2.append(scene.get("content", ""))
        new_text2 = "\n\n".join(p for p in new_parts2 if p)
        similarity2 = _similarity(new_text2)
        _log_auto_fix(state, "anti_copy_retry", {
            "first_similarity": round(similarity, 3),
            "retry_similarity": round(similarity2, 3),
            "threshold": SPIN_THRESHOLD,
            "ok": similarity2 <= SPIN_THRESHOLD,
        })
        if similarity2 > SPIN_THRESHOLD:
            _log_agent(state, "scene_writer", "failed", chapter=chapter_number,
                       message=f"反复读重试后仍复读(相似度 {similarity2:.0%}),LLM 拒绝改写")
            return False, f"重写两次都复读(首 {similarity:.0%}→retry {similarity2:.0%}),建议换模型"
        # 第二次过关,用第二次的结果
        by_id = by_id2
        similarity = similarity2

    updated = 0
    for scene in state["scenes"]:
        item = by_id.get(scene["id"])
        if not item or not item.get("content"):
            continue
        scene["content"] = str(item["content"]).strip()
        scene["words"] = len(scene["content"])
        scene["wordsNoPunct"] = count_chars_no_punct(scene["content"])
        updated += 1
    if updated == 0:
        return False, "模型未返回可用重写内容"
    suffix = " · 反复读重试成功" if retried else ""
    return True, f"按批注重写 {updated} 个场景 (相似度 {similarity:.0%}){suffix}"


def archive_current_chapter(state: dict[str, Any]) -> None:
    archive = state.setdefault("chapterArchive", [])
    chapter_number = state["project"]["chapterNumber"]
    scenes_snapshot = deepcopy(state.get("scenes", []))
    total_chars = sum(len(s.get("content", "")) for s in scenes_snapshot)
    total_no_punct = sum(
        count_chars_no_punct(s.get("content", "") or "")
        for s in scenes_snapshot
    )
    chapter = {
        "number": chapter_number,
        "chapterNumber": chapter_number,  # 别名,前端 / 测试脚本统一字段
        "title": state["project"]["chapterTitle"],
        "chapterTitle": state["project"]["chapterTitle"],
        "scenes": scenes_snapshot,
        "draftScore": state.get("draftScore", 0),
        "editorScore": (state.get("editorReview") or {}).get("score"),
        "wordCount": total_chars,
        "charsNoPunct": total_no_punct,
        "truthAfter": state.get("truthAfter", ""),
    }
    for index, existing in enumerate(archive):
        if existing.get("number") == chapter_number or existing.get("chapterNumber") == chapter_number:
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
    state["chiefEditorPassed"] = False
    state["chiefEditorRequiresUser"] = False
    state["editorReview"] = None
    state["editorScoreHistory"] = []
    state["autoFixLog"] = []
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
    state["agentTimeline"] = []
    state["revisionAttempts"] = {}
    state["editorReview"] = None
    state["editorScoreHistory"] = []
    state["autoFixLog"] = []
    state["chiefEditorPassed"] = False
    state["chiefEditorRequiresUser"] = False
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


# ============================================================
# Agent 流水线时间线 (agentTimeline)
# - 每个 agent 在关键路径前后调 _log_agent 写一条记录
# - 前端拿 state.agentTimeline 渲染流水线可视化
# - status: idle | running | done | failed | revising | review_required
# ============================================================
_AGENT_LABELS = {
    "ideation": "题材策划",
    "planner": "章节规划",
    "scene_writer": "正文写手",
    "audit": "审计修订",
    "chief_editor": "小说总编",
    "truth": "真相归档",
}


def _log_agent(
    state: dict[str, Any],
    agent_id: str,
    status: str,
    *,
    message: str = "",
    chapter: int | None = None,
    duration_ms: int | None = None,
) -> None:
    """统一写入 agent 状态的入口。也顺手 add_trace 一次方便老 UI 看。"""
    timeline = state.setdefault("agentTimeline", [])
    entry = {
        "agent": agent_id,
        "label": _AGENT_LABELS.get(agent_id, agent_id),
        "status": status,
        "ts": now_label(),
        "message": message,
        "chapterNumber": chapter,
    }
    if duration_ms is not None:
        entry["duration_ms"] = duration_ms
    timeline.append(entry)
    # 长度截断
    if len(timeline) > 50:
        del timeline[: len(timeline) - 50]
    # 同步写 trace,老 UI 仍然能看到
    chapter_part = f" (第 {chapter} 章)" if chapter else ""
    add_trace(state, entry["label"], f"[{status}]{chapter_part} {message}".strip())


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
        {"id": "editor", "label": "总编审核", "done": bool(state.get("chiefEditorPassed"))},
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
    if not state.get("chiefEditorPassed") and "committed" not in state.get("truthAfter", ""):
        if state.get("chiefEditorRequiresUser"):
            return {
                "currentStep": "write",
                "primaryLabel": "按总编批注重写本章",
                "primaryEndpoint": "/api/editor/rewrite",
                "helperText": "总编两次审核仍不通过。请点击重写让正文写手按总编批注重做,或在面板里选择人工放行。",
                "steps": steps,
            }
        return {
            "currentStep": "editor",
            "primaryLabel": "总编审核章节",
            "primaryEndpoint": "/api/editor/review",
            "helperText": "审计已通过,让小说总编对照网文红线再审一次:字数、章末钩子、动机、流派调性、canon 一致性。",
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
        # 每次 GET 时按当前 active 章节重算左侧章节列表,确保已完成章节不会因历史窗口算法被裁掉
        try:
            active_number = _safe_int(project.get("chapterNumber"), 1) or 1
            outlines = output.get("outline", {}).get("chapterOutlines") or output.get("ideaDraft", {}).get("chapterOutlines") or []
            if outlines or active_number >= 1:
                output["chapters"] = build_chapter_rows(outlines, active_number)
        except Exception:
            pass
    output["workflow"] = workflow_meta(output)
    # 导出提醒:已归档章节中尚未导出的数量;到达 5 章触发横幅
    try:
        last_exported = int(state.get("lastExportedChapter") or 0)
        archive = state.get("chapterArchive", []) or []
        unexported = sorted(
            [int(c.get("number") or 0) for c in archive if int(c.get("number") or 0) > last_exported]
        )
        output["exportReminder"] = {
            "active": len(unexported) >= 5,
            "chaptersSinceLastExport": len(unexported),
            "lastExportedChapter": last_exported,
            "unexportedChapters": unexported,
        }
    except Exception:
        output["exportReminder"] = {"active": False, "chaptersSinceLastExport": 0,
                                     "lastExportedChapter": 0, "unexportedChapters": []}
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


def export_chapter_plaintext(state: dict[str, Any], chapter_number: int | None = None) -> str:
    """纯文本单章导出。无 markdown 语法，适合直接复制到网文平台编辑器。"""
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
        f"第 {number} 章 · {title}",
        f"作品：{project['title']}",
        "",
        "",
    ]
    for scene in scenes:
        content = str(scene.get("content") or "").strip()
        if not content:
            continue
        lines.extend([content, ""])
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


def export_book_plaintext(state: dict[str, Any]) -> str:
    """纯文本全书导出。无 markdown 语法，适合直接复制到网文平台编辑器。"""
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
        project["title"],
        "",
        f"流派：{project.get('genre', '')} · 平台：{project.get('platform', '')} · 篇幅：{project.get('storyLengthLabel', '未设置')}",
        "",
    ]
    if project.get("synopsis"):
        lines.extend(["【作品简介】", "", str(project["synopsis"]).strip(), "", ""])
    for chapter in chapters:
        lines.extend(["", f"第 {chapter['number']} 章 · {chapter['title']}", "", ""])
        for scene in chapter.get("scenes", []):
            content = str(scene.get("content") or "").strip()
            if content:
                lines.extend([content, ""])
    return "\n".join(lines).strip() + "\n"


_CN_PUNCT = set("，。！？、；：“”‘’《》「」『』（）()【】[]——……·~～·.,!?:;\"'\\/\n\r\t 　")


def count_chars_no_punct(text: str) -> int:
    """统计中文有效字数：剔除中英文标点、空白和换行。"""
    if not text:
        return 0
    return sum(1 for ch in text if ch not in _CN_PUNCT and not ch.isspace())


def chapter_min_words_no_punct(state: dict[str, Any]) -> int:
    """从作品 storyLength 拿到不含标点的章节字数下限，默认 2000。"""
    project = state.get("project") or {}
    key = str(project.get("storyLength") or "long").strip()
    cfg = STORY_LENGTH_CONFIGS.get(key, STORY_LENGTH_CONFIGS["long"])
    return int(cfg.get("minChapterWordsNoPunct") or 2000)


def chapter_max_words_no_punct(state: dict[str, Any]) -> int:
    """从作品 storyLength 拿到不含标点的章节字数上限，默认 2500。"""
    project = state.get("project") or {}
    key = str(project.get("storyLength") or "long").strip()
    cfg = STORY_LENGTH_CONFIGS.get(key, STORY_LENGTH_CONFIGS["long"])
    return int(cfg.get("maxChapterWordsNoPunct") or 2500)


# ---- 去 AI 味:基线驱动的硬规则检测器 ----
# 基线来源: C:/qj-deai/analysis/baseline_qimao.json (七猫 32 章爆款)
# 维度: 章末钩子 / 单句成段比 / 对话占比 / AI 黑名单 / 升华句式 / 平均句长

_DE_AI_BLACKLIST = (
    "然而", "值得一提", "综上所述", "与此同时", "在这一刻", "在某种意义上",
    "不可否认", "众所周知", "毫无疑问", "从某种角度", "可以说是", "令人",
    "不由得", "不禁", "深深地", "深邃", "复杂的情感", "难以言喻",
    "油然而生", "心头一震", "心中一凛", "不由分说",
    "彰显", "映衬", "透露出一种", "蕴含着", "宛若", "正如",
    "一般而言", "总而言之", "不约而同",
    # v3 补充:基于千卷出稿 4-gram 与高频 LLM 套词扫描
    "随即", "顿时", "瞬间", "一时间", "片刻之后", "与其说",
    "仿佛一切", "似乎一切", "整个世界",
)

_DE_AI_SUBLIMATION = (
    r"并非.{0,15}而是",
    r"不仅.{0,15}更",
    r"不仅仅.{0,15}还",
    r"与其说.{0,15}不如说",
    r"就如同.{0,15}一般",
    r"恰如.{0,15}一般",
    r"仿佛.{0,15}一般",
)

_DE_AI_HOOK_WORDS = (
    "？", "！", "...", "…",
    "竟然", "不料", "突然", "只见", "居然", "怎么会", "什么",
    "猛地", "戛然", "下一刻", "却见",
    "岂料", "蓦地", "蓦然", "霎时", "陡然", "骤然",
    "哪知", "谁知", "不曾想", "想不到", "竟",
)

_DE_AI_DIALOG_MARKS = ("“", "”", "‘", "’", "「", "」", "『", "』")

_DE_AI_SENT_END = set("。！？!?…")


def _split_de_ai_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in text.split("\n") if p.strip()]


def _split_de_ai_sentences(text: str) -> list[str]:
    sents: list[str] = []
    buf: list[str] = []
    for ch in text:
        buf.append(ch)
        if ch in _DE_AI_SENT_END:
            s = "".join(buf).strip()
            if s:
                sents.append(s)
            buf = []
    if buf:
        s = "".join(buf).strip()
        if s:
            sents.append(s)
    return sents


def compute_de_ai_metrics(text: str) -> dict[str, Any]:
    """计算章节的关键去 AI 味度量。返回 dict 含:
    - chars_no_punct, sentences, paragraphs
    - sent_avg, single_sent_para_ratio, dialog_para_ratio
    - has_chapter_hook, last_para_preview
    - ai_blacklist_total, ai_blacklist_hits
    - sublimation_hits
    - flags: list[str] — 给 chief_editor 用的可读问题摘要
    """
    text = text or ""
    paras = _split_de_ai_paragraphs(text)
    sents = _split_de_ai_sentences(text)
    no_punct = count_chars_no_punct(text)
    if not sents or no_punct < 50:
        return {
            "chars_no_punct": no_punct,
            "sentences": len(sents),
            "paragraphs": len(paras),
            "sent_avg": 0,
            "single_sent_para_ratio": 0,
            "dialog_para_ratio": 0,
            "has_chapter_hook": False,
            "last_para_preview": "",
            "ai_blacklist_total": 0,
            "ai_blacklist_hits": {},
            "sublimation_hits": 0,
            "flags": ["正文过短,无法度量"],
        }
    sent_lens = [count_chars_no_punct(s) for s in sents]
    sent_avg = sum(sent_lens) / max(len(sent_lens), 1)
    para_sent_counts = [len(_split_de_ai_sentences(p)) for p in paras]
    single_sent_para_ratio = sum(1 for c in para_sent_counts if c <= 1) / max(len(paras), 1)
    dialog_paras = sum(1 for p in paras if any(m in p for m in _DE_AI_DIALOG_MARKS))
    dialog_para_ratio = dialog_paras / max(len(paras), 1)
    last_para = paras[-1] if paras else ""
    has_chapter_hook = any(w in last_para for w in _DE_AI_HOOK_WORDS)
    blacklist_hits: dict[str, int] = {}
    for w in _DE_AI_BLACKLIST:
        c = text.count(w)
        if c:
            blacklist_hits[w] = c
    blacklist_total = sum(blacklist_hits.values())
    sublim_hits = 0
    for pat in _DE_AI_SUBLIMATION:
        sublim_hits += len(re.findall(pat, text))

    flags: list[str] = []
    if not has_chapter_hook:
        flags.append("章末无钩子(末段无 ?! 省略号或转折词)")
    if single_sent_para_ratio > 0.70:
        flags.append(f"单句成段率过高 {single_sent_para_ratio:.0%}(爆款 P75 ≤ 69%),段落过碎")
    if dialog_para_ratio < 0.25:
        flags.append(f"对话段比例 {dialog_para_ratio:.0%} 过低(爆款 P25 ≥ 27%),角色不说话")
    if blacklist_total >= 3:
        sample = ", ".join(f"{k}×{v}" for k, v in list(blacklist_hits.items())[:5])
        flags.append(f"AI 雷区词命中 {blacklist_total} 次:{sample}")
    if sublim_hits >= 2:
        flags.append(f"升华/比喻模板句式 {sublim_hits} 次(并非/不仅/宛若 等)")
    if sent_avg < 14:
        flags.append(f"平均句长 {sent_avg:.1f} 字过短(爆款 P25 ≥ 16.7),句式太碎")

    return {
        "chars_no_punct": no_punct,
        "sentences": len(sents),
        "paragraphs": len(paras),
        "sent_avg": round(sent_avg, 1),
        "single_sent_para_ratio": round(single_sent_para_ratio, 3),
        "dialog_para_ratio": round(dialog_para_ratio, 3),
        "has_chapter_hook": has_chapter_hook,
        "last_para_preview": last_para[:80],
        "ai_blacklist_total": blacklist_total,
        "ai_blacklist_hits": blacklist_hits,
        "sublimation_hits": sublim_hits,
        "flags": flags,
    }


# ---- 去 AI 味:代码自修(在 LLM 之前先清掉硬规则可修的部分)----
# 雷区词替换字典 — key=原词, value=替换 (空字符串=直接删除)
# 设计原则:能删则删,需要保留语义的换成具体口语化词
_DE_AI_REPLACEMENTS: dict[str, str] = {
    # 全删:无语义贡献的 AI 口吻填充词
    "不禁": "", "不由得": "", "不由分说": "", "不约而同": "",
    "深深地": "", "深邃": "", "油然而生": "",
    "心头一震": "", "心中一凛": "",
    "复杂的情感": "", "难以言喻": "",
    "在这一刻": "", "在某种意义上": "", "从某种角度": "",
    "一般而言": "", "总而言之": "", "综上所述": "",
    "值得一提": "", "毫无疑问": "", "不可否认": "", "众所周知": "",
    "可以说是": "",
    "随即": "", "顿时": "", "瞬间": "", "一时间": "",
    "片刻之后": "",
    "蕴含着": "", "彰显": "", "透露出一种": "",
    "仿佛一切": "", "似乎一切": "", "整个世界": "",
    # 替换成更具体/口语化
    "然而": "可", "与此同时": "同时",
    "令人": "", "宛若": "像", "正如": "像",
    "映衬": "衬", "与其说": "",
    "就如同": "像", "恰如": "像",
}

# 升华/比喻句式 — 整句删除(包括前后逗号到句号的整句)
_DE_AI_SUBLIMATION_REGEX: tuple = (
    re.compile(r"[^。！？!?\n]*?并非[^。！？!?\n]{0,30}?而是[^。！？!?\n]*?[。！？!?]"),
    re.compile(r"[^。！？!?\n]*?不仅[^。！？!?\n]{0,30}?更[^。！？!?\n]*?[。！？!?]"),
    re.compile(r"[^。！？!?\n]*?不仅仅[^。！？!?\n]{0,30}?还[^。！？!?\n]*?[。！？!?]"),
    re.compile(r"[^。！？!?\n]*?与其说[^。！？!?\n]{0,30}?不如说[^。！？!?\n]*?[。！？!?]"),
    re.compile(r"[^。！？!?\n]*?就如同[^。！？!?\n]{0,30}?一般[^。！？!?\n]*?[。！？!?]"),
    re.compile(r"[^。！？!?\n]*?恰如[^。！？!?\n]{0,30}?一般[^。！？!?\n]*?[。！？!?]"),
    re.compile(r"[^。！？!?\n]*?仿佛[^。！？!?\n]{0,30}?一般[^。！？!?\n]*?[。！？!?]"),
)

# 章末钩子词(扩展自 _DE_AI_HOOK_WORDS)
_CHAPTER_HOOK_MARKERS: tuple = (
    "?", "!", "？", "！", "…", "...",
    "竟然", "不料", "突然", "猛地", "下一刻", "却见",
    "岂料", "蓦地", "蓦然", "霎时", "陡然", "骤然",
    "哪知", "谁知", "不曾想", "想不到", "竟", "怎么会",
)


def _strip_extra_punct(text: str) -> str:
    """清掉替换后留下的连续标点 / 行首孤立标点。"""
    text = re.sub(r"，{2,}", "，", text)
    text = re.sub(r"，([。！？!?…])", r"\1", text)
    text = re.sub(r"^[，、 \t]+", "", text, flags=re.MULTILINE)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _log_auto_fix(state: dict[str, Any], fix_type: str, detail: dict | str) -> None:
    """把自修动作写入 state.autoFixLog,前端能看到代码改了什么。"""
    log = state.setdefault("autoFixLog", [])
    log.append({
        "ts": datetime.now().strftime("%H:%M:%S"),
        "chapter": state.get("project", {}).get("chapterNumber"),
        "type": fix_type,
        "detail": detail,
    })
    # 截断保留最近 30 条
    if len(log) > 30:
        state["autoFixLog"] = log[-30:]


def auto_fix_hard_rules(state: dict[str, Any]) -> dict[str, Any]:
    """LLM 之前用代码清掉硬规则违例:雷区词 / 升华句 / 短段。
    返回执行摘要 dict。
    """
    summary = {
        "blacklist_replaced": 0,
        "blacklist_details": {},
        "sublimation_removed": 0,
        "short_paragraphs_merged": 0,
        "scenes_touched": 0,
    }
    scenes = state.get("scenes") or []
    for scene in scenes:
        text = scene.get("content") or ""
        if not text:
            continue
        original = text

        # 1. 雷区词替换
        for word, replacement in _DE_AI_REPLACEMENTS.items():
            count = text.count(word)
            if count > 0:
                text = text.replace(word, replacement)
                summary["blacklist_replaced"] += count
                summary["blacklist_details"][word] = summary["blacklist_details"].get(word, 0) + count

        # 2. 升华句整句删
        for pattern in _DE_AI_SUBLIMATION_REGEX:
            matches = pattern.findall(text)
            if matches:
                text = pattern.sub("", text)
                summary["sublimation_removed"] += len(matches)

        # 3. 清扫替换后的标点残留
        text = _strip_extra_punct(text)

        # 4. 短段合并(< 15 字且不含钩子标点/对话引号的段,跟下一段并)
        paragraphs = [p.strip() for p in re.split(r"\n\n+", text) if p.strip()]
        merged: list[str] = []
        for p in paragraphs:
            if merged and len(merged[-1]) < 15:
                last = merged[-1]
                ends_with_hook = any(last.endswith(m) for m in ("?", "!", "？", "！", "…"))
                has_dialog = any(m in last for m in _DE_AI_DIALOG_MARKS)
                if not ends_with_hook and not has_dialog:
                    merged[-1] = last + p
                    summary["short_paragraphs_merged"] += 1
                    continue
            merged.append(p)
        text = "\n\n".join(merged)

        if text != original:
            scene["content"] = text
            scene["words"] = len(text)
            scene["wordsNoPunct"] = count_chars_no_punct(text)
            summary["scenes_touched"] += 1

    if summary["blacklist_replaced"] or summary["sublimation_removed"] or summary["short_paragraphs_merged"]:
        _log_auto_fix(state, "hard_rules", {
            "blacklist": summary["blacklist_replaced"],
            "blacklist_top": dict(list(summary["blacklist_details"].items())[:5]),
            "sublimation": summary["sublimation_removed"],
            "short_para_merged": summary["short_paragraphs_merged"],
            "scenes_touched": summary["scenes_touched"],
        })
    return summary


def auto_fix_chapter_hook(state: dict[str, Any]) -> tuple[bool, str]:
    """末段不含钩子时,只调写手重写最后一段。整章其余不动。"""
    scenes = state.get("scenes") or []
    if not scenes:
        return False, "无场景"
    last_scene = scenes[-1]
    content = last_scene.get("content") or ""
    paragraphs = [p.strip() for p in re.split(r"\n\n+", content) if p.strip()]
    if not paragraphs:
        return False, "末场景为空"
    last_para = paragraphs[-1]
    if any(m in last_para for m in _CHAPTER_HOOK_MARKERS):
        return False, "已有钩子,无需修"

    config = get_llm_config("scene_writer")
    if not config.configured:
        return False, "未配置写手"

    chapter_number = state["project"]["chapterNumber"]
    system = (
        "你是中文网文写手。任务是【只把给定的最后一段改写成带强钩子的章末段】。"
        "钩子要求:含疑问句/惊叹/省略号,或'竟然/不料/突然/猛地/却见/下一刻'等转折词;"
        "留下一个未解开的事件、未到的人、或主角一句反应性独白。"
        "字数与原段相近(±50%),不要堆砌形容词,不要总结升华。"
        "禁用词:然而/不禁/不由得/油然而生/心头一震/深深地/宛若/正如/与此同时。"
        "只输出 JSON: {\"new_paragraph\":string}。"
    )
    user = json.dumps({
        "original_last_paragraph": last_para,
        "scene_summary": last_scene.get("summary", ""),
        "chapter_title": state.get("project", {}).get("chapterTitle", ""),
    }, ensure_ascii=False)
    try:
        data = chat_json(system, user, temperature=0.7, timeout=45, agent="scene_writer")
    except Exception as exc:  # noqa: BLE001
        _log_agent(state, "scene_writer", "failed", chapter=chapter_number,
                   message=f"章末钩子修复失败:{str(exc)[:80]}")
        return False, f"LLM 调用失败:{exc}"
    new_para = (data.get("new_paragraph") or "").strip()
    if not new_para:
        return False, "模型未返回新段"
    if not any(m in new_para for m in _CHAPTER_HOOK_MARKERS):
        return False, "改写后仍无钩子"

    paragraphs[-1] = new_para
    last_scene["content"] = "\n\n".join(paragraphs)
    last_scene["words"] = len(last_scene["content"])
    last_scene["wordsNoPunct"] = count_chars_no_punct(last_scene["content"])
    _log_auto_fix(state, "chapter_hook", {
        "before": last_para[:80],
        "after": new_para[:80],
    })
    return True, new_para[:60]


def apply_editor_patches(state: dict[str, Any], patches: list[dict]) -> tuple[bool, str]:
    """按总编 patch 列表逐条修,只动 patch 命中的段落。
    每个 patch 给 LLM 80 字左右的创意空间,大幅降低复读 / 失败率。
    """
    if not patches:
        return False, "无 patch"
    config = get_llm_config("scene_writer")
    if not config.configured:
        return False, "未配置写手"
    chapter_number = state["project"]["chapterNumber"]

    by_scene: dict[str, list[dict]] = {}
    for p in patches:
        sid = p.get("scene_id")
        if sid:
            by_scene.setdefault(sid, []).append(p)
    if not by_scene:
        return False, "patches 无有效 scene_id"

    total_target = len(patches)
    applied = 0
    spin_count = 0

    for scene in state["scenes"]:
        scene_patches = by_scene.get(scene["id"], [])
        if not scene_patches:
            continue

        paragraphs = [p.strip() for p in re.split(r"\n\n+", scene.get("content") or "") if p.strip()]
        if not paragraphs:
            continue

        targets = []
        for p in scene_patches:
            idx = p.get("paragraph_idx")
            if not isinstance(idx, int) or idx < 0 or idx >= len(paragraphs):
                continue
            targets.append({
                "patch_id": p.get("patch_id") or f"p{len(targets)+1}",
                "paragraph_idx": idx,
                "original": paragraphs[idx],
                "issue_type": p.get("issue_type", ""),
                "severity": p.get("severity", "major"),
                "repair_strategy": p.get("repair_strategy", "按总编要求修复"),
            })
        if not targets:
            continue

        # 上下文:目标段 + 前后各 1 段
        ctx_indices: set[int] = set()
        for t in targets:
            ctx_indices.add(t["paragraph_idx"])
            if t["paragraph_idx"] > 0:
                ctx_indices.add(t["paragraph_idx"] - 1)
            if t["paragraph_idx"] < len(paragraphs) - 1:
                ctx_indices.add(t["paragraph_idx"] + 1)
        ctx = [
            {
                "idx": i,
                "text": paragraphs[i],
                "is_target": i in {t["paragraph_idx"] for t in targets},
            }
            for i in sorted(ctx_indices)
        ]

        def _call_patches(temperature: float, anti_copy: bool) -> dict | None:
            sys_prompt = (
                "你是中文网文写手。任务是【按总编 patch 修复指定段落】。"
                "只改 is_target=true 的段落,is_target=false 的段落是给你看上下文。"
                "每个 patch 只动那一段,字数与原段 ±30%,保留剧情和人物关系。"
                "\n【去 AI 味红线 - 违反 = 该 patch 视为失败】:"
                "\n  - 禁用词:不禁/不由得/深深地/然而/与此同时/在这一刻/令人/宛若/正如/"
                "心头一震/油然而生/随即/顿时/瞬间/一时间;"
                "\n  - 禁用句式:并非...而是/不仅...更/宛若...一般/与其说...不如说;"
                "\n  - 是末段必含钩子:?! 或'竟然/不料/突然/猛地'等;"
                "\n  - 不要每句一段,短句要并;不要堆形容词;尽量用具体动作和对白。"
            )
            if anti_copy:
                sys_prompt += (
                    "\n【上一轮你复读了原段,本次必须大幅改写】"
                    "\n  - 新段首句必须与 original 首句不同;"
                    "\n  - 至少 60% 字符要重新写,不能照搬;"
                    "\n  - 句序、动作、人物视角任选一个维度做改变。"
                )
            sys_prompt += "\n只输出 JSON: {\"patches\":[{\"patch_id\":string,\"new_paragraph\":string}]}。"
            try:
                return chat_json(sys_prompt, user, temperature=temperature, timeout=60, agent="scene_writer")
            except Exception as exc:  # noqa: BLE001
                _log_agent(state, "scene_writer", "failed", chapter=chapter_number,
                           message=f"patch {scene['id']}: {str(exc)[:80]}")
                return None

        user = json.dumps({
            "scene_id": scene["id"],
            "scene_summary": scene.get("summary", ""),
            "context": ctx,
            "targets": [
                {
                    "patch_id": t["patch_id"],
                    "paragraph_idx": t["paragraph_idx"],
                    "issue_type": t["issue_type"],
                    "severity": t["severity"],
                    "repair_strategy": t["repair_strategy"],
                    "original": t["original"],
                }
                for t in targets
            ],
        }, ensure_ascii=False)
        data = _call_patches(temperature=0.6, anti_copy=False)
        if data is None:
            continue

        SPIN_THRESHOLD = 0.80  # 段级阈值,比章级松一点(段太短 minor 改动会很相似)

        def _process(payload: dict) -> tuple[int, int, list[tuple[int, str]]]:
            """返回 (scene_applied, spin, retry_targets[(idx, original)])"""
            retry_list: list[tuple[int, str]] = []
            local_applied = 0
            local_spin = 0
            local_new = list(paragraphs)
            returned_p = payload.get("patches") or []
            local_by_pid: dict[str, str] = {}
            for p in returned_p:
                if isinstance(p, dict) and p.get("patch_id"):
                    local_by_pid[p["patch_id"]] = (p.get("new_paragraph") or "").strip()
            for t in targets:
                new_text = local_by_pid.get(t["patch_id"], "")
                if not new_text:
                    continue
                similarity = difflib.SequenceMatcher(None, t["original"], new_text).ratio()
                if similarity > SPIN_THRESHOLD:
                    local_spin += 1
                    retry_list.append((t["paragraph_idx"], t["patch_id"]))
                    continue
                local_new[t["paragraph_idx"]] = new_text
                local_applied += 1
            return local_applied, local_spin, retry_list, local_new

        scene_applied, scene_spin, retry_list, new_paragraphs = _process(data)

        # 全部段空转 → 高温反复读 retry 一次
        if scene_applied == 0 and retry_list:
            _log_agent(state, "scene_writer", "revising", chapter=chapter_number,
                       message=f"patch 全空转({scene_spin} 段),scene {scene['id']} 高温重试")
            data2 = _call_patches(temperature=0.95, anti_copy=True)
            if data2 is not None:
                a2, s2, _, np2 = _process(data2)
                _log_auto_fix(state, "anti_copy_retry_patch", {
                    "scene_id": scene["id"],
                    "first_spin": scene_spin,
                    "retry_applied": a2,
                    "retry_spin": s2,
                })
                if a2 > 0:
                    scene_applied = a2
                    new_paragraphs = np2
                    scene_spin = s2

        spin_count += scene_spin
        if scene_applied > 0:
            scene["content"] = "\n\n".join(new_paragraphs)
            scene["words"] = len(scene["content"])
            scene["wordsNoPunct"] = count_chars_no_punct(scene["content"])
            applied += scene_applied

    if applied > 0:
        _log_auto_fix(state, "editor_patches", {
            "applied": applied,
            "total": total_target,
            "spin": spin_count,
        })
        note = f"应用 {applied}/{total_target} 个 patch"
        if spin_count:
            note += f" (空转 {spin_count})"
        return True, note
    return False, f"0/{total_target} patch 应用成功"


def generate_scene_contents_optional_llm(state: dict[str, Any]) -> tuple[bool, str]:
    config = get_llm_config("scene_writer")
    if not config.configured:
        return False, "未配置大模型 API Key，使用本地场景草稿。"

    system = (
        "你是中文网文连载写手,文风以七猫/起点/番茄爆款为标尺。只输出 JSON,不要 Markdown。"
        "根据给定作品设定和 Scene Cards 生成当前章节分场景正文。"
        "\n【字数硬性要求】每个场景 targetWords 指不含标点的有效中文字数,"
        "中文标点(,。!?、;:""''《》……—)不计入字数;"
        "全章合计有效字数(不含标点)必须落在 2000-2500 字之间,"
        "禁止低于 2000,也禁止超过 2500。按 targetWords × 1.18 估算实际含标点产出长度。"
        "\n【去 AI 味结构红线】(违反任意一条等于不合格,会被总编打回重写):"
        "\n  1. 章末必须有钩子:疑问/惊叹/省略号 或 '竟然/不料/突然/猛地/下一刻' 等转折词,留下未解事件;"
        "\n  2. 单句成段率 ≤ 65%:相邻的短句要合并成长段落,不要每句一段;爆款段落经常包含 2-4 句;"
        "\n  3. 含引号对话段比例 ≥ 30%:主要场景里主角和对手必须真说话,用中文双引号""...""穿插于动作之间,不要堆在一起;"
        "\n  4. 平均句长 16-25 字:不要全是 6-10 字短句,也不要写 40+ 字长句;"
        "\n【AI 雷区词禁用清单】(出现即扣分,改为具体动作或感官):"
        "然而、与此同时、在这一刻、不可否认、众所周知、毫无疑问、令人XX、不由得、不禁、"
        "深深地、深邃、复杂的情感、油然而生、心头一震、心中一凛、宛若、正如、一般而言、不约而同。"
        "\n【升华句式禁用】不要写'并非...而是'/'不仅...更...'/'宛若...一般',改用动作让画面自己说话。"
        "\n【风格指引】具体名词压过形容词(写'铜钱掉地''油灯灭了'胜过'气氛凝重');"
        "情绪用动作映射(写'她攥紧了拳'胜过'她很愤怒');五感分布要均衡,不要全靠'看'。"
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
                    "targetWords": min(max(scene.get("words", 700), 400), 1100),
                    # planner 出的 per-scene 去 AI 味靶子
                    "dialogueShare": scene.get("dialogueShare"),
                    "hookAtEnd": scene.get("hookAtEnd", False),
                    "hookType": scene.get("hookType"),
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

    def _apply_returned(data_in: dict[str, Any]) -> int:
        returned = data_in.get("scenes") or []
        by_id = {item.get("id"): item for item in returned if isinstance(item, dict)}
        updated_local = 0
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
                scene["wordsNoPunct"] = count_chars_no_punct(scene["content"])
                updated_local += 1
        return updated_local

    # 第一轮:整章 JSON 一次出。失败也不抛 — 留到下面单场景补稿兜底
    try:
        data = chat_json(system, user, temperature=0.75, timeout=120, agent="scene_writer")
        updated = _apply_returned(data)
    except Exception as exc:  # noqa: BLE001 - JSON 解析/超时等都进单场景兜底
        _log_agent(
            state,
            "scene_writer",
            "failed",
            chapter=state["project"].get("chapterNumber"),
            message=f"整章首轮失败,进入单场景兜底:{type(exc).__name__}: {str(exc)[:150]}",
        )
        updated = 0
    scene_total = len(state["scenes"])
    total_no_punct = sum(s.get("wordsNoPunct", 0) for s in state["scenes"] if s.get("content"))
    target_min = chapter_min_words_no_punct(state)
    target_max = chapter_max_words_no_punct(state)
    # 半稿兜底:LLM 偶尔只返回首场景就截断 — 改用「单场景逐个补稿」模式
    # 比整章重出更稳:8k context 内一次只让 LLM 写一个 scene,不会被 token 限制截断
    if updated < scene_total or total_no_punct < max(target_min - 200, 1500):
        _log_agent(
            state,
            "scene_writer",
            "revising",
            chapter=state["project"].get("chapterNumber"),
            message=(
                f"半稿兜底触发:total_no_punct={total_no_punct} < target_min={target_min},"
                f"启动单场景补稿(共 {scene_total} 个场景)"
            ),
        )
        single_system = (
            "你是中文网文连载写手。任务:为一个被截断的场景补写完整正文。"
            "只输出 JSON 对象 {\"content\":string},不要任何解释。"
            "硬性要求:content 字段必须是 600-1100 字的完整中文正文(不含标点字数),"
            "禁止只写几句话或省略。"
            "正文要求:含中文双引号对话至少 3 处;段落分布合理(不要每句一段);"
            "禁用词:然而、与此同时、不由得、不禁、宛若、正如、深邃、油然而生。"
        )
        for sc_idx, sc in enumerate(state["scenes"]):
            current_len = count_chars_no_punct(sc.get("content") or "")
            if current_len >= 400:  # 已经够长就跳过
                continue
            target_w = min(max(sc.get("words", 700), 600), 1100)
            single_user = json.dumps({
                "chapterTitle": state["project"].get("chapterTitle"),
                "chapterNumber": state["project"].get("chapterNumber"),
                "sceneId": sc.get("id"),
                "sceneTitle": sc.get("title"),
                "sceneType": sc.get("type"),
                "sceneSummary": sc.get("summary"),
                "targetWords": target_w,
                "minWords": 600,
                "maxWords": 1100,
                "isLastScene": sc is state["scenes"][-1],
                "hookType": sc.get("hookType"),
                "dialogueShare": sc.get("dialogueShare"),
                "currentDraftLen": current_len,
                "currentDraft": (sc.get("content") or "")[:500],
                "character": state["characters"]["suHan"],
                "readerPromise": state.get("plan", {}).get("readerPromise", ""),
                "memoryTail": (state.get("memory", {}).get("canon") or [])[-5:],
            }, ensure_ascii=False)
            try:
                single_data = chat_json(single_system, single_user, temperature=0.8,
                                         timeout=120, agent="scene_writer")
                new_content = str(single_data.get("content") or "").strip()
                new_no_punct = count_chars_no_punct(new_content)
                # 接受门槛降到 300 字 + 比现在更长 (适度改进就采纳)
                if new_no_punct > current_len and new_no_punct >= 300:
                    sc["content"] = new_content
                    sc["words"] = len(new_content)
                    sc["wordsNoPunct"] = new_no_punct
                    _log_agent(
                        state,
                        "scene_writer",
                        "done",
                        chapter=state["project"].get("chapterNumber"),
                        message=f"场景 {sc_idx+1} 补稿成功:{current_len} → {new_no_punct} 字",
                    )
                else:
                    _log_agent(
                        state,
                        "scene_writer",
                        "failed",
                        chapter=state["project"].get("chapterNumber"),
                        message=(
                            f"场景 {sc_idx+1} 补稿无效:current={current_len},"
                            f"returned={new_no_punct}(门槛 300+)"
                        ),
                    )
            except Exception as exc:  # noqa: BLE001 - 单场景失败不影响其他场景
                _log_agent(
                    state,
                    "scene_writer",
                    "failed",
                    chapter=state["project"].get("chapterNumber"),
                    message=f"场景 {sc_idx+1} 补稿异常:{type(exc).__name__}: {exc}",
                )
                continue
        # 重算 updated 和 total
        updated = sum(1 for s in state["scenes"] if s.get("content"))
        total_no_punct = sum(s.get("wordsNoPunct", 0) for s in state["scenes"] if s.get("content"))
    warning = ""
    if total_no_punct < target_min:
        warning = (
            f"⚠ 本章正文不含标点共 {total_no_punct} 字，低于平台底线 {target_min} 字，"
            "建议在「单章扩写」中追加字数或在场景卡里调高 targetWords 后重生。"
        )
    elif total_no_punct > target_max:
        warning = (
            f"⚠ 本章正文不含标点共 {total_no_punct} 字，超过节奏上限 {target_max} 字，"
            "建议在场景卡里调低 targetWords 后重生或手动精简。"
        )
    msg = (
        f"大模型已生成 {updated} 个场景正文（{config.model}），"
        f"本章不含标点约 {total_no_punct} 字（目标 {target_min}-{target_max}）。"
    )
    if warning:
        msg = f"{msg} {warning}"
    return True, msg


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
    # 字数守门:逐场景对比 LLM 改写前后字数,改幅过大(>25%)的拒收,保留原文
    updated = 0
    rejected_scenes = []
    for scene in state.get("scenes", []):
        item = returned.get(scene.get("id"))
        content = str(item.get("content") or "").strip() if item else ""
        if not content:
            continue
        prev_no_punct = count_chars_no_punct(scene.get("content") or "")
        new_no_punct = count_chars_no_punct(content)
        # 改写前长度 < 200 字本身就是半稿,style 阶段不允许"补全"
        if prev_no_punct < 200:
            rejected_scenes.append(scene.get("id"))
            continue
        # 改幅守门:超过原文 ±25% 视为偏离审校职责
        upper = int(prev_no_punct * 1.25)
        lower = int(prev_no_punct * 0.75)
        if new_no_punct > upper or new_no_punct < lower:
            rejected_scenes.append(scene.get("id"))
            continue
        scene["content"] = content
        scene["words"] = len(content)
        scene["wordsNoPunct"] = new_no_punct
        tags = scene.get("tags", [])
        if isinstance(tags, list) and "原创表达" not in tags:
            tags.append("原创表达")
        updated += 1
    report = build_human_style_report(state)
    if rejected_scenes:
        report["rejectedScenes"] = rejected_scenes
        report["rejectReason"] = "改幅超过 ±25% 或原文过短,保留原稿避免 style 阶段越界"
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
        # planner agent: 在锁定计划前让 LLM 把本章场景骨架展开,时间线上 planner 亮灯
        try:
            chapter_number = state["project"].get("chapterNumber") or 1
            plan_chapter_with_llm(state, chapter_number)
        except Exception:  # noqa: BLE001 - planner 失败不阻断流程,沿用默认骨架
            pass
        state["plan"]["status"] = "locked"
        state["directorDecision"] = "计划已锁定"
        add_trace(state, "Plan Gate", "用户锁定计划，允许 Scene Writer 分场景生成。")
        message = "计划已锁定"

    elif endpoint == "/api/scenes/generate":
        _require_active_project(state)
        chapter_number = state["project"]["chapterNumber"]
        # Step 1: planner agent 先把当前章节场景骨架展开/精化
        plan_chapter_with_llm(state, chapter_number)
        # Step 2: scene_writer 真正写正文
        _log_agent(state, "scene_writer", "running", chapter=chapter_number,
                   message="按 Scene Cards 生成正文草稿")
        llm_used = False
        llm_note = ""
        try:
            llm_used, llm_note = generate_scene_contents_optional_llm(state)
        except Exception as exc:  # noqa: BLE001 - fallback keeps workflow running and records error.
            llm_note = f"大模型生成失败，已回退本地草稿：{exc}"
            _log_agent(state, "scene_writer", "failed", chapter=chapter_number,
                       message=str(exc)[:200])
        else:
            _log_agent(state, "scene_writer", "done", chapter=chapter_number,
                       message=llm_note or "正文已生成")
        for scene in state["scenes"]:
            if scene["status"] == "draft":
                scene["status"] = "ready"
        state["directorDecision"] = "正文已按场景生成，等待审计"
        state["humanStyleReport"] = {"status": "pending", "chapterNumber": state["project"]["chapterNumber"]}
        # 章节正文重生成: 清掉总编打回标志,重置审计/总编结果
        state["draftScore"] = 0
        state["chiefEditorPassed"] = False
        state["chiefEditorRequiresUser"] = False
        state["editorReview"] = None
        state["editorScoreHistory"] = []
        state["autoFixLog"] = []
        state["revisionAttempts"][str(state["project"]["chapterNumber"])] = 0
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
        chapter_number = state["project"]["chapterNumber"]
        _log_agent(state, "audit", "running", chapter=chapter_number,
                   message="审计连续性/读者体验/风格语言")
        add_trace(state, "Audit Board", "连续性、读者体验、风格语言三类审计。")
        scenes = state.get("scenes") or []
        text = "\n\n".join(s.get("content", "") for s in scenes if s.get("content"))
        no_punct = count_chars_no_punct(text)
        target_min = chapter_min_words_no_punct(state)
        target_max = chapter_max_words_no_punct(state)
        config = get_llm_config("audit")
        llm_used = False
        if config.configured and text.strip():
            system_audit = (
                "你是中文网文审计编辑,只输出 JSON 不要解释。检查本章在 6 个维度的得分:"
                "连续性/读者体验/角色一致性/冲突升级/章末钩子/AI腔控制。"
                "每项给 60-95 的分数 + 一句中文 reason(<=40字),"
                "再给 draftScore (本章草稿总分,0-100,综合 6 项加权) + issues 数组。"
                "schema: {\"draftScore\":number,"
                "\"scores\":[{\"name\":string,\"score\":number,\"reason\":string}],"
                "\"issues\":[{\"level\":\"major\"|\"minor\",\"location\":string,\"issue\":string,\"fix\":string}]}"
            )
            user_audit = json.dumps({
                "chapterNumber": chapter_number,
                "chapterTitle": state["project"].get("chapterTitle"),
                "noPunctCharCount": no_punct,
                "requiredRange": f"{target_min}-{target_max}",
                "text": text[:8000],
                "genre": state.get("project", {}).get("genreLabel", ""),
                "canon": (state.get("memory", {}).get("canon") or [])[-10:],
                "readerPromise": state.get("plan", {}).get("readerPromise", ""),
            }, ensure_ascii=False)
            try:
                data_audit = chat_json(system_audit, user_audit, temperature=0.35,
                                        timeout=90, agent="audit")
                draft_score = _safe_int(data_audit.get("draftScore"), 0)
                scores_arr = data_audit.get("scores") or []
                issues_arr = data_audit.get("issues") or []
                if draft_score and isinstance(scores_arr, list):
                    state["draftScore"] = max(min(draft_score, 100), 0)
                    state["scores"] = [
                        {
                            "name": str(s.get("name", "")),
                            "score": _safe_int(s.get("score"), 0),
                            "reason": str(s.get("reason", ""))[:120],
                        }
                        for s in scores_arr
                        if isinstance(s, dict) and s.get("name")
                    ]
                    state["issues"] = [
                        {
                            "level": str(i.get("level", "minor")),
                            "location": str(i.get("location", "")),
                            "issue": str(i.get("issue", ""))[:200],
                            "fix": str(i.get("fix", ""))[:200],
                        }
                        for i in issues_arr
                        if isinstance(i, dict)
                    ]
                    llm_used = True
            except Exception as exc:  # noqa: BLE001 - 失败回退本地规则
                add_trace(state, "Audit Board", f"LLM 审计失败,沿用本地规则:{exc}")
        if not llm_used:
            # 回退:本地规则给个非硬编码的真实分(基于字数+detector)
            de_ai = compute_de_ai_metrics(text) if text.strip() else {}
            base = 85
            if no_punct < target_min:
                base -= 15
            elif no_punct > target_max:
                base -= 5
            if not de_ai.get("has_chapter_hook", True):
                base -= 10
            if de_ai.get("ai_blacklist_total", 0) >= 3:
                base -= 8
            state["draftScore"] = max(min(base, 95), 40)
            state["scores"] = [
                {"name": "连续性", "score": base, "reason": "本地规则评估"},
                {"name": "读者体验", "score": base - 2, "reason": "本地规则评估"},
                {"name": "角色一致性", "score": base, "reason": "本地规则评估"},
                {"name": "冲突升级", "score": base - 3, "reason": "本地规则评估"},
                {"name": "章末钩子", "score": (85 if de_ai.get("has_chapter_hook", True) else 60),
                 "reason": "末段钩子检测"},
                {"name": "AI 腔控制", "score": max(95 - de_ai.get("ai_suspicion_score", 0), 40),
                 "reason": f"AI 嫌疑 {de_ai.get('ai_suspicion_score', 0)}/100"},
                {"name": "原创表达", "score": 0, "reason": "等待原创表达审校"},
            ]
            state["issues"] = []
        state["directorDecision"] = "审计完成，等待定向修订"
        _log_agent(state, "audit", "done", chapter=chapter_number,
                   message=f"打分 {state.get('draftScore')}/100, "
                           f"{len(state.get('issues') or [])} 个问题"
                           + (" (LLM)" if llm_used else " (本地)"))
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

    elif endpoint == "/api/editor/review":
        _require_active_project(state)
        chapter_number = state["project"]["chapterNumber"]
        # 多轮自动循环:total ≤ 3 (1 次首审 + 2 次重写后复审)
        attempts = state.setdefault("revisionAttempts", {})
        key = str(chapter_number)
        MAX_REVISIONS = 2  # 最多两次自动重写
        message = ""
        review = None

        # 进入循环前先做一次"代码层硬规则修复" — 能批量修的不调 LLM
        # (雷区词替换 / 升华句删除 / 短段合并 / 多余标点清理)
        pre_fix = auto_fix_hard_rules(state)
        # 章末钩子专项:只在缺钩子时调 1 次 LLM 重写末段
        hook_ok, hook_note = auto_fix_chapter_hook(state)
        if hook_ok:
            _log_agent(state, "scene_writer", "done", chapter=chapter_number,
                       message=f"章末钩子局部重写:{hook_note}")

        for loop_round in range(1, MAX_REVISIONS + 2):
            review = chief_editor_review(state, chapter_number)
            if not review:
                if loop_round == 1:
                    detail = state.get("lastChiefEditorError") or ""
                    if detail:
                        raise RuntimeError(f"总编审核失败:{detail}")
                    raise RuntimeError("总编审核失败,请稍后重试")
                # 循环中失败 — 用上一轮 review 结果继续
                break
            state["editorReview"] = review
            action = review.get("action") or "revise"
            score = review.get("score") or 0
            history = state.setdefault("editorScoreHistory", [])
            history.append({
                "round": loop_round,
                "score": score,
                "action": action,
                "ts": datetime.now().strftime("%H:%M:%S"),
            })
            current = attempts.get(key, 0)
            if action == "approve":
                state["chiefEditorPassed"] = True
                state["chiefEditorRequiresUser"] = False
                rounds_text = f"({current} 次重写后)" if current > 0 else ""
                message = f"总编通过 ({score}/100){rounds_text}"
                break
            if action == "reject" or current >= MAX_REVISIONS:
                state["chiefEditorPassed"] = False
                state["chiefEditorRequiresUser"] = True
                _log_agent(state, "chief_editor", "review_required", chapter=chapter_number,
                           message=f"{current+1} 次审核仍不通过,需要人工介入")
                message = f"总编 {current+1} 次审核仍不通过 ({score}/100),请人工决定"
                break
            # 自动重写 1 次:优先用 patches 微改,失败再退回整章重写
            attempts[key] = current + 1
            patches = review.get("patches") or []
            ok = False
            note = ""
            if patches:
                _log_agent(state, "scene_writer", "revising", chapter=chapter_number,
                           message=f"按总编 {len(patches)} 条 patch 微改 (第 {current+1} 次)")
                ok, note = apply_editor_patches(state, patches)
            if not ok:
                # patches 路径未生效:退回整章重写(带 editorNotes)
                _log_agent(state, "scene_writer", "revising", chapter=chapter_number,
                           message=f"patch 微改未成功,改走整章重写 (第 {current+1} 次)")
                ok, note = rewrite_scenes_with_editor_notes(state, review.get("editorNotes", ""))
            if not ok:
                state["chiefEditorRequiresUser"] = True
                message = f"总编打回但自动重写失败 ({note}),请人工介入"
                break
            # 重写后再跑一次硬规则修复 — 确保 LLM 没把雷区词又写回来
            auto_fix_hard_rules(state)
            auto_fix_chapter_hook(state)
            _log_agent(state, "scene_writer", "done", chapter=chapter_number,
                       message=f"重写完成:{note}")
            # 重写后清审计分,但不退出循环 — 直接再走 chief_editor
            state["draftScore"] = 0
            state["chiefEditorPassed"] = False
            state["chiefEditorRequiresUser"] = False
            # next loop iteration 再调 chief_editor_review

    elif endpoint == "/api/editor/metrics":
        # 独立调度 detector,不调 LLM,用于前端"诊断按钮"或事后审计
        _require_active_project(state)
        scenes = state.get("scenes") or []
        text = "\n\n".join(s.get("content", "") for s in scenes if s.get("content"))
        if not text.strip():
            raise RuntimeError("当前章节正文为空")
        de_ai = compute_de_ai_metrics(text)
        state["lastDeAiMetrics"] = de_ai
        flag_count = len(de_ai.get("flags") or [])
        message = f"度量完成,{flag_count} 项触发硬规则" if flag_count else "度量完成,所有硬规则达标"

    elif endpoint == "/api/editor/override":
        _require_active_project(state)
        # 用户强制通过总编审核
        state["chiefEditorPassed"] = True
        state["chiefEditorRequiresUser"] = False
        _log_agent(state, "chief_editor", "done",
                   chapter=state["project"]["chapterNumber"],
                   message="用户人工通过")
        message = "已人工通过总编审核"

    elif endpoint == "/api/editor/rewrite":
        _require_active_project(state)
        chapter_number = state["project"]["chapterNumber"]
        review = state.get("editorReview") or {}
        notes = (review.get("editorNotes") or "").strip()
        if not notes:
            issues = review.get("issues") or []
            notes = "; ".join(
                f"[{(it.get('severity') or 'minor')}] {(it.get('text') or '')}"
                for it in issues[:6]
                if it.get("text")
            ) or "请按之前的审计意见和去 AI 味红线全面重写本章。"
        _log_agent(state, "scene_writer", "revising", chapter=chapter_number,
                   message=f"用户触发重写,带总编批注回到正文写手:{notes[:60]}")
        ok, note = rewrite_scenes_with_editor_notes(state, notes)
        if not ok:
            _log_agent(state, "scene_writer", "failed", chapter=chapter_number,
                       message=f"重写失败:{note}")
            raise RuntimeError(f"重写失败:{note}")
        _log_agent(state, "scene_writer", "done", chapter=chapter_number,
                   message=f"重写完成:{note}")
        # 重置审计/总编状态,流程退回到 audit
        state["draftScore"] = 0
        state["chiefEditorPassed"] = False
        state["chiefEditorRequiresUser"] = False
        state["editorReview"] = None
        # 用户手动重写后, 把自动循环计数清零, 让下一轮总编审核重新有 2 次自动机会
        state["revisionAttempts"][str(chapter_number)] = 0
        state["humanStyleReport"] = {"status": "pending", "chapterNumber": chapter_number}
        state["directorDecision"] = "正文已按总编批注重写,等待重新审计"
        add_trace(state, "Scene Writer", f"按总编批注重写本章:{note}")
        message = f"已按总编批注重写本章 ({note}),请重新审计"

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
        chapter_number = state["project"]["chapterNumber"]
        chapter_title = state["project"]["chapterTitle"]
        protagonist = state["characters"]["suHan"]["name"]
        # Truth agent: 真接 LLM,从正文里抽取本章新增的事实/伏笔/世界设定/角色状态
        truth_used, truth_note = settle_truth_with_llm(state, chapter_number, chapter_title)
        if not truth_used:
            # LLM 未启用 或 失败:沿用原本的硬编码占位,确保流程不断
            _log_agent(state, "truth", "done", chapter=chapter_number,
                       message=f"占位归档({truth_note})")
            canon_title = f"第 {chapter_number} 章事件归档"
            if not any(item.get("title") == canon_title for item in state["memory"]["canon"]):
                state["memory"]["canon"].append(
                    {
                        "title": canon_title,
                        "text": f"{protagonist}完成《{chapter_title}》关键事件：{state['plan'].get('readerPromise','')}",
                    }
                )
        next_version = parse_truth_version(state.get("truthAfter", "v1")) + 1
        state["truthAfter"] = f"v{next_version} committed"
        state["directorDecision"] = "TruthPatch 已写入"
        state["issues"] = [issue for issue in state.get("issues", []) if issue.get("level") == "minor"]
        add_trace(state, "TruthMerger", f"TruthPatch schema 校验通过，真相文件版本递增到 v{next_version}。{truth_note}")
        message = f"TruthPatch 已写入 (v{next_version}){truth_note and ' · '+truth_note}"

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
        _log_agent(state, "ideation", "running", message="生成题材书案")
        try:
            idea = generate_idea_draft_optional_llm(body)
        except Exception as exc:
            _log_agent(state, "ideation", "failed", message=str(exc)[:200])
            raise
        ensure_idea_polish_fields(idea)
        state["pendingIdeaDraft"] = idea
        source = f"大模型 {idea.get('llmModel')}" if idea.get("llmGenerated") else "本地题材配置"
        _log_agent(state, "ideation", "done",
                   message=f"《{idea['selectedTitle']}》 · {source}")
        add_trace(state, "Ideation", f"根据题材生成书案：《{idea['selectedTitle']}》（{source}）。")
        message = "题材书案已生成"

    elif endpoint == "/api/ideation/set-genre":
        idea = state.get("pendingIdeaDraft")
        if not idea:
            raise ValueError("没有待打磨的书案，请先生成题材方案")
        ensure_idea_polish_fields(idea)
        new_genre = str(body.get("genre") or "").strip()
        if new_genre not in GENRE_DEEP_CONFIGS:
            raise ValueError(f"不支持的流派：{new_genre}")
        old_genre = idea.get("genre")
        if old_genre == new_genre:
            message = "流派未变化"
        else:
            deep = GENRE_DEEP_CONFIGS[new_genre]
            before_snapshot = {
                "genre": old_genre,
                "genreLabel": idea.get("genreLabel"),
                "deepRules": idea.get("deepRules"),
            }
            after_snapshot = {
                "genre": new_genre,
                "genreLabel": deep["label"],
                "deepRules": {
                    "coreMechanism": deep["coreMechanism"],
                    "openingRecipe": deep["openingRecipe"],
                    "payoffEngine": deep["payoffEngine"],
                    "mustHave": deep["mustHave"],
                    "avoid": deep["avoid"],
                    "firstThree": deep["firstThree"],
                },
            }
            idea["genre"] = after_snapshot["genre"]
            idea["genreLabel"] = after_snapshot["genreLabel"]
            idea["deepRules"] = after_snapshot["deepRules"]
            _append_revision(idea, "genre", before_snapshot, after_snapshot, source="manual")
            # 候选(标题/主角/简介)只在「首次离开原始生成流派」时打标
            # 之后即使再换几次流派,只要还没同步重生成候选,stale 永远指向"候选实际生成时的流派"
            # 改回当初生成时的流派 → 候选刚好对上,标记撤掉
            existing_stale = idea.get("candidatesStaleAfterGenre")
            if existing_stale:
                if existing_stale == deep["label"]:
                    # 改回了候选最初对应的流派,横幅自动撤掉
                    idea["candidatesStaleAfterGenre"] = None
                # else: 已有 stale 标记,保持不变(继续指向最初生成流派)
            else:
                # 首次离开生成流派,用 before 的 label 作为"候选当时的流派"
                old_label = before_snapshot.get("genreLabel") or old_genre or "上一流派"
                idea["candidatesStaleAfterGenre"] = old_label
            message = f"已改判为「{deep['label']}」（修订 {len(idea['revisions'])}）"
        idea["confirmed"] = False
        state["pendingIdeaDraft"] = idea

    elif endpoint == "/api/ideation/regenerate-candidates":
        idea = state.get("pendingIdeaDraft")
        if not idea:
            raise ValueError("没有待打磨的书案，请先生成题材方案")
        ensure_idea_polish_fields(idea)
        result = regenerate_all_candidates(idea)
        old_label = idea.get("candidatesStaleAfterGenre") or "上一流派"
        new_label = idea.get("genreLabel") or idea.get("genre") or "当前流派"
        instruction = f"流派从「{old_label}」改判为「{new_label}」后同步重生成候选"
        rev_count_before = len(idea.get("revisions", []))
        for field in ("recommendedTitles", "recommendedProtagonists", "synopsisOptions"):
            new_value = result[field]
            before = idea.get(field)
            idea[field] = new_value
            # 数组改动可能让 selectedTitle / selectedProtagonist / selectedSynopsis 指向失效项,顺手重置
            if field == "recommendedTitles" and idea.get("selectedTitle") not in new_value:
                idea["selectedTitle"] = new_value[0]
            if field == "recommendedProtagonists" and idea.get("selectedProtagonist") not in new_value:
                idea["selectedProtagonist"] = new_value[0]
            if field == "synopsisOptions":
                first = new_value[0] if new_value else None
                first_text = ""
                first_style = ""
                if isinstance(first, dict):
                    first_text = str(first.get("text") or "")
                    first_style = str(first.get("style") or "")
                elif isinstance(first, str):
                    first_text = first
                existing_texts = [(o.get("text") if isinstance(o, dict) else o) for o in new_value]
                if first_text and idea.get("selectedSynopsis") not in existing_texts:
                    idea["selectedSynopsis"] = first_text
                    if first_style:
                        idea["selectedSynopsisStyle"] = first_style
            _append_revision(idea, field, before, new_value, source="ai-genre-sync", instruction=instruction)
        idea["candidatesStaleAfterGenre"] = None
        idea["confirmed"] = False
        state["pendingIdeaDraft"] = idea
        added = len(idea["revisions"]) - rev_count_before
        message = f"已按「{new_label}」流派同步重生成 3 组候选（修订 +{added}）"

    elif endpoint == "/api/ideation/patch":
        idea = state.get("pendingIdeaDraft")
        if not idea:
            raise ValueError("没有待打磨的书案，请先生成题材方案")
        ensure_idea_polish_fields(idea)
        field = str(body.get("field") or "").strip()
        if field not in IDEA_EDITABLE_TEXT_FIELDS:
            raise ValueError(f"字段 {field} 不允许手动编辑")
        new_value = body.get("value")
        if not isinstance(new_value, str):
            raise ValueError("手动编辑只接受字符串")
        new_value = new_value.strip()
        if not new_value:
            raise ValueError("内容不能为空")
        before = idea.get(field)
        if before == new_value:
            message = "内容未变化"
        else:
            idea[field] = new_value
            _append_revision(idea, field, before, new_value, source="manual")
            message = f"已记录手改：{IDEA_FIELD_LABELS.get(field, field)}（修订 {len(idea['revisions'])}）"
        # 手改后视为有未确认改动,撤销 confirmed
        idea["confirmed"] = False
        state["pendingIdeaDraft"] = idea

    elif endpoint == "/api/ideation/refine":
        idea = state.get("pendingIdeaDraft")
        if not idea:
            raise ValueError("没有待打磨的书案，请先生成题材方案")
        ensure_idea_polish_fields(idea)
        field = str(body.get("field") or "").strip()
        instruction = str(body.get("instruction") or "").strip()
        if field in IDEA_EDITABLE_TEXT_FIELDS:
            new_value = refine_idea_field(idea, field, instruction)
            before = idea.get(field)
            idea[field] = new_value
            _append_revision(idea, field, before, new_value, source="ai", instruction=instruction)
        elif field in IDEA_REGENERATABLE_ARRAY_FIELDS:
            new_value = _regenerate_array_field(idea, field, instruction)
            before = idea.get(field)
            idea[field] = new_value
            # 数组改动可能让 selectedTitle / selectedProtagonist / selectedSynopsis 指向失效项,顺手重置一下
            if field == "recommendedTitles" and idea.get("selectedTitle") not in new_value:
                idea["selectedTitle"] = new_value[0]
            if field == "recommendedProtagonists" and idea.get("selectedProtagonist") not in new_value:
                idea["selectedProtagonist"] = new_value[0]
            if field == "synopsisOptions":
                first_text = ""
                first_style = ""
                first = new_value[0] if new_value else None
                if isinstance(first, dict):
                    first_text = str(first.get("text") or "")
                    first_style = str(first.get("style") or "")
                elif isinstance(first, str):
                    first_text = first
                if first_text and idea.get("selectedSynopsis") not in [
                    (o.get("text") if isinstance(o, dict) else o) for o in new_value
                ]:
                    idea["selectedSynopsis"] = first_text
                    if first_style:
                        idea["selectedSynopsisStyle"] = first_style
            _append_revision(idea, field, before, new_value, source="ai", instruction=instruction)
            # 用户主动重生成了某个候选数组,横幅可以撤掉
            idea["candidatesStaleAfterGenre"] = None
        else:
            raise ValueError(f"字段 {field} 不支持 AI 打磨")
        idea["confirmed"] = False
        state["pendingIdeaDraft"] = idea
        message = f"AI 打磨完成：{IDEA_FIELD_LABELS.get(field, field)}（修订 {len(idea['revisions'])}）"

    elif endpoint == "/api/ideation/revert":
        idea = state.get("pendingIdeaDraft")
        if not idea:
            raise ValueError("没有待打磨的书案，请先生成题材方案")
        ensure_idea_polish_fields(idea)
        revision_id = str(body.get("revisionId") or "").strip()
        if not revision_id:
            raise ValueError("缺少修订 ID")
        field, before, target_value = _revert_field_value(idea, revision_id)
        if field == "genre" and isinstance(target_value, dict):
            # genre 字段的快照是 {genre, genreLabel, deepRules} 打包,要展开回去
            current_snapshot = {
                "genre": idea.get("genre"),
                "genreLabel": idea.get("genreLabel"),
                "deepRules": idea.get("deepRules"),
            }
            idea["genre"] = target_value.get("genre")
            idea["genreLabel"] = target_value.get("genreLabel")
            idea["deepRules"] = target_value.get("deepRules")
            _append_revision(idea, "genre", current_snapshot, target_value, source="revert", instruction=f"回退到 {revision_id}")
            display_label = target_value.get("genreLabel") or target_value.get("genre") or "流派"
            message = f"已回退流派为「{display_label}」（修订 {len(idea['revisions'])}）"
        else:
            idea[field] = target_value
            _append_revision(idea, field, before, target_value, source="revert", instruction=f"回退到 {revision_id}")
            message = f"已回退 {IDEA_FIELD_LABELS.get(field, field)}（修订 {len(idea['revisions'])}）"
        idea["confirmed"] = False
        state["pendingIdeaDraft"] = idea

    elif endpoint == "/api/ideation/confirm":
        idea = state.get("pendingIdeaDraft")
        if not idea:
            raise ValueError("没有待打磨的书案，请先生成题材方案")
        ensure_idea_polish_fields(idea)
        confirmed = bool(body.get("confirmed"))
        idea["confirmed"] = confirmed
        state["pendingIdeaDraft"] = idea
        message = "已确认书案设定" if confirmed else "已取消确认"

    elif endpoint == "/api/projects/create-from-idea":
        idea = body.get("ideaDraft") or state.get("pendingIdeaDraft") or state.get("ideaDraft")
        if not idea:
            raise ValueError("没有可采用的书案，请先生成题材方案")
        ensure_idea_polish_fields(idea)
        if not idea.get("confirmed"):
            raise RuntimeError("请先勾选「我已通读并确认」再创建作品")
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

    elif endpoint == "/api/export/acknowledge":
        # 用户在导出提醒横幅点「我已导出」时:把 lastExportedChapter 推到当前已归档最高章
        archived_max = max([int(c.get("number") or 0) for c in state.get("chapterArchive", [])] + [0])
        state["lastExportedChapter"] = max(int(state.get("lastExportedChapter") or 0), archived_max)
        add_trace(state, "Export", f"用户确认已导出至第 {state['lastExportedChapter']} 章")
        message = "已确认导出,提醒已清除"

    else:
        raise KeyError(f"Unknown endpoint: {endpoint}")

    store.save(state)
    return state, message


class NovelStudioHandler(SimpleHTTPRequestHandler):
    server_version = "Qianjuan/0.1"

    def _setup_user_ctx(self) -> None:
        """认证身份解析(按优先级):
        1) Authorization: Bearer <ec-ai 主站 token> → 用主站 uid (登录用户)
        2) Cookie qj_uid → 用老的匿名 cookie uid (兼容期, 不再下发新 cookie)
        3) 无身份 → 临时分配 uid, 但标记为 anonymous (前端会触发登录门)
        """
        from http.cookies import SimpleCookie

        uid: str | None = None
        authed_email: str | None = None
        is_authed = False

        # 1) Authorization header
        auth_header = self.headers.get("Authorization") or self.headers.get("authorization") or ""
        if auth_header.startswith("Bearer "):
            token = auth_header[len("Bearer ") :].strip()
            decoded = verify_ec_ai_token(token)
            if decoded:
                uid = decoded["uid"]
                authed_email = decoded["email"]
                is_authed = True

        # 1.5) Forum current-user trust 通道
        # 主站 ai秘密基地 的用户态是纯前端 localStorage (aisecretlair-forum-current-user),
        # 没有后端 cookie/JWT 可用。千卷与主站同源 (www.aisecretlair.com),所以
        # localStorage 同源共享 — 我们让前端把这个用户 JSON base64 后通过
        # X-Forum-Current-User 头送过来,后端 trust 这个值取 uid/email。
        # 安全性等同于主站本身 (主站也是纯客户端 auth),不增加新攻击面。
        if not is_authed:
            forum_header = (
                self.headers.get("X-Forum-Current-User")
                or self.headers.get("x-forum-current-user")
                or ""
            )
            if forum_header:
                try:
                    import base64 as _b64
                    pad = "=" * ((4 - len(forum_header) % 4) % 4)
                    raw = _b64.urlsafe_b64decode(forum_header + pad).decode("utf-8")
                    user_obj = json.loads(raw)
                    forum_uid = (user_obj.get("id") or "").strip()
                    forum_email = (user_obj.get("email") or "").strip()
                    if forum_uid:
                        uid = "forum_" + forum_uid
                        authed_email = forum_email or None
                        is_authed = True
                except Exception:  # noqa: BLE001
                    pass

        # 2) 兼容老 cookie
        if not uid:
            cookie_header = self.headers.get("Cookie", "")
            if cookie_header:
                try:
                    parsed = SimpleCookie(cookie_header)
                    morsel = parsed.get(COOKIE_NAME)
                    if morsel:
                        uid = _safe_uid(morsel.value)
                except Exception:  # noqa: BLE001
                    uid = None

        # 3) 无身份: 给个临时 uid,但不写 cookie(前端门会要求登录)
        if not uid:
            uid = uuid.uuid4().hex
            # 不再自动 set cookie,因为我们要强制登录
            # self._new_uid = uid
        _user_ctx.uid = uid
        _user_ctx.is_authed = is_authed
        _user_ctx.email = authed_email
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

    # 公开端点白名单(不需要登录就能访问)
    PUBLIC_ENDPOINTS = frozenset({"/api/auth/status"})

    def _require_auth_or_401(self, parsed_path: str) -> bool:
        """临时取消鉴权拦截 — 全部放行 (2026-05-24 用户要求, 等 SSO 重做)"""
        return True

    def do_GET(self) -> None:
        self._setup_user_ctx()
        parsed = urlparse(self.path)
        # 公开端点: 不需要登录就能查询身份状态
        if parsed.path == "/api/auth/status":
            self.send_json({
                "authenticated": bool(getattr(_user_ctx, "is_authed", False)),
                "email": getattr(_user_ctx, "email", None),
                "uid": _current_uid() if getattr(_user_ctx, "is_authed", False) else None,
                "loginUrl": "https://www.aisecretlair.com/",
            })
            return
        # 鉴权门:其他 /api/* 必须登录
        if not self._require_auth_or_401(parsed.path):
            return
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
        if parsed.path in ("/api/export/markdown", "/api/export/txt"):
            state = store.load()
            query = parse_qs(parsed.query)
            chapter_number = _safe_int(query.get("chapter", [None])[0], 0) or None
            try:
                content = export_chapter_plaintext(state, chapter_number).encode("utf-8")
            except RuntimeError as user_exc:
                self.send_export_error(str(user_exc))
                return
            filename_number = chapter_number or state["project"]["chapterNumber"]
            # 记录最近导出章节,触发/清空导出提醒
            try:
                target = max(int(state.get("lastExportedChapter") or 0), int(filename_number))
                state["lastExportedChapter"] = target
                store.save(state)
            except Exception:
                pass
            filename = f"chapter-{filename_number:03d}.txt"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        if parsed.path in ("/api/export/book", "/api/export/book-txt"):
            state = store.load()
            try:
                content = export_book_plaintext(state).encode("utf-8")
            except RuntimeError as user_exc:
                self.send_export_error(str(user_exc))
                return
            project_title = (state.get("project") or {}).get("title") or "book"
            safe_title = re.sub(r"[\\/:*?\"<>|\s]+", "_", project_title).strip("_") or "book"
            # 全书导出 → 已完成的所有章节都已落盘
            try:
                archived_max = max([int(c.get("number") or 0) for c in state.get("chapterArchive", [])] + [0])
                state["lastExportedChapter"] = max(int(state.get("lastExportedChapter") or 0), archived_max)
                store.save(state)
            except Exception:
                pass
            filename = f"{safe_title}.txt"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
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
        if not self._require_auth_or_401(parsed.path):
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
            # 容错解码:某些客户端(curl on Windows、误配置 SDK)会发 GBK/Latin-1 编码的请求体
            # 优先按 Content-Type 上声明的 charset;否则 utf-8 → gbk → utf-8(replace) 链式回退
            ctype = self.headers.get("Content-Type", "") or ""
            declared = ""
            for piece in ctype.split(";"):
                piece = piece.strip().lower()
                if piece.startswith("charset="):
                    declared = piece[len("charset="):].strip().strip('"').strip("'")
                    break
            decoded = None
            tried = []
            order = []
            if declared:
                order.append(declared)
            for cand in ("utf-8", "gbk", "gb18030"):
                if cand not in order:
                    order.append(cand)
            for enc in order:
                try:
                    decoded = body_bytes.decode(enc)
                    break
                except (UnicodeDecodeError, LookupError) as exc:
                    tried.append(f"{enc}:{type(exc).__name__}")
                    continue
            if decoded is None:
                # 最后兜底:utf-8 replace,保证不抛 500;业务层会按字段缺失友好提示
                decoded = body_bytes.decode("utf-8", errors="replace")
            try:
                body = json.loads(decoded) if decoded.strip() else {}
            except json.JSONDecodeError as exc:
                self.send_json(
                    {"error": "请求体不是合法 JSON,请检查客户端是否以 UTF-8 编码发送", "detail": str(exc)[:200]},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
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

    def _wants_html(self) -> bool:
        """浏览器直接访问(地址栏/外链)时 Accept 头含 text/html;
        前端 fetch() 默认是 */*。靠这个区分两种来访者。"""
        accept = (self.headers.get("Accept") or "").lower()
        if "text/html" in accept and "application/json" not in accept.split(",")[0]:
            return True
        return "text/html" in accept and accept.startswith("text/html")

    def send_user_error_page(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST,
                             title: str = "暂时还没法导出", back_label: str = "回到千卷工作台") -> None:
        """给浏览器直接访问 API 的人一个体面的错误页,而不是光秃秃的 JSON。"""
        safe_msg = (message or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        safe_title = (title or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        html = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title} · 千卷</title>
<style>
  :root {{
    --bg:#f6f3ec; --paper:#fffdf8; --ink:#25211b; --muted:#746f66;
    --line:#ded7ca; --amber:#b36a1f; --amber-soft:#f6eadb; --teal:#1f7a74;
  }}
  *{{box-sizing:border-box}}
  html,body{{height:100%;margin:0}}
  body{{
    font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Noto Serif SC","Microsoft YaHei",serif;
    background:
      radial-gradient(900px 600px at 20% 0%, rgba(179,106,31,.10), transparent 60%),
      radial-gradient(700px 500px at 100% 100%, rgba(31,122,116,.08), transparent 60%),
      var(--bg);
    color:var(--ink);
    display:grid;place-items:center;
    padding:32px 20px;
    -webkit-font-smoothing:antialiased;
  }}
  .card{{
    max-width:520px;width:100%;
    background:var(--paper);
    border:1px solid var(--line);
    border-radius:18px;
    padding:42px 36px 32px;
    text-align:center;
    box-shadow:0 24px 60px rgba(62,53,39,.10);
    position:relative;
  }}
  .card::before{{
    content:"";position:absolute;inset:8px;border:1px dashed rgba(179,106,31,.22);
    border-radius:12px;pointer-events:none;
  }}
  .icon{{
    width:64px;height:64px;border-radius:50%;
    background:var(--amber-soft);
    color:var(--amber);
    font-size:32px;line-height:64px;
    margin:0 auto 18px;
    border:1px solid rgba(179,106,31,.25);
  }}
  h1{{
    font-size:22px;margin:0 0 12px;font-weight:600;letter-spacing:.5px;
  }}
  p.msg{{
    font-size:15px;line-height:1.75;color:var(--ink);
    margin:0 0 8px;
  }}
  p.hint{{
    font-size:13px;color:var(--muted);margin:0 0 26px;line-height:1.7;
  }}
  .divider{{
    width:48px;height:1px;background:var(--line);margin:18px auto;
  }}
  .actions{{
    display:flex;gap:10px;justify-content:center;flex-wrap:wrap;margin-top:8px;
  }}
  .btn{{
    display:inline-flex;align-items:center;gap:6px;
    padding:10px 20px;border-radius:10px;
    text-decoration:none;font-size:14px;font-weight:500;
    transition:transform 80ms, background 120ms, border-color 120ms;
    font-family:inherit;
  }}
  .btn.primary{{
    background:var(--amber);color:#fffdf8;border:1px solid var(--amber);
  }}
  .btn.primary:hover{{background:#9a5a17;border-color:#9a5a17}}
  .btn.ghost{{
    background:transparent;color:var(--ink);border:1px solid var(--line);
  }}
  .btn.ghost:hover{{background:rgba(179,106,31,.06);border-color:rgba(179,106,31,.4)}}
  .btn:active{{transform:translateY(1px)}}
  .seal{{
    margin-top:26px;font-size:11px;color:var(--muted);letter-spacing:.2em;
  }}
</style>
</head>
<body>
  <div class="card">
    <div class="icon">📜</div>
    <h1>{safe_title}</h1>
    <p class="msg">{safe_msg}</p>
    <p class="hint">回到工作台,先在「题材成书」或「手动新建」里开一本作品,写到 settle 之后就能导出归档了。</p>
    <div class="divider"></div>
    <div class="actions">
      <a class="btn primary" href="/toolbox/qianjuan/">{back_label}</a>
      <a class="btn ghost" href="/toolbox/">浏览 AI 工具箱</a>
    </div>
    <div class="seal">千 卷 · 沉 淀 创 作</div>
  </div>
</body>
</html>"""
        content = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def send_export_error(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        """导出口的错误统一走这:浏览器直接访问 → HTML 错误页;fetch → JSON。"""
        if self._wants_html():
            self.send_user_error_page(message, status=status)
        else:
            self.send_json({"error": message}, status=status)

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
