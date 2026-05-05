import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_context_notifier as hcn


class DummyAdapter:
    def __init__(self):
        self.sent = []
        self._post_delivery_callbacks = {}
        self._active_sessions = {}

    async def send(self, chat_id, content, metadata=None):
        self.sent.append((chat_id, content, metadata))
        return SimpleNamespace(success=True)

    def register_post_delivery_callback(self, session_key, callback, *, generation=None):
        if generation is None:
            self._post_delivery_callbacks[session_key] = callback
        else:
            self._post_delivery_callbacks[session_key] = (generation, callback)


def test_supported_platform_allowlist_matches_planned_scope():
    assert hcn.DEFAULT_SUPPORTED_PLATFORMS == {
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
    assert not {
        "email",
        "sms",
        "wecom",
        "wecom_callback",
        "weixin",
        "qqbot",
        "yuanbao",
        "api_server",
        "webhook",
        "homeassistant",
    } & hcn.DEFAULT_SUPPORTED_PLATFORMS


def test_format_notice_uses_expected_emoji_k_values_model_suffix_and_reasoning_effort():
    assert hcn.format_notice(50, 135_000, 270_000) == ":straight_ruler: Context: 50% (135K/270K)"
    assert hcn.format_notice(65, 176_000, 270_000) == ":straight_ruler: Context: 65% (176K/270K)"
    assert hcn.format_notice(70, 189_000, 270_000) == ":warning: Context: 70% (189K/270K)"
    assert hcn.format_notice(85, 230_000, 270_000, model="gpt-5.5") == ":warning: Context: 85% (230K/270K), gpt-5.5"
    assert hcn.format_notice(85, 230_000, 270_000, model="gpt-5.5", effort="medium") == ":warning: Context: 85% (230K/270K), gpt-5.5 (medium)"
    assert hcn.format_notice(90, 243_000, 270_000) == ":rotating_light: Context: 90% (243K/270K)"


def test_display_model_name_uses_last_path_component():
    assert hcn.display_model_name("openai-codex/gpt-5.5") == "gpt-5.5"
    assert hcn.display_model_name("anthropic/claude-sonnet-4") == "claude-sonnet-4"
    assert hcn.display_model_name("") == ""


def test_display_reasoning_effort_reads_config_dict_and_none_state():
    assert hcn.display_reasoning_effort({"enabled": True, "effort": "medium"}) == "medium"
    assert hcn.display_reasoning_effort({"enabled": False}) == "none"
    assert hcn.display_reasoning_effort({"effort": ""}) == ""
    assert hcn.display_reasoning_effort(None) == ""


def test_compact_token_count_uses_millions_for_large_context_windows():
    assert hcn.compact_token_count(270_000) == "270K"
    assert hcn.compact_token_count(1_000_000) == "1M"
    assert hcn.compact_token_count(1_250_000) == "1.2M"
    assert hcn.format_notice(85, 850_000, 1_000_000, model="gpt-5.5") == ":warning: Context: 85% (850K/1M), gpt-5.5"


def test_evaluate_notification_dedupes_and_handles_bucket_jumps():
    record = {}

    notice = hcn.evaluate_notification(
        record,
        used=194_400,
        limit=270_000,
        model="openai-codex/gpt-5.5",
        effort="medium",
    )

    assert notice == {
        "bucket": 70,
        "text": ":warning: Context: 70% (194K/270K), gpt-5.5 (medium)",
        "used": 194_400,
        "limit": 270_000,
        "percent": 72.0,
    }
    assert record["last_notified_bucket"] == 70

    assert hcn.evaluate_notification(record, used=200_000, limit=270_000) is None
    assert record["last_notified_bucket"] == 70

    next_notice = hcn.evaluate_notification(record, used=204_000, limit=270_000)
    assert next_notice["bucket"] == 75
    assert record["last_notified_bucket"] == 75


def test_evaluate_notification_lowers_bucket_after_context_shrinks_without_notifying():
    record = {"last_notified_bucket": 85}

    assert hcn.evaluate_notification(record, used=94_500, limit=270_000) is None
    assert record["last_notified_bucket"] == 35

    notice = hcn.evaluate_notification(record, used=135_000, limit=270_000)
    assert notice["bucket"] == 50
    assert record["last_notified_bucket"] == 50


def test_evaluate_notification_skips_invalid_or_below_threshold():
    assert hcn.evaluate_notification({}, used=0, limit=270_000) is None
    assert hcn.evaluate_notification({}, used=135_000, limit=0) is None
    assert hcn.evaluate_notification({}, used=132_000, limit=270_000) is None


def test_cache_roundtrip_is_atomic(tmp_path):
    cache_path = tmp_path / "cache.json"
    data = {"sessions": {"s1": {"last_notified_bucket": 50}}}

    hcn.write_cache(cache_path, data)

    assert json.loads(cache_path.read_text()) == data
    assert not (tmp_path / "cache.json.tmp").exists()
    assert hcn.load_cache(cache_path) == data


def test_extract_usage_prefers_running_agent_context_compressor():
    compressor = SimpleNamespace(last_prompt_tokens=230_000, context_length=270_000)
    agent = SimpleNamespace(context_compressor=compressor)
    gateway = SimpleNamespace(_running_agents={"session": agent}, _agent_cache={})

    assert hcn.extract_usage(gateway, "session") == (230_000, 270_000, agent)


def test_extract_usage_falls_back_to_cached_agent_context_compressor():
    compressor = SimpleNamespace(last_prompt_tokens=135_000, context_length=270_000)
    agent = SimpleNamespace(context_compressor=compressor)
    gateway = SimpleNamespace(_running_agents={}, _agent_cache={"session": (agent, "sig")})

    assert hcn.extract_usage(gateway, "session") == (135_000, 270_000, agent)


def test_extract_usage_skips_when_context_compressor_unavailable():
    gateway = SimpleNamespace(_running_agents={"session": SimpleNamespace()}, _agent_cache={})

    assert hcn.extract_usage(gateway, "session") is None


def test_capture_slack_session_context_uses_session_key_and_thread_fallback():
    source = SimpleNamespace(platform=SimpleNamespace(value="slack"), chat_id="C1", thread_id=None)
    event = SimpleNamespace(source=source, message_id="177.1")
    entry = SimpleNamespace(session_key="key", session_id="sid")
    session_store = SimpleNamespace(get_or_create_session=lambda src: entry)
    gateway = SimpleNamespace()

    meta = hcn.capture_gateway_context(event=event, gateway=gateway, session_store=session_store)

    assert meta["session_key"] == "key"
    assert meta["session_id"] == "sid"
    assert meta["platform"] == "slack"
    assert meta["chat_id"] == "C1"
    assert meta["thread_id"] == "177.1"
    assert hcn._SESSION_CONTEXT_BY_ID["sid"] is meta


def test_capture_gateway_context_supports_telegram_and_keeps_thread_metadata():
    adapter = DummyAdapter()
    source = SimpleNamespace(
        platform=SimpleNamespace(value="telegram"),
        chat_id="chat-1",
        thread_id="topic-42",
    )
    event = SimpleNamespace(source=source, message_id="msg-1", metadata={"message_thread_id": "topic-42"})
    entry = SimpleNamespace(session_key="key", session_id="sid")
    session_store = SimpleNamespace(get_or_create_session=lambda src: entry)
    gateway = SimpleNamespace(adapters={"telegram": adapter})

    meta = hcn.capture_gateway_context(event=event, gateway=gateway, session_store=session_store)

    assert meta["platform"] == "telegram"
    assert meta["adapter"] is adapter
    assert meta["chat_id"] == "chat-1"
    assert meta["thread_id"] == "topic-42"
    assert meta["metadata"] == {"message_thread_id": "topic-42"}


def test_capture_gateway_context_falls_back_when_adapter_key_is_unhashable():
    adapter = DummyAdapter()
    source = SimpleNamespace(
        platform=SimpleNamespace(value="discord"),
        chat_id="thread-channel-1",
        thread_id=None,
    )
    event = SimpleNamespace(source=source, message_id="msg-1")
    entry = SimpleNamespace(session_key="key", session_id="sid")
    session_store = SimpleNamespace(get_or_create_session=lambda src: entry)
    gateway = SimpleNamespace(adapters={"discord": adapter})

    meta = hcn.capture_gateway_context(event=event, gateway=gateway, session_store=session_store)

    assert meta["platform"] == "discord"
    assert meta["adapter"] is adapter
    assert meta["chat_id"] == "thread-channel-1"


def test_capture_gateway_context_ignores_unsupported_platform():
    source = SimpleNamespace(platform=SimpleNamespace(value="email"), chat_id="C1", thread_id=None)
    event = SimpleNamespace(source=source, message_id="m1")
    session_store = SimpleNamespace(get_or_create_session=lambda src: pytest.fail("should not create session"))

    assert hcn.capture_gateway_context(event=event, gateway=SimpleNamespace(), session_store=session_store) is None


def test_notice_send_metadata_preserves_thread_context():
    assert hcn.notice_send_metadata({"platform": "slack", "thread_id": "177.1", "metadata": {}}) == {"thread_id": "177.1"}
    assert hcn.notice_send_metadata({"platform": "telegram", "thread_id": "42", "metadata": {"message_thread_id": "42"}}) == {"thread_id": "42", "message_thread_id": "42"}
    assert hcn.notice_send_metadata({"platform": "signal", "thread_id": None, "metadata": {}}) is None


def test_post_llm_call_records_non_slack_platform_and_registers_notice(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr(hcn, "CACHE_PATH", cache_path)
    adapter = DummyAdapter()
    compressor = SimpleNamespace(last_prompt_tokens=194_400, context_length=270_000)
    agent = SimpleNamespace(context_compressor=compressor, reasoning_config={"enabled": True, "effort": "medium"})
    gateway = SimpleNamespace(_running_agents={"session": agent}, _agent_cache={}, adapters={"telegram": adapter})
    hcn._SESSION_CONTEXT_BY_ID["sid"] = {
        "gateway": gateway,
        "adapter": adapter,
        "session_key": "session",
        "session_id": "sid",
        "platform": "telegram",
        "chat_id": "chat-1",
        "thread_id": "42",
        "metadata": {"message_thread_id": "42"},
        "loop": None,
    }

    hcn.post_llm_call(session_id="sid", model="openai-codex/gpt-5.5", platform="telegram")

    data = json.loads(cache_path.read_text())
    record = data["sessions"]["session"]
    assert record["platform"] == "telegram"
    assert record["thread_id"] == "42"
    assert record["model"] == "openai-codex/gpt-5.5"
    assert record["reasoning_effort"] == "medium"
    assert "session" in adapter._post_delivery_callbacks

def test_register_post_delivery_notice_chains_existing_callback_and_sends_after_it():
    adapter = DummyAdapter()
    order = []
    adapter._post_delivery_callbacks["session"] = lambda: order.append("existing")

    source = SimpleNamespace(platform=SimpleNamespace(value="slack"), chat_id="C1")
    loop = asyncio.new_event_loop()
    try:
        hcn.register_post_delivery_notice(
            adapter=adapter,
            session_key="session",
            chat_id="C1",
            meta={"platform": "slack", "thread_id": "T1", "metadata": {}},
            text=":warning: Context: 85% (230K/270K)",
            loop=loop,
            generation=None,
        )
        cb = adapter._post_delivery_callbacks["session"]
        cb()
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()

    assert order == ["existing"]
    assert adapter.sent == [("C1", ":warning: Context: 85% (230K/270K)", {"thread_id": "T1"})]


def test_register_post_delivery_notice_preserves_existing_generation_tuple():
    adapter = DummyAdapter()
    order = []
    adapter._post_delivery_callbacks["session"] = (7, lambda: order.append("existing"))
    loop = asyncio.new_event_loop()
    try:
        hcn.register_post_delivery_notice(
            adapter=adapter,
            session_key="session",
            chat_id="C1",
            meta={"platform": "slack", "thread_id": None, "metadata": {}},
            text=":warning: Context: 85% (230K/270K)",
            loop=loop,
            generation=7,
        )
        generation, cb = adapter._post_delivery_callbacks["session"]
        assert generation == 7
        cb()
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()

    assert order == ["existing"]
    assert adapter.sent == [("C1", ":warning: Context: 85% (230K/270K)", None)]
