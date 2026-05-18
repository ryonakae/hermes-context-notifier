import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_context_notifier as hcn


@pytest.fixture(autouse=True)
def clear_plugin_globals():
    hcn._SESSION_CONTEXT_BY_ID.clear()
    if hasattr(hcn, "_DELIVERY_LEDGER_BY_SESSION"):
        hcn._DELIVERY_LEDGER_BY_SESSION.clear()
    if hasattr(hcn, "_ADAPTER_OBSERVERS"):
        hcn._ADAPTER_OBSERVERS.clear()


class DummyAdapter:
    def __init__(self):
        self.sent = []
        self.edits = []
        self._post_delivery_callbacks = {}
        self._active_sessions = {}

    async def send(self, chat_id, content, metadata=None):
        self.sent.append((chat_id, content, metadata))
        return SimpleNamespace(success=True, message_id=f"m{len(self.sent)}")

    async def edit_message(self, chat_id, message_id, content, metadata=None):
        self.edits.append((chat_id, message_id, content, metadata))
        return SimpleNamespace(success=True, message_id=message_id)

    def register_post_delivery_callback(self, session_key, callback, *, generation=None):
        if generation is None:
            self._post_delivery_callbacks[session_key] = callback
        else:
            self._post_delivery_callbacks[session_key] = (generation, callback)


class SlackLikeAdapter(DummyAdapter):
    async def edit_message(self, chat_id, message_id, content, *, finalize=False):
        self.edits.append((chat_id, message_id, content, finalize))
        return SimpleNamespace(success=True, message_id=message_id)


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
    assert hcn.format_notice(85, 230_000, 270_000, model="gpt-5.5", effort="medium") == ":warning: Context: 85% (230K/270K), gpt-5.5 medium"
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
        "text": ":warning: Context: 70% (194K/270K), gpt-5.5 medium",
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


def test_delivery_ledger_records_and_prunes_entries(monkeypatch):
    monkeypatch.setattr(hcn, "MAX_LEDGER_ENTRIES_PER_SESSION", 2)

    hcn.record_delivery_entry("session", {"message_id": "1", "content": "old"})
    hcn.record_delivery_entry("session", {"message_id": "2", "content": "mid"})
    hcn.record_delivery_entry("session", {"message_id": "3", "content": "new"})

    assert [entry["message_id"] for entry in hcn._DELIVERY_LEDGER_BY_SESSION["session"]] == ["2", "3"]


def test_delivery_ledger_prunes_old_sessions(monkeypatch):
    monkeypatch.setattr(hcn, "MAX_LEDGER_SESSIONS", 2)

    hcn.record_delivery_entry("s1", {"message_id": "1", "content": "one"})
    hcn.record_delivery_entry("s2", {"message_id": "2", "content": "two"})
    hcn.record_delivery_entry("s3", {"message_id": "3", "content": "three"})

    assert list(hcn._DELIVERY_LEDGER_BY_SESSION) == ["s2", "s3"]


def test_select_edit_candidate_prefers_exact_assistant_response_match():
    hcn.record_delivery_entry("session", {"chat_id": "C1", "message_id": "old", "content": "related but not final"})
    hcn.record_delivery_entry("session", {"chat_id": "C1", "message_id": "final", "content": "Final answer"})

    candidate = hcn.select_edit_candidate("session", "Final answer", ":warning: Context: 85% (230K/270K)")

    assert candidate["message_id"] == "final"


def test_select_edit_candidate_skips_any_existing_context_notice():
    hcn.record_delivery_entry(
        "session",
        {
            "chat_id": "C1",
            "message_id": "m1",
            "content": "Final answer\n\n:straight_ruler: Context: 50% (135K/270K)",
        },
    )

    candidate = hcn.select_edit_candidate("session", "Final answer", ":warning: Context: 85% (230K/270K)")

    assert candidate is None


def test_select_edit_candidate_skips_progress_and_status_entries():
    hcn.record_delivery_entry("session", {"chat_id": "C1", "message_id": "m1", "content": "⏳ Still working..."})
    hcn.record_delivery_entry("session", {"chat_id": "C1", "message_id": "m2", "content": "Dangerous command requires approval"})

    candidate = hcn.select_edit_candidate("session", "Final answer", ":warning: Context: 85% (230K/270K)")

    assert candidate is None


def test_select_edit_candidate_returns_none_when_only_non_matching_entries_exist():
    hcn.record_delivery_entry("session", {"chat_id": "C1", "message_id": "m1", "content": "Unrelated text"})

    candidate = hcn.select_edit_candidate("session", "Final answer", ":warning: Context: 85% (230K/270K)")

    assert candidate is None


def test_select_edit_candidate_rejects_short_prefix_match():
    hcn.record_delivery_entry("session", {"chat_id": "C1", "message_id": "m1", "content": "Final"})

    candidate = hcn.select_edit_candidate("session", "Final answer", ":warning: Context: 85% (230K/270K)")

    assert candidate is None


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
    assert id(adapter) in hcn._ADAPTER_OBSERVERS


def test_capture_gateway_context_installs_adapter_observer_idempotently():
    adapter = DummyAdapter()
    source = SimpleNamespace(platform=SimpleNamespace(value="slack"), chat_id="C1", thread_id="T1")
    event = SimpleNamespace(source=source, message_id="msg-1", metadata={"thread_ts": "T1"})
    entry = SimpleNamespace(session_key="key", session_id="sid")
    session_store = SimpleNamespace(get_or_create_session=lambda src: entry)
    gateway = SimpleNamespace(adapters={"slack": adapter})

    hcn.capture_gateway_context(event=event, gateway=gateway, session_store=session_store)
    first_send = adapter.send
    hcn.capture_gateway_context(event=event, gateway=gateway, session_store=session_store)

    assert adapter.send is first_send
    assert len(hcn._ADAPTER_OBSERVERS) == 1


@pytest.mark.asyncio
async def test_observed_send_and_edit_return_original_results_and_record_matching_session():
    adapter = DummyAdapter()
    source = SimpleNamespace(platform=SimpleNamespace(value="slack"), chat_id="C1", thread_id="T1")
    event = SimpleNamespace(source=source, message_id="msg-1", metadata={"thread_ts": "T1"})
    entry = SimpleNamespace(session_key="key", session_id="sid")
    session_store = SimpleNamespace(get_or_create_session=lambda src: entry)
    gateway = SimpleNamespace(adapters={"slack": adapter})
    hcn.capture_gateway_context(event=event, gateway=gateway, session_store=session_store)

    send_result = await adapter.send("C1", "Final answer", metadata={"thread_ts": "T1"})
    edit_result = await adapter.edit_message("C1", send_result.message_id, "Final answer edited")

    assert send_result.message_id == "m1"
    assert edit_result.message_id == "m1"
    assert [(e["method"], e["message_id"], e["content"]) for e in hcn._DELIVERY_LEDGER_BY_SESSION["key"]] == [
        ("send", "m1", "Final answer"),
        ("edit", "m1", "Final answer edited"),
    ]
    assert hcn._DELIVERY_LEDGER_BY_SESSION["key"][-1]["metadata"] == {"thread_ts": "T1"}


@pytest.mark.asyncio
async def test_observer_skips_failed_status_context_notice_and_unmatched_deliveries():
    class FailingAdapter(DummyAdapter):
        async def send(self, chat_id, content, metadata=None):
            self.sent.append((chat_id, content, metadata))
            return SimpleNamespace(success=False, message_id="failed")

    adapter = FailingAdapter()
    source = SimpleNamespace(platform=SimpleNamespace(value="slack"), chat_id="C1", thread_id="T1")
    event = SimpleNamespace(source=source, message_id="msg-1", metadata={"thread_ts": "T1"})
    entry = SimpleNamespace(session_key="key", session_id="sid")
    session_store = SimpleNamespace(get_or_create_session=lambda src: entry)
    gateway = SimpleNamespace(adapters={"slack": adapter})
    hcn.capture_gateway_context(event=event, gateway=gateway, session_store=session_store)

    await adapter.send("C1", "Final answer", metadata={"thread_ts": "T1"})
    await adapter.edit_message("C1", "m1", "⏳ Still working...", metadata={"thread_ts": "T1"})
    await adapter.edit_message("C1", "m1", ":warning: Context: 85% (230K/270K)", metadata={"thread_ts": "T1"})
    await adapter.edit_message("C2", "m2", "Other chat", metadata={"thread_ts": "T1"})

    assert hcn._DELIVERY_LEDGER_BY_SESSION.get("key") is None


@pytest.mark.asyncio
async def test_observer_requires_thread_metadata_when_session_has_thread():
    adapter = DummyAdapter()
    source = SimpleNamespace(platform=SimpleNamespace(value="slack"), chat_id="C1", thread_id="T1")
    event = SimpleNamespace(source=source, message_id="msg-1", metadata={"thread_ts": "T1"})
    entry = SimpleNamespace(session_key="key", session_id="sid")
    session_store = SimpleNamespace(get_or_create_session=lambda src: entry)
    gateway = SimpleNamespace(adapters={"slack": adapter})
    hcn.capture_gateway_context(event=event, gateway=gateway, session_store=session_store)

    await adapter.send("C1", "Final answer")

    assert hcn._DELIVERY_LEDGER_BY_SESSION.get("key") is None


@pytest.mark.asyncio
async def test_observer_recording_errors_do_not_change_send_result(monkeypatch):
    adapter = DummyAdapter()
    source = SimpleNamespace(platform=SimpleNamespace(value="slack"), chat_id="C1", thread_id="T1")
    event = SimpleNamespace(source=source, message_id="msg-1", metadata={"thread_ts": "T1"})
    entry = SimpleNamespace(session_key="key", session_id="sid")
    session_store = SimpleNamespace(get_or_create_session=lambda src: entry)
    gateway = SimpleNamespace(adapters={"slack": adapter})
    hcn.capture_gateway_context(event=event, gateway=gateway, session_store=session_store)

    def boom(*args, **kwargs):
        raise RuntimeError("ledger failed")

    monkeypatch.setattr(hcn, "_record_delivery_from_send", boom)

    result = await adapter.send("C1", "Final answer", metadata={"thread_ts": "T1"})

    assert result.message_id == "m1"


def test_register_post_delivery_notice_ignores_closed_loop_without_raising():
    adapter = DummyAdapter()
    loop = asyncio.new_event_loop()
    loop.close()

    hcn.register_post_delivery_notice(
        adapter=adapter,
        session_key="session",
        chat_id="C1",
        meta={"platform": "slack", "thread_id": None, "metadata": {}},
        text=":warning: Context: 85% (230K/270K)",
        loop=loop,
        generation=None,
        assistant_response="",
    )

    adapter._post_delivery_callbacks["session"]()
    assert adapter.sent == []


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


def test_register_post_delivery_notice_edits_matching_candidate_without_side_message():
    adapter = SlackLikeAdapter()
    loop = asyncio.new_event_loop()
    hcn.record_delivery_entry(
        "session",
        {"chat_id": "C1", "message_id": "m1", "content": "Final answer", "metadata": {"foo": "bar"}},
    )
    try:
        hcn.register_post_delivery_notice(
            adapter=adapter,
            session_key="session",
            chat_id="C1",
            meta={"platform": "slack", "thread_id": "T1", "metadata": {}},
            text=":warning: Context: 85% (230K/270K)",
            loop=loop,
            generation=None,
            assistant_response="Final answer",
        )
        adapter._post_delivery_callbacks["session"]()
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()

    assert adapter.edits == [("C1", "m1", "Final answer\n\n:warning: Context: 85% (230K/270K)", False)]
    assert adapter.sent == []


def test_register_post_delivery_notice_falls_back_when_edit_fails():
    class FailingEditAdapter(DummyAdapter):
        async def edit_message(self, chat_id, message_id, content, metadata=None):
            self.edits.append((chat_id, message_id, content, metadata))
            return SimpleNamespace(success=False, message_id=message_id)

    adapter = FailingEditAdapter()
    loop = asyncio.new_event_loop()
    hcn.record_delivery_entry("session", {"chat_id": "C1", "message_id": "m1", "content": "Final answer"})
    try:
        hcn.register_post_delivery_notice(
            adapter=adapter,
            session_key="session",
            chat_id="C1",
            meta={"platform": "slack", "thread_id": "T1", "metadata": {}},
            text=":warning: Context: 85% (230K/270K)",
            loop=loop,
            generation=None,
            assistant_response="Final answer",
        )
        adapter._post_delivery_callbacks["session"]()
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()

    assert adapter.edits
    assert adapter.sent == [("C1", ":warning: Context: 85% (230K/270K)", {"thread_id": "T1"})]


def test_register_post_delivery_notice_falls_back_when_adapter_is_not_editable():
    adapter = DummyAdapter()
    adapter.SUPPORTS_MESSAGE_EDITING = False
    loop = asyncio.new_event_loop()
    hcn.record_delivery_entry("session", {"chat_id": "C1", "message_id": "m1", "content": "Final answer"})
    try:
        hcn.register_post_delivery_notice(
            adapter=adapter,
            session_key="session",
            chat_id="C1",
            meta={"platform": "slack", "thread_id": None, "metadata": {}},
            text=":warning: Context: 85% (230K/270K)",
            loop=loop,
            generation=None,
            assistant_response="Final answer",
        )
        adapter._post_delivery_callbacks["session"]()
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()

    assert adapter.edits == []
    assert adapter.sent == [("C1", ":warning: Context: 85% (230K/270K)", None)]


def test_register_post_delivery_notice_falls_back_when_assistant_response_missing():
    adapter = DummyAdapter()
    loop = asyncio.new_event_loop()
    hcn.record_delivery_entry("session", {"chat_id": "C1", "message_id": "m1", "content": "Final answer"})
    try:
        hcn.register_post_delivery_notice(
            adapter=adapter,
            session_key="session",
            chat_id="C1",
            meta={"platform": "slack", "thread_id": None, "metadata": {}},
            text=":warning: Context: 85% (230K/270K)",
            loop=loop,
            generation=None,
            assistant_response="",
        )
        adapter._post_delivery_callbacks["session"]()
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()

    assert adapter.edits == []
    assert adapter.sent == [("C1", ":warning: Context: 85% (230K/270K)", None)]


@pytest.mark.asyncio
async def test_non_streaming_flow_appends_notice_by_editing_final_message(tmp_path, monkeypatch):
    monkeypatch.setattr(hcn, "CACHE_PATH", tmp_path / "cache.json")
    adapter = DummyAdapter()
    source = SimpleNamespace(platform=SimpleNamespace(value="slack"), chat_id="C1", thread_id="T1")
    event = SimpleNamespace(source=source, message_id="msg-1", metadata={"thread_ts": "T1"})
    entry = SimpleNamespace(session_key="session", session_id="sid")
    session_store = SimpleNamespace(get_or_create_session=lambda src: entry)
    compressor = SimpleNamespace(last_prompt_tokens=230_000, context_length=270_000)
    agent = SimpleNamespace(context_compressor=compressor, reasoning_config={"enabled": True, "effort": "medium"})
    gateway = SimpleNamespace(_running_agents={"session": agent}, _agent_cache={}, adapters={"slack": adapter})
    assistant_response = "Final answer"

    hcn.capture_gateway_context(event=event, gateway=gateway, session_store=session_store)
    send_result = await adapter.send("C1", assistant_response, metadata={"thread_ts": "T1"})
    assert send_result.message_id == "m1"

    hcn.post_llm_call(session_id="sid", model="openai-codex/gpt-5.5", platform="slack", assistant_response=assistant_response)
    adapter._post_delivery_callbacks["session"]()
    await asyncio.sleep(0.01)

    assert adapter.edits[-1] == (
        "C1",
        "m1",
        "Final answer\n\n:warning: Context: 85% (230K/270K), gpt-5.5 medium",
        {"thread_id": "T1", "thread_ts": "T1"},
    )
    assert len(adapter.sent) == 1


@pytest.mark.asyncio
async def test_streaming_like_flow_edits_last_finalized_message(tmp_path, monkeypatch):
    monkeypatch.setattr(hcn, "CACHE_PATH", tmp_path / "cache.json")
    adapter = DummyAdapter()
    source = SimpleNamespace(platform=SimpleNamespace(value="slack"), chat_id="C1", thread_id="T1")
    event = SimpleNamespace(source=source, message_id="msg-1", metadata={"thread_ts": "T1"})
    entry = SimpleNamespace(session_key="session", session_id="sid")
    session_store = SimpleNamespace(get_or_create_session=lambda src: entry)
    compressor = SimpleNamespace(last_prompt_tokens=230_000, context_length=270_000)
    agent = SimpleNamespace(context_compressor=compressor, reasoning_config={"enabled": True, "effort": "medium"})
    gateway = SimpleNamespace(_running_agents={"session": agent}, _agent_cache={}, adapters={"slack": adapter})

    hcn.capture_gateway_context(event=event, gateway=gateway, session_store=session_store)
    send_result = await adapter.send("C1", "Partial answer", metadata={"thread_ts": "T1"})
    await adapter.edit_message("C1", send_result.message_id, "Final answer")

    hcn.post_llm_call(session_id="sid", model="gpt-5.5", platform="slack", assistant_response="Final answer")
    adapter._post_delivery_callbacks["session"]()
    await asyncio.sleep(0.01)

    assert adapter.edits[-1] == (
        "C1",
        "m1",
        "Final answer\n\n:warning: Context: 85% (230K/270K), gpt-5.5 medium",
        {"thread_id": "T1", "thread_ts": "T1"},
    )
    assert [entry["content"] for entry in hcn._DELIVERY_LEDGER_BY_SESSION["session"]].count(
        "Final answer\n\n:warning: Context: 85% (230K/270K), gpt-5.5 medium"
    ) == 1


@pytest.mark.asyncio
async def test_realistic_flow_falls_back_to_side_message_when_edit_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(hcn, "CACHE_PATH", tmp_path / "cache.json")

    class FailingEditAdapter(DummyAdapter):
        async def edit_message(self, chat_id, message_id, content, metadata=None):
            self.edits.append((chat_id, message_id, content, metadata))
            return SimpleNamespace(success=False, message_id=message_id)

    adapter = FailingEditAdapter()
    source = SimpleNamespace(platform=SimpleNamespace(value="telegram"), chat_id="chat-1", thread_id="topic-42")
    event = SimpleNamespace(source=source, message_id="msg-1", metadata={"message_thread_id": "topic-42"})
    entry = SimpleNamespace(session_key="session", session_id="sid")
    session_store = SimpleNamespace(get_or_create_session=lambda src: entry)
    compressor = SimpleNamespace(last_prompt_tokens=230_000, context_length=270_000)
    agent = SimpleNamespace(context_compressor=compressor, reasoning_config={"enabled": True, "effort": "medium"})
    gateway = SimpleNamespace(_running_agents={"session": agent}, _agent_cache={}, adapters={"telegram": adapter})

    hcn.capture_gateway_context(event=event, gateway=gateway, session_store=session_store)
    await adapter.send("chat-1", "Final answer", metadata={"message_thread_id": "topic-42"})
    hcn.post_llm_call(session_id="sid", model="gpt-5.5", platform="telegram", assistant_response="Final answer")
    adapter._post_delivery_callbacks["session"]()
    await asyncio.sleep(0.01)

    assert adapter.sent[-1] == (
        "chat-1",
        ":warning: Context: 85% (230K/270K), gpt-5.5 medium",
        {"thread_id": "topic-42", "message_thread_id": "topic-42"},
    )
