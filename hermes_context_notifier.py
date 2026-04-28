"""Hermes context usage threshold notifier plugin."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PLUGIN_DIR = Path(__file__).resolve().parent
CACHE_PATH = PLUGIN_DIR / "cache.json"

_SESSION_CONTEXT_BY_ID: dict[str, dict[str, Any]] = {}


def _platform_value(platform: Any) -> str:
    return str(getattr(platform, "value", platform) or "").lower()


def is_slack_event(event: Any) -> bool:
    source = getattr(event, "source", None)
    return _platform_value(getattr(source, "platform", None)) == "slack"


def _k(tokens: int) -> int:
    return int(round((int(tokens) or 0) / 1000))


def emoji_for_bucket(bucket: int) -> str:
    if bucket <= 65:
        return ":straight_ruler:"
    if bucket <= 85:
        return ":warning:"
    return ":rotating_light:"


def format_notice(bucket: int, used: int, limit: int) -> str:
    return f"{emoji_for_bucket(bucket)} Context: {int(bucket)}% ({_k(used)}K/{_k(limit)}K)"


def load_cache(path: Path = CACHE_PATH) -> dict[str, Any]:
    try:
        if not path.exists():
            return {"sessions": {}}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"sessions": {}}
        sessions = data.get("sessions")
        if not isinstance(sessions, dict):
            data["sessions"] = {}
        return data
    except Exception:
        return {"sessions": {}}


def write_cache(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _bucket_for(used: int, limit: int) -> tuple[int, float] | None:
    if not used or not limit or used <= 0 or limit <= 0:
        return None
    percent = (used / limit) * 100
    bucket = int(percent // 5) * 5
    return bucket, percent


def evaluate_notification(record: dict[str, Any], *, used: int, limit: int) -> dict[str, Any] | None:
    bucket_data = _bucket_for(used, limit)
    if bucket_data is None:
        return None
    bucket, percent = bucket_data
    last = int(record.get("last_notified_bucket", 0) or 0)

    record["used"] = int(used)
    record["limit"] = int(limit)
    record["percent"] = round(percent, 1)
    record["bucket"] = bucket

    if bucket < last:
        record["last_notified_bucket"] = bucket
        return None

    if bucket < 50 or bucket <= last:
        return None

    record["last_notified_bucket"] = bucket
    return {
        "bucket": bucket,
        "text": format_notice(bucket, used, limit),
        "used": int(used),
        "limit": int(limit),
        "percent": round(percent, 1),
    }


def _agent_usage(agent: Any) -> tuple[int, int, Any] | None:
    ctx = getattr(agent, "context_compressor", None)
    if ctx is None:
        return None
    used = int(getattr(ctx, "last_prompt_tokens", 0) or 0)
    limit = int(getattr(ctx, "context_length", 0) or 0)
    if used <= 0 or limit <= 0:
        return None
    return used, limit, agent


def extract_usage(gateway: Any, session_key: str) -> tuple[int, int, Any] | None:
    running = getattr(gateway, "_running_agents", {}) or {}
    agent = running.get(session_key)
    usage = _agent_usage(agent) if agent is not None else None
    if usage is not None:
        return usage

    cache = getattr(gateway, "_agent_cache", {}) or {}
    cached = cache.get(session_key)
    if isinstance(cached, tuple) and cached:
        agent = cached[0]
    else:
        agent = cached
    return _agent_usage(agent) if agent is not None else None


def capture_gateway_context(event: Any, gateway: Any, session_store: Any, **_: Any) -> dict[str, Any] | None:
    if not is_slack_event(event):
        return None

    entry = session_store.get_or_create_session(event.source)
    session_key = getattr(entry, "session_key", "") or ""
    session_id = getattr(entry, "session_id", "") or ""
    if not session_key or not session_id:
        return None

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    source = event.source
    adapters = getattr(gateway, "adapters", {}) or {}
    adapter = None
    platform_key = getattr(source, "platform", None)
    try:
        adapter = adapters.get(platform_key)
    except TypeError:
        adapter = None
    if adapter is None:
        for key, candidate in adapters.items():
            if _platform_value(key) == "slack":
                adapter = candidate
                break

    thread_id = getattr(source, "thread_id", None) or getattr(event, "message_id", None)
    meta = {
        "gateway": gateway,
        "adapter": adapter,
        "session_store": session_store,
        "session_key": session_key,
        "session_id": session_id,
        "platform": "slack",
        "chat_id": getattr(source, "chat_id", None),
        "thread_id": thread_id,
        "loop": loop,
    }
    _SESSION_CONTEXT_BY_ID[session_id] = meta
    return meta


def _unwrap_callback(entry: Any) -> tuple[int | None, Callable | None]:
    if isinstance(entry, tuple) and len(entry) == 2:
        generation, callback = entry
        return generation, callback if callable(callback) else None
    return None, entry if callable(entry) else None


def _send_later(adapter: Any, chat_id: str, text: str, thread_id: str | None, loop: asyncio.AbstractEventLoop | None) -> None:
    metadata = {"thread_id": thread_id} if thread_id else None

    async def _send() -> None:
        await adapter.send(chat_id, text, metadata=metadata)

    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
    asyncio.run_coroutine_threadsafe(_send(), loop)


def register_post_delivery_notice(
    *,
    adapter: Any,
    session_key: str,
    chat_id: str,
    thread_id: str | None,
    text: str,
    loop: asyncio.AbstractEventLoop | None,
    generation: int | None = None,
) -> None:
    existing_entry = getattr(adapter, "_post_delivery_callbacks", {}).get(session_key)
    existing_generation, existing_callback = _unwrap_callback(existing_entry)
    effective_generation = generation if generation is not None else existing_generation

    def combined_callback() -> None:
        if callable(existing_callback):
            existing_callback()
        _send_later(adapter, chat_id, text, thread_id, loop)

    if hasattr(adapter, "register_post_delivery_callback"):
        adapter.register_post_delivery_callback(
            session_key,
            combined_callback,
            generation=effective_generation,
        )
    else:
        adapter._post_delivery_callbacks[session_key] = (
            (effective_generation, combined_callback)
            if effective_generation is not None
            else combined_callback
        )


def _adapter_generation(adapter: Any, session_key: str) -> int | None:
    try:
        event = getattr(adapter, "_active_sessions", {}).get(session_key)
        generation = getattr(event, "_hermes_run_generation", None)
        return int(generation) if generation is not None else None
    except Exception:
        return None


def pre_gateway_dispatch(event: Any, gateway: Any, session_store: Any, **kwargs: Any) -> None:
    capture_gateway_context(event=event, gateway=gateway, session_store=session_store, **kwargs)
    return None


def post_llm_call(session_id: str, model: str = "", platform: str = "", **_: Any) -> None:
    meta = _SESSION_CONTEXT_BY_ID.get(session_id)
    if not meta:
        return None
    gateway = meta.get("gateway")
    session_key = meta.get("session_key")
    chat_id = meta.get("chat_id")
    if not gateway or not session_key or not chat_id:
        return None

    usage = extract_usage(gateway, session_key)
    if usage is None:
        return None
    used, limit, _agent = usage

    cache = load_cache(CACHE_PATH)
    sessions = cache.setdefault("sessions", {})
    record = sessions.setdefault(session_key, {})
    record.update(
        {
            "session_id": session_id,
            "platform": "slack",
            "chat_id": chat_id,
            "thread_id": meta.get("thread_id"),
            "model": model,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    notice = evaluate_notification(record, used=used, limit=limit)
    write_cache(CACHE_PATH, cache)
    if notice is None:
        return None

    adapter = meta.get("adapter")
    if adapter is None:
        # Fallback for adapter maps keyed differently from event.source.platform.
        for key, candidate in (getattr(gateway, "adapters", {}) or {}).items():
            if _platform_value(key) == "slack":
                adapter = candidate
                break
    if adapter is None:
        return None

    register_post_delivery_notice(
        adapter=adapter,
        session_key=session_key,
        chat_id=chat_id,
        thread_id=meta.get("thread_id"),
        text=notice["text"],
        loop=meta.get("loop"),
        generation=_adapter_generation(adapter, session_key),
    )
    return None


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch)
    ctx.register_hook("post_llm_call", post_llm_call)
