"""Hermes context usage threshold notifier plugin."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PLUGIN_DIR = Path(__file__).resolve().parent
CACHE_PATH = PLUGIN_DIR / "cache.json"

DEFAULT_SUPPORTED_PLATFORMS = {
    "slack",
    "telegram",
    "discord",
    "mattermost",
    "matrix",
    "whatsapp",
    "signal",
    "feishu",
    "dingtalk",
    "bluebubbles",
}

_SESSION_CONTEXT_BY_ID: dict[str, dict[str, Any]] = {}
_DELIVERY_LEDGER_BY_SESSION: dict[str, list[dict[str, Any]]] = {}
_ADAPTER_OBSERVERS: dict[int, dict[str, Any]] = {}
_CAPTURE_SEQUENCE = 0
_DELIVERY_SEQUENCE = 0
MAX_LEDGER_ENTRIES_PER_SESSION = 20
MAX_LEDGER_SESSIONS = 100
MIN_SUFFIX_CANDIDATE_CHARS = 80

_CONTEXT_NOTICE_RE = re.compile(
    r"(?m)^(?::(?:straight_ruler|warning|rotating_light):|[📏⚠️🚨])\s+Context:\s+\d+%\s+\([^)]*\)(?:,\s*.*)?$"
)
_STATUS_PREFIXES = (
    "⏳ Still working",
    "⚠️ No activity",
    "Dangerous command requires approval",
)


def record_delivery_entry(session_key: str, entry: dict[str, Any]) -> None:
    global _DELIVERY_SEQUENCE
    if not session_key:
        return
    _DELIVERY_SEQUENCE += 1
    entry["delivery_sequence"] = _DELIVERY_SEQUENCE
    entries = _DELIVERY_LEDGER_BY_SESSION.setdefault(session_key, [])
    entries.append(dict(entry))
    overflow = len(entries) - MAX_LEDGER_ENTRIES_PER_SESSION
    if overflow > 0:
        del entries[:overflow]
    session_overflow = len(_DELIVERY_LEDGER_BY_SESSION) - MAX_LEDGER_SESSIONS
    for old_session_key in list(_DELIVERY_LEDGER_BY_SESSION)[: max(0, session_overflow)]:
        if old_session_key != session_key:
            _DELIVERY_LEDGER_BY_SESSION.pop(old_session_key, None)


def normalize_delivery_text(text: Any) -> str:
    return "\n".join(str(text or "").strip().split())


def is_context_notice_text(text: Any) -> bool:
    return bool(_CONTEXT_NOTICE_RE.search(str(text or "")))


def is_obvious_status_text(text: Any) -> bool:
    stripped = str(text or "").strip()
    if not stripped:
        return True
    if is_context_notice_text(stripped) and normalize_delivery_text(stripped).startswith(":"):
        return True
    return any(stripped.startswith(prefix) for prefix in _STATUS_PREFIXES)


def _content_matches_response(content: str, assistant_response: str) -> bool:
    normalized_content = normalize_delivery_text(content)
    normalized_response = normalize_delivery_text(assistant_response)
    if not normalized_content or not normalized_response:
        return False
    return normalized_content == normalized_response


def _content_is_response_suffix(content: str, assistant_response: str) -> bool:
    normalized_content = normalize_delivery_text(content)
    normalized_response = normalize_delivery_text(assistant_response)
    if len(normalized_content) < MIN_SUFFIX_CANDIDATE_CHARS:
        return False
    return bool(normalized_response) and normalized_response.endswith(normalized_content)


def _safe_edit_entry(entry: dict[str, Any]) -> str | None:
    content = str(entry.get("content") or "")
    if not entry.get("message_id") or not entry.get("chat_id") or not content:
        return None
    if is_context_notice_text(content) or is_obvious_status_text(content):
        return None
    return content


def select_edit_candidate(
    session_key: str,
    assistant_response: str,
    notice_text: str,
    min_delivery_sequence: int | None = None,
) -> dict[str, Any] | None:
    del notice_text
    for entry in reversed(_DELIVERY_LEDGER_BY_SESSION.get(session_key, [])):
        if min_delivery_sequence is not None and int(entry.get("delivery_sequence") or 0) <= min_delivery_sequence:
            continue
        content = _safe_edit_entry(entry)
        if content is None or entry.get("split_parent"):
            continue
        if _content_matches_response(content, assistant_response) or _content_is_response_suffix(content, assistant_response):
            return entry
        return None
    return None


def _result_success(result: Any) -> bool:
    return bool(getattr(result, "success", result is not None))


def _result_message_id(result: Any, fallback: Any = None) -> str:
    return str(getattr(result, "message_id", None) or getattr(result, "ts", None) or fallback or "")


def _metadata_from_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    metadata = kwargs.get("metadata")
    if metadata is None and args:
        candidate = args[-1]
        if isinstance(candidate, dict):
            metadata = candidate
    return dict(metadata) if isinstance(metadata, dict) else {}


def _metadata_thread_id(metadata: dict[str, Any]) -> str | None:
    for key in ("thread_id", "thread_ts", "message_thread_id", "root_id"):
        value = metadata.get(key)
        if value:
            return str(value)
    return None


def _matching_session_meta(adapter: Any, chat_id: Any, metadata: dict[str, Any]) -> dict[str, Any] | None:
    chat = str(chat_id or "")
    delivery_thread = _metadata_thread_id(metadata)
    matches = []
    for meta in _SESSION_CONTEXT_BY_ID.values():
        if meta.get("adapter") is not adapter:
            continue
        if str(meta.get("chat_id") or "") != chat:
            continue
        meta_thread = str(meta.get("thread_id") or "")
        if bool(meta_thread) != bool(delivery_thread):
            continue
        if meta_thread and delivery_thread and meta_thread != delivery_thread:
            continue
        matches.append(meta)
    if not matches:
        return None
    return max(matches, key=lambda item: int(item.get("captured_at", 0) or 0))


def _ledger_entry_for_message(chat_id: Any, message_id: Any) -> tuple[str, dict[str, Any]] | None:
    chat = str(chat_id or "")
    msg = str(message_id or "")
    if not chat or not msg:
        return None
    for session_key, entries in reversed(list(_DELIVERY_LEDGER_BY_SESSION.items())):
        for entry in reversed(entries):
            if str(entry.get("chat_id") or "") == chat and str(entry.get("message_id") or "") == msg:
                return session_key, entry
    return None


def _record_delivery_from_send(adapter: Any, chat_id: Any, content: Any, args: tuple[Any, ...], kwargs: dict[str, Any], result: Any) -> None:
    if not _result_success(result) or is_obvious_status_text(content) or is_context_notice_text(content):
        return
    metadata = _metadata_from_call(args, kwargs)
    meta = _matching_session_meta(adapter, chat_id, metadata)
    if meta is None:
        return
    message_id = _result_message_id(result)
    if not message_id:
        return
    base_entry = {
        "session_key": meta["session_key"],
        "platform": meta.get("platform"),
        "chat_id": str(chat_id),
        "thread_id": _metadata_thread_id(metadata) or meta.get("thread_id"),
        "message_id": message_id,
        "content": str(content or ""),
        "metadata": metadata,
        "method": "send",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    record_delivery_entry(meta["session_key"], base_entry)


def _record_delivery_from_edit(
    adapter: Any,
    chat_id: Any,
    message_id: Any,
    content: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    result: Any,
) -> None:
    if not _result_success(result) or is_obvious_status_text(content) or is_context_notice_text(content):
        return
    metadata = _metadata_from_call(args, kwargs)
    meta = _matching_session_meta(adapter, chat_id, metadata)
    previous_entry: dict[str, Any] | None = None
    if meta is None:
        previous = _ledger_entry_for_message(chat_id, message_id)
        if previous is None:
            return
        previous_session_key, previous_entry = previous
        meta = {
            "session_key": previous_session_key,
            "platform": previous_entry.get("platform"),
            "thread_id": previous_entry.get("thread_id"),
        }
        if not metadata:
            metadata = dict(previous_entry.get("metadata") or {})
    resolved_message_id = _result_message_id(result, fallback=message_id)
    if not resolved_message_id:
        return
    record_delivery_entry(
        meta["session_key"],
        {
            "session_key": meta["session_key"],
            "platform": meta.get("platform"),
            "chat_id": str(chat_id),
            "thread_id": _metadata_thread_id(metadata) or meta.get("thread_id"),
            "message_id": resolved_message_id,
            "content": str(content or ""),
            "metadata": metadata,
            "method": "edit",
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def ensure_adapter_observer(adapter: Any) -> None:
    if adapter is None:
        return
    key = id(adapter)
    if key in _ADAPTER_OBSERVERS:
        return
    original_send = getattr(adapter, "send", None)
    if not callable(original_send):
        return
    original_edit = getattr(adapter, "edit_message", None)

    async def observed_send(chat_id: Any, content: Any, *args: Any, **kwargs: Any) -> Any:
        result = await original_send(chat_id, content, *args, **kwargs)
        try:
            _record_delivery_from_send(adapter, chat_id, content, args, kwargs, result)
        except Exception:
            pass
        return result

    setattr(adapter, "send", observed_send)
    observer = {"send": original_send}

    if callable(original_edit):
        async def observed_edit(chat_id: Any, message_id: Any, content: Any, *args: Any, **kwargs: Any) -> Any:
            result = await original_edit(chat_id, message_id, content, *args, **kwargs)
            try:
                _record_delivery_from_edit(adapter, chat_id, message_id, content, args, kwargs, result)
            except Exception:
                pass
            return result

        setattr(adapter, "edit_message", observed_edit)
        observer["edit_message"] = original_edit

    _ADAPTER_OBSERVERS[key] = observer


def _platform_value(platform: Any) -> str:
    return str(getattr(platform, "value", platform) or "").lower()


def is_slack_event(event: Any) -> bool:
    source = getattr(event, "source", None)
    return _platform_value(getattr(source, "platform", None)) == "slack"


def is_supported_event(event: Any) -> bool:
    source = getattr(event, "source", None)
    return _platform_value(getattr(source, "platform", None)) in DEFAULT_SUPPORTED_PLATFORMS


def compact_token_count(tokens: int) -> str:
    tokens = int(tokens) or 0
    if abs(tokens) >= 1_000_000:
        value = tokens / 1_000_000
        if value.is_integer():
            return f"{int(value)}M"
        return f"{value:.1f}M"
    return f"{int(round(tokens / 1000))}K"


def emoji_for_bucket(bucket: int) -> str:
    if bucket <= 65:
        return ":straight_ruler:"
    if bucket <= 85:
        return ":warning:"
    return ":rotating_light:"


def display_model_name(model: str) -> str:
    model = (model or "").strip()
    if not model:
        return ""
    return model.rsplit("/", 1)[-1]


def display_reasoning_effort(reasoning_config: Any) -> str:
    if not isinstance(reasoning_config, dict):
        return ""
    if reasoning_config.get("enabled") is False:
        return "none"
    effort = str(reasoning_config.get("effort", "") or "").strip().lower()
    return effort


def format_notice(bucket: int, used: int, limit: int, model: str = "", effort: str = "") -> str:
    text = f"{emoji_for_bucket(bucket)} Context: {int(bucket)}% ({compact_token_count(used)}/{compact_token_count(limit)})"
    display_model = display_model_name(model)
    display_effort = (effort or "").strip().lower()
    if display_model and display_effort:
        text = f"{text}, {display_model} {display_effort}"
    elif display_model:
        text = f"{text}, {display_model}"
    elif display_effort:
        text = f"{text}, {display_effort}"
    return text


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


def evaluate_notification(
    record: dict[str, Any],
    *,
    used: int,
    limit: int,
    model: str = "",
    effort: str = "",
) -> dict[str, Any] | None:
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
        "text": format_notice(bucket, used, limit, model=model, effort=effort),
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


def reasoning_effort_for_turn(agent: Any, gateway: Any, hook_reasoning_config: Any = None) -> str:
    for reasoning_config in (
        hook_reasoning_config,
        getattr(agent, "reasoning_config", None),
        getattr(gateway, "_reasoning_config", None),
    ):
        effort = display_reasoning_effort(reasoning_config)
        if effort:
            return effort
    return ""


def _adapter_for_platform(gateway: Any, platform_key: Any, platform: str) -> Any:
    adapters = getattr(gateway, "adapters", {}) or {}
    try:
        adapter = adapters.get(platform_key)
    except TypeError:
        adapter = None
    if adapter is not None:
        return adapter
    for key, candidate in adapters.items():
        if _platform_value(key) == platform:
            return candidate
    return None


def _event_metadata(event: Any) -> dict[str, Any]:
    metadata = getattr(event, "metadata", None)
    return dict(metadata) if isinstance(metadata, dict) else {}


def _thread_id_for_event(event: Any, platform: str) -> str | None:
    source = getattr(event, "source", None)
    thread_id = getattr(source, "thread_id", None)
    if thread_id:
        return str(thread_id)

    metadata = _event_metadata(event)
    for key in ("thread_id", "thread_ts", "message_thread_id", "root_id"):
        value = metadata.get(key)
        if value:
            return str(value)

    if platform == "slack":
        return getattr(event, "message_id", None)
    return None


def capture_gateway_context(event: Any, gateway: Any, session_store: Any, **_: Any) -> dict[str, Any] | None:
    global _CAPTURE_SEQUENCE
    if not is_supported_event(event):
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
    platform_key = getattr(source, "platform", None)
    platform = _platform_value(platform_key)
    adapter = _adapter_for_platform(gateway, platform_key, platform)
    ensure_adapter_observer(adapter)
    thread_id = _thread_id_for_event(event, platform)
    _CAPTURE_SEQUENCE += 1
    meta = {
        "gateway": gateway,
        "adapter": adapter,
        "session_store": session_store,
        "session_key": session_key,
        "session_id": session_id,
        "platform": platform,
        "chat_id": getattr(source, "chat_id", None),
        "thread_id": thread_id,
        "metadata": _event_metadata(event),
        "loop": loop,
        "captured_at": _CAPTURE_SEQUENCE,
        "delivery_start": _DELIVERY_SEQUENCE,
    }
    _SESSION_CONTEXT_BY_ID[session_id] = meta
    return meta


def _unwrap_callback(entry: Any) -> tuple[int | None, Callable | None]:
    if isinstance(entry, tuple) and len(entry) == 2:
        generation, callback = entry
        return generation, callback if callable(callback) else None
    return None, entry if callable(entry) else None


def notice_send_metadata(meta: dict[str, Any]) -> dict[str, Any] | None:
    original = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    metadata: dict[str, Any] = {}

    thread_id = meta.get("thread_id")
    if thread_id:
        metadata["thread_id"] = thread_id

    for key in ("message_thread_id", "thread_ts", "root_id"):
        value = original.get(key)
        if value:
            metadata[key] = value

    return metadata or None


def _send_later(adapter: Any, chat_id: str, text: str, meta: dict[str, Any], loop: asyncio.AbstractEventLoop | None) -> None:
    metadata = notice_send_metadata(meta)

    async def _send() -> None:
        try:
            await adapter.send(chat_id, text, metadata=metadata)
        except Exception:
            return

    _schedule_later(_send, loop)


def _schedule_later(coro_factory: Callable[[], Any], loop: asyncio.AbstractEventLoop | None) -> None:
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
    if loop.is_closed():
        return
    try:
        future = asyncio.run_coroutine_threadsafe(coro_factory(), loop)
    except RuntimeError:
        return

    def _consume_exception(done: Any) -> None:
        try:
            if not done.cancelled():
                done.exception()
        except Exception:
            pass

    future.add_done_callback(_consume_exception)


def adapter_may_edit(adapter: Any) -> bool:
    if getattr(adapter, "SUPPORTS_MESSAGE_EDITING", True) is False:
        return False
    return callable(getattr(adapter, "edit_message", None))


async def _call_edit_message(adapter: Any, chat_id: Any, message_id: Any, content: str, metadata: dict[str, Any] | None) -> Any:
    try:
        return await adapter.edit_message(chat_id, message_id, content, metadata=metadata)
    except TypeError as exc:
        if "metadata" not in str(exc):
            raise
        return await adapter.edit_message(chat_id, message_id, content)


def _edit_or_send_later(
    adapter: Any,
    chat_id: str,
    text: str,
    meta: dict[str, Any],
    loop: asyncio.AbstractEventLoop | None,
    session_key: str,
    assistant_response: str,
) -> None:
    if not assistant_response or not adapter_may_edit(adapter):
        _send_later(adapter, chat_id, text, meta, loop)
        return

    candidate = select_edit_candidate(
        session_key,
        assistant_response,
        text,
        min_delivery_sequence=meta.get("delivery_start"),
    )
    if candidate is None:
        _send_later(adapter, chat_id, text, meta, loop)
        return

    updated_content = str(candidate["content"]).rstrip() + "\n\n" + text
    edit_metadata = {**(notice_send_metadata(meta) or {}), **(candidate.get("metadata") or {})} or None

    async def _edit_or_fallback() -> None:
        try:
            result = await _call_edit_message(
                adapter,
                candidate["chat_id"],
                candidate["message_id"],
                updated_content,
                edit_metadata,
            )
        except Exception:
            try:
                await adapter.send(chat_id, text, metadata=notice_send_metadata(meta))
            except Exception:
                pass
            return
        if not _result_success(result):
            try:
                await adapter.send(chat_id, text, metadata=notice_send_metadata(meta))
            except Exception:
                pass
            return
        record_delivery_entry(
            session_key,
            {
                "session_key": session_key,
                "platform": meta.get("platform"),
                "chat_id": candidate["chat_id"],
                "thread_id": candidate.get("thread_id") or meta.get("thread_id"),
                "message_id": _result_message_id(result, fallback=candidate["message_id"]),
                "content": updated_content,
                "metadata": candidate.get("metadata") or {},
                "method": "edit",
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    _schedule_later(_edit_or_fallback, loop)


def register_post_delivery_notice(
    *,
    adapter: Any,
    session_key: str,
    chat_id: str,
    text: str,
    meta: dict[str, Any],
    loop: asyncio.AbstractEventLoop | None,
    generation: int | None = None,
    assistant_response: str = "",
) -> None:
    existing_entry = getattr(adapter, "_post_delivery_callbacks", {}).get(session_key)
    existing_generation, existing_callback = _unwrap_callback(existing_entry)
    effective_generation = generation if generation is not None else existing_generation

    def combined_callback() -> None:
        if callable(existing_callback):
            existing_callback()
        _edit_or_send_later(adapter, chat_id, text, meta, loop, session_key, assistant_response)

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


def post_llm_call(
    session_id: str,
    model: str = "",
    platform: str = "",
    reasoning_config: Any = None,
    assistant_response: str = "",
    **_: Any,
) -> None:
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
    used, limit, agent = usage
    effort = reasoning_effort_for_turn(agent, gateway, reasoning_config)

    cache = load_cache(CACHE_PATH)
    sessions = cache.setdefault("sessions", {})
    record = sessions.setdefault(session_key, {})
    record.update(
        {
            "session_id": session_id,
            "platform": meta.get("platform") or platform or "",
            "chat_id": chat_id,
            "thread_id": meta.get("thread_id"),
            "model": model,
            "reasoning_effort": effort,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    notice = evaluate_notification(record, used=used, limit=limit, model=model, effort=effort)
    write_cache(CACHE_PATH, cache)
    if notice is None:
        return None

    adapter = meta.get("adapter")
    if adapter is None:
        # Fallback for adapter maps keyed differently from event.source.platform.
        adapter = _adapter_for_platform(gateway, None, meta.get("platform") or platform or "")
    if adapter is None:
        return None

    register_post_delivery_notice(
        adapter=adapter,
        session_key=session_key,
        chat_id=chat_id,
        text=notice["text"],
        meta=meta,
        loop=meta.get("loop"),
        generation=_adapter_generation(adapter, session_key),
        assistant_response=assistant_response,
    )
    return None


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch)
    ctx.register_hook("post_llm_call", post_llm_call)
