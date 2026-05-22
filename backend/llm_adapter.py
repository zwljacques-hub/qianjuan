from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LLMConfig:
    configured: bool
    provider: str
    api_key: str
    base_url: str
    model: str


AGENT_ROLES = [
    {"id": "ideation", "label": "题材策划"},
    {"id": "planner", "label": "章节规划"},
    {"id": "scene_writer", "label": "正文写手"},
    {"id": "audit", "label": "审计修订"},
    {"id": "truth", "label": "真相归档"},
]


# ============================================================
# 多租户 LLM 配置 (per-user 运行时覆盖)
# - server.py 在每个请求入口调 set_user_id(uid)
# - 每个 user 有自己的 global / per-agent 覆盖 dict
# - 没有覆盖时回退到 /etc/qianjuan.env 的服务器级 default
# - 内存存储,服务重启清空,这跟 README 一致
# ============================================================
_uid_ctx = threading.local()
_RUNTIME_BY_USER: dict[str, dict[str, str]] = {}
_RUNTIME_AGENTS_BY_USER: dict[str, dict[str, dict[str, str]]] = {}
_ANONYMOUS = "_anonymous"


def set_user_id(uid: str | None) -> None:
    """server.py 每次请求入口处调用一次,设置当前线程的 user_id。"""
    _uid_ctx.uid = uid or _ANONYMOUS


def _current_uid() -> str:
    return getattr(_uid_ctx, "uid", None) or _ANONYMOUS


def _user_global() -> dict[str, str]:
    return _RUNTIME_BY_USER.setdefault(_current_uid(), {})


def _user_agents() -> dict[str, dict[str, str]]:
    return _RUNTIME_AGENTS_BY_USER.setdefault(_current_uid(), {})


def _env_llm_config() -> LLMConfig:
    api_key = (
        os.environ.get("NOVEL_LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY")
        or ""
    ).strip()

    if os.environ.get("DEEPSEEK_API_KEY") and not (
        os.environ.get("NOVEL_LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    ):
        base_url = "https://api.deepseek.com/v1"
        provider = "deepseek"
        default_model = "deepseek-chat"
    else:
        base_url = os.environ.get("NOVEL_LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        provider = "openai-compatible"
        default_model = "gpt-4o-mini"

    model = os.environ.get("NOVEL_LLM_MODEL") or os.environ.get("OPENAI_MODEL") or default_model
    return LLMConfig(
        configured=bool(api_key),
        provider=provider,
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        model=model,
    )


def get_llm_config(agent: str | None = None) -> LLMConfig:
    config = _env_llm_config()
    g = _user_global()
    values = {
        "api_key": g.get("apiKey") or config.api_key,
        "base_url": (g.get("baseUrl") or config.base_url).rstrip("/"),
        "model": g.get("model") or config.model,
        "provider": g.get("provider") or config.provider,
    }

    if agent:
        agent_config = _user_agents().get(agent, {})
        values["api_key"] = agent_config.get("apiKey") or values["api_key"]
        values["base_url"] = (agent_config.get("baseUrl") or values["base_url"]).rstrip("/")
        values["model"] = agent_config.get("model") or values["model"]
        values["provider"] = agent_config.get("provider") or values["provider"]

    return LLMConfig(
        configured=bool(values["api_key"]),
        provider=values["provider"],
        api_key=values["api_key"],
        base_url=values["base_url"],
        model=values["model"],
    )


def update_runtime_llm_config(config: dict[str, Any]) -> None:
    g = _user_global()
    global_config = config.get("global") or {}
    for source, target in [
        ("apiKey", "apiKey"),
        ("baseUrl", "baseUrl"),
        ("model", "model"),
        ("provider", "provider"),
    ]:
        value = str(global_config.get(source) or "").strip()
        if value:
            g[target] = value

    agents = config.get("agents") or {}
    if isinstance(agents, list):
        agents = {str(item.get("id")): item for item in agents if isinstance(item, dict) and item.get("id")}
    if not isinstance(agents, dict):
        return

    valid_roles = {role["id"] for role in AGENT_ROLES}
    user_agents = _user_agents()
    for role_id, role_config in agents.items():
        if role_id not in valid_roles or not isinstance(role_config, dict):
            continue
        current = user_agents.setdefault(role_id, {})
        for source, target in [
            ("apiKey", "apiKey"),
            ("baseUrl", "baseUrl"),
            ("model", "model"),
            ("provider", "provider"),
        ]:
            value = str(role_config.get(source) or "").strip()
            if value:
                current[target] = value


def public_llm_config() -> dict[str, Any]:
    global_config = get_llm_config()
    agents = []
    user_agents = _user_agents()
    for role in AGENT_ROLES:
        role_config = get_llm_config(role["id"])
        explicit = user_agents.get(role["id"], {})
        agents.append(
            {
                "id": role["id"],
                "label": role["label"],
                "configured": role_config.configured,
                "baseUrl": role_config.base_url,
                "model": role_config.model,
                "hasCustomApiKey": bool(explicit.get("apiKey")),
                "customBaseUrl": explicit.get("baseUrl", ""),
                "customModel": explicit.get("model", ""),
            }
        )
    return {
        "configured": global_config.configured,
        "provider": global_config.provider,
        "baseUrl": global_config.base_url,
        "model": global_config.model,
        "hasRuntimeApiKey": bool(_user_global().get("apiKey")),
        "roles": AGENT_ROLES,
        "agents": agents,
    }


def public_llm_status() -> dict[str, Any]:
    config = get_llm_config()
    return {
        "configured": config.configured,
        "provider": config.provider,
        "baseUrl": config.base_url,
        "model": config.model,
        "agents": public_llm_config()["agents"],
    }


def chat_json(system: str, user: str, *, temperature: float = 0.4, timeout: int = 90, agent: str | None = None) -> dict[str, Any]:
    config = get_llm_config(agent)
    if not config.configured:
        raise RuntimeError("未配置大模型 API Key。请设置 NOVEL_LLM_API_KEY 或 OPENAI_API_KEY。")

    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }

    try:
        data = _post_chat_completion(config, payload, timeout)
    except RuntimeError as error:
        if "response_format" not in str(error):
            raise
        payload.pop("response_format", None)
        data = _post_chat_completion(config, payload, timeout)

    content = data["choices"][0]["message"]["content"]
    return _parse_json_content(content)


def _post_chat_completion(config: LLMConfig, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{config.base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LLM HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"LLM request failed: {error}") from error


def _parse_json_content(content: str) -> dict[str, Any]:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start >= 0 and end > start:
            return json.loads(content[start : end + 1])
        raise
