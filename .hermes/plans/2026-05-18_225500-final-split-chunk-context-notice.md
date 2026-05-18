# Final Split Chunk Context Notice Implementation Plan

> **For Hermes:** Implement this plan with TDD. Do not modify Hermes core in the immediate slice. Use independent review before commit.

**Goal:** When a context notice fires after a long assistant response, append it to the final editable platform message whenever the final chunk can be identified safely; otherwise fall back to a side-message without risking wrong-message edits.

**Architecture:** Keep `hermes-context-notifier` plugin-only for the first slice. Improve the in-memory delivery ledger so adapter-internal splits are represented as safe final-chunk candidates only for platforms/adapters where the returned `message_id` is known to be the last visible chunk. Treat unknown or first-id split adapters as unsafe and fall back. Track a follow-up core/adapter contract plan for a future `delivery_parts`/continuation-id standard instead of adding platform history lookups.

**Tech Stack:** Python 3.11, Hermes gateway plugin hooks (`pre_gateway_dispatch`, `post_llm_call`), Hermes platform adapters, pytest.

---

## Current Findings

### What already works

- Normal short/medium Slack replies can be edited successfully: the 50% notice was appended to the prior assistant message in live Slack.
- Stream-consumer-created final chunks can be selected when the plugin sees each chunk as a separate `adapter.send()` / `adapter.edit_message()` call.
- `delivery_sequence > delivery_start` prevents stale prior-turn ledger entries from being edited.

### Main gap

Adapter-internal splitting is opaque to the plugin observer. The observer sees one `adapter.send(chat_id, full_content, ...)` call, but the adapter may have sent multiple platform messages internally.

Platform behavior relevant to this plan:

| Platform | Internal split | Editing | Returned `message_id` after split | Immediate plugin behavior |
|---|---:|---:|---|---|
| Slack | yes | yes | last chunk | Can safely derive final chunk from `truncate_message()` and edit last chunk |
| Mattermost | yes | yes | last chunk | Same class as Slack |
| Matrix | yes | yes | last chunk | Same class as Slack |
| WhatsApp | yes | yes | last chunk | Same class as Slack, bridge reliability caveat |
| Feishu | yes | yes | last chunk | Same class as Slack, format caveat |
| Telegram | send/edit split | yes | send path varies; edit overflow exposes `continuation_message_ids` | Use `continuation_message_ids` when present; otherwise conservative fallback |
| Discord | yes | yes | first chunk | Do not edit internal-split exact matches in plugin-only slice |
| Signal | no safe edit | no | none | fallback only |
| BlueBubbles/iMessage | split but no edit | no | last guid | fallback only |
| DingTalk | mode-dependent | conditional | webhook synthetic/card out_track_id | fallback in plugin-only slice unless separately validated |

### Non-goals for this immediate plan

- No platform history API lookup.
- No Hermes core changes.
- No broad enablement for unsupported platforms.
- No attempt to infer final chunk for Discord first-id split results.
- No mutation of persisted `cache.json` with message bodies.

---

## Immediate Plugin-Only Plan

### Task 1: Add adapter-internal split classification helpers

**Objective:** Identify when a recorded delivery represents a single adapter call that internally split a long message, and classify whether its returned `message_id` can be treated as the final visible chunk.

**Files:**
- Modify: `hermes_context_notifier.py`
- Test: `tests/test_context_notifier.py`

**Step 1: Write failing tests**

Add tests for a pure helper, names may be adjusted during implementation:

```python
def test_split_chunks_for_adapter_uses_adapter_limit_and_formatting():
    adapter = SimpleNamespace(
        MAX_MESSAGE_LENGTH=20,
        truncate_message=lambda text, limit: [text[:limit], text[limit:]],
    )

    chunks = hcn.split_chunks_for_adapter(adapter, "x" * 30)

    assert chunks == ["x" * 20, "x" * 10]


def test_platform_returns_last_split_message_id_scope():
    assert hcn.platform_returns_last_split_message_id("slack") is True
    assert hcn.platform_returns_last_split_message_id("mattermost") is True
    assert hcn.platform_returns_last_split_message_id("matrix") is True
    assert hcn.platform_returns_last_split_message_id("whatsapp") is True
    assert hcn.platform_returns_last_split_message_id("feishu") is True
    assert hcn.platform_returns_last_split_message_id("discord") is False
    assert hcn.platform_returns_last_split_message_id("telegram") is False
```

**Step 2: Run tests to verify failure**

```bash
python -m pytest tests/test_context_notifier.py::test_split_chunks_for_adapter_uses_adapter_limit_and_formatting tests/test_context_notifier.py::test_platform_returns_last_split_message_id_scope -q
```

Expected: FAIL because helpers do not exist.

**Step 3: Implement minimal helpers**

Add constants/helpers near the ledger functions:

```python
LAST_ID_INTERNAL_SPLIT_PLATFORMS = {
    "slack",
    "mattermost",
    "matrix",
    "whatsapp",
    "feishu",
}


def platform_returns_last_split_message_id(platform: str) -> bool:
    return str(platform or "").lower() in LAST_ID_INTERNAL_SPLIT_PLATFORMS


def split_chunks_for_adapter(adapter: Any, content: str) -> list[str]:
    text = str(content or "")
    truncate = getattr(adapter, "truncate_message", None)
    limit = int(getattr(adapter, "MAX_MESSAGE_LENGTH", 0) or 0)
    if not callable(truncate) or limit <= 0 or not text:
        return [text] if text else []
    format_message = getattr(adapter, "format_message", None)
    if callable(format_message):
        try:
            text = str(format_message(text) or "")
        except Exception:
            return [str(content or "")] if content else []

    kwargs = {}
    len_fn = getattr(adapter, "message_len_fn", None)
    if callable(len_fn):
        kwargs["len_fn"] = len_fn
    platform_name = str(getattr(adapter, "name", "") or "").lower()
    if "telegram" in platform_name:
        try:
            from gateway.platforms.telegram import utf16_len
            kwargs["len_fn"] = utf16_len
        except Exception:
            return [text]

    try:
        chunks = truncate(text, limit, **kwargs)
    except TypeError:
        try:
            chunks = truncate(text, max_length=limit, **kwargs)
        except TypeError:
            chunks = truncate(text, limit)
    except Exception:
        return [text]
    return [str(chunk) for chunk in chunks if str(chunk or "")]
```

Keep the helper dependency-free and tolerant: if adapter-specific splitting cannot be reproduced, return `[text]` and avoid special handling. Important: mirror the adapter send path as closely as possible by applying `format_message()` before `truncate_message()`. For Telegram, use UTF-16 length semantics when available; otherwise treat reconstructed Telegram split content as unsafe and fall back rather than guessing chunk boundaries.

**Step 4: Verify pass**

```bash
python -m pytest tests/test_context_notifier.py::test_split_chunks_for_adapter_uses_adapter_limit_and_formatting tests/test_context_notifier.py::test_platform_returns_last_split_message_id_scope -q
```

Expected: PASS.

---

### Task 2: Record safe final chunk content for last-id internal splits

**Objective:** When `adapter.send()` internally split a long message and the platform is known to return the last chunk id, record the last chunk content in the ledger so the notice edits `last_chunk + notice`, not `full_response + notice`.

**Files:**
- Modify: `hermes_context_notifier.py:_record_delivery_from_send`
- Test: `tests/test_context_notifier.py`

**Step 1: Write failing test for Slack-like last-id split**

This unit test intentionally uses a simplified `truncate_message()` mock. It verifies plugin ledger behavior, not Hermes' real chunking algorithm. Real adapter chunking and chunk indicators are covered by manual gateway validation and, if needed, future core adapter tests.

```python
@pytest.mark.asyncio
async def test_observed_send_records_last_chunk_for_last_id_internal_split():
    class SlackSplitAdapter(DummyAdapter):
        MAX_MESSAGE_LENGTH = 20

        def truncate_message(self, content, max_length, **_):
            return [content[:max_length], content[max_length:]]

        async def send(self, chat_id, content, metadata=None):
            self.sent.append((chat_id, content, metadata))
            return SimpleNamespace(success=True, message_id="last")

    adapter = SlackSplitAdapter()
    source = SimpleNamespace(platform=SimpleNamespace(value="slack"), chat_id="C1", thread_id="T1")
    event = SimpleNamespace(source=source, message_id="msg-1", metadata={"thread_ts": "T1"})
    entry = SimpleNamespace(session_key="key", session_id="sid")
    session_store = SimpleNamespace(get_or_create_session=lambda src: entry)
    gateway = SimpleNamespace(adapters={"slack": adapter})
    hcn.capture_gateway_context(event=event, gateway=gateway, session_store=session_store)

    await adapter.send("C1", "a" * 30, metadata={"thread_ts": "T1"})

    recorded = hcn._DELIVERY_LEDGER_BY_SESSION["key"][-1]
    assert recorded["message_id"] == "last"
    assert recorded["content"] == "a" * 10
    assert recorded["method"] == "send_split_tail"
    assert recorded["split_chunks"] == 2
```

**Step 2: Run to verify failure**

```bash
python -m pytest tests/test_context_notifier.py::test_observed_send_records_last_chunk_for_last_id_internal_split -q
```

Expected: FAIL because the full content is recorded today.

**Step 3: Implement minimal code**

In `_record_delivery_from_send()`, after metadata/session matching and message id resolution:

```python
    platform = str(meta.get("platform") or "").lower()
    entry_content = str(content or "")
    method = "send"
    chunks = split_chunks_for_adapter(adapter, entry_content)
    if len(chunks) > 1:
        if platform_returns_last_split_message_id(platform):
            entry_content = chunks[-1]
            method = "send_split_tail"
        else:
            method = "send_split_unsafe"
```

Then store `entry_content`, `method`, and optional split metadata:

```python
        "content": entry_content,
        "method": method,
        "split_chunks": len(chunks) if len(chunks) > 1 else None,
        "split_original_length": len(str(content or "")) if len(chunks) > 1 else None,
```

Do not persist raw full split bodies outside the in-memory ledger.

**Step 4: Verify pass**

```bash
python -m pytest tests/test_context_notifier.py::test_observed_send_records_last_chunk_for_last_id_internal_split -q
```

Expected: PASS.

---

### Task 3: Reject unsafe adapter-internal split entries

**Objective:** Avoid wrong-message edits for Discord-like adapters that return the first chunk id after internal split.

**Files:**
- Modify: `hermes_context_notifier.py:_safe_edit_entry` or `select_edit_candidate`
- Test: `tests/test_context_notifier.py`

**Step 1: Write failing test**

```python
@pytest.mark.asyncio
async def test_discord_like_internal_split_falls_back_instead_of_editing_first_chunk(tmp_path, monkeypatch):
    monkeypatch.setattr(hcn, "CACHE_PATH", tmp_path / "cache.json")

    class DiscordSplitAdapter(DummyAdapter):
        MAX_MESSAGE_LENGTH = 20

        def truncate_message(self, content, max_length, **_):
            return [content[:max_length], content[max_length:]]

        async def send(self, chat_id, content, metadata=None):
            self.sent.append((chat_id, content, metadata))
            return SimpleNamespace(success=True, message_id="first")

    adapter = DiscordSplitAdapter()
    source = SimpleNamespace(platform=SimpleNamespace(value="discord"), chat_id="C1", thread_id="T1")
    event = SimpleNamespace(source=source, message_id="msg-1", metadata={"thread_id": "T1"})
    entry = SimpleNamespace(session_key="session", session_id="sid")
    session_store = SimpleNamespace(get_or_create_session=lambda src: entry)
    compressor = SimpleNamespace(last_prompt_tokens=158_000, context_length=272_000)
    agent = SimpleNamespace(context_compressor=compressor, reasoning_config={"enabled": True, "effort": "medium"})
    gateway = SimpleNamespace(_running_agents={"session": agent}, _agent_cache={}, adapters={"discord": adapter})
    full_response = "a" * 30

    hcn.capture_gateway_context(event=event, gateway=gateway, session_store=session_store)
    await adapter.send("C1", full_response, metadata={"thread_id": "T1"})
    hcn.post_llm_call(session_id="sid", model="gpt-5.5", platform="discord", assistant_response=full_response)
    adapter._post_delivery_callbacks["session"]()
    await asyncio.sleep(0.01)

    assert adapter.edits == []
    assert adapter.sent[-1][1] == ":straight_ruler: Context: 55% (158K/272K), gpt-5.5 medium"
```

**Step 2: Run to verify failure**

```bash
python -m pytest tests/test_context_notifier.py::test_discord_like_internal_split_falls_back_instead_of_editing_first_chunk -q
```

Expected: FAIL if current code exact-matches the full response and edits first chunk.

**Step 3: Implement minimal rejection**

Mark unsafe split entries in Task 2 (`method == "send_split_unsafe"`) and reject in `_safe_edit_entry()`:

```python
    if entry.get("method") == "send_split_unsafe":
        return None
```

Do not scan older safe entries after rejecting a latest unsafe split candidate; `select_edit_candidate()` should continue to preserve the current "latest safe visible response only" rule. If the unsafe split entry is the latest relevant delivery, fallback.

**Step 4: Verify pass**

```bash
python -m pytest tests/test_context_notifier.py::test_discord_like_internal_split_falls_back_instead_of_editing_first_chunk -q
```

Expected: PASS.

---

### Task 4: Use Telegram `continuation_message_ids` from edit overflow results

**Objective:** When an adapter edit returns continuation ids and a final `message_id`, record a final continuation ledger entry so the notice can target the final visible Telegram chunk.

**Files:**
- Modify: `hermes_context_notifier.py:_record_delivery_from_edit`
- Test: `tests/test_context_notifier.py`

**Step 1: Write failing test**

```python
@pytest.mark.asyncio
async def test_observed_edit_records_telegram_overflow_final_continuation():
    class TelegramOverflowAdapter(DummyAdapter):
        async def edit_message(self, chat_id, message_id, content, metadata=None):
            self.edits.append((chat_id, message_id, content, metadata))
            return SimpleNamespace(
                success=True,
                message_id="tail",
                continuation_message_ids=("mid", "tail"),
            )

    adapter = TelegramOverflowAdapter()
    source = SimpleNamespace(platform=SimpleNamespace(value="telegram"), chat_id="123", thread_id="42")
    event = SimpleNamespace(source=source, message_id="msg-1", metadata={"message_thread_id": "42"})
    entry = SimpleNamespace(session_key="key", session_id="sid")
    session_store = SimpleNamespace(get_or_create_session=lambda src: entry)
    gateway = SimpleNamespace(adapters={"telegram": adapter})
    hcn.capture_gateway_context(event=event, gateway=gateway, session_store=session_store)

    await adapter.edit_message("123", "orig", "final chunk with enough detail to be selected safely " * 2, metadata={"message_thread_id": "42"})

    recorded = hcn._DELIVERY_LEDGER_BY_SESSION["key"][-1]
    assert recorded["message_id"] == "tail"
    assert recorded["method"] == "edit_continuation_tail"
```

**Step 2: Run to verify failure**

```bash
python -m pytest tests/test_context_notifier.py::test_observed_edit_records_telegram_overflow_final_continuation -q
```

Expected: FAIL or records only generic edit metadata.

**Step 3: Implement minimal continuation handling**

In `_record_delivery_from_edit()`, after resolving `resolved_message_id`, inspect:

```python
continuation_ids = tuple(getattr(result, "continuation_message_ids", ()) or ())
```

If `continuation_ids` is non-empty and `resolved_message_id == continuation_ids[-1]`, record a ledger entry with:

```python
"message_id": resolved_message_id,
"content": str(content or ""),
"method": "edit_continuation_tail",
"continuation_message_ids": continuation_ids,
```

Important limitation: the adapter result does not include per-continuation content. For Telegram overflow, `content` may still be the full edit text, not the final chunk. If helper splitting can reproduce chunks with adapter `MAX_MESSAGE_LENGTH`, use the last chunk as content. Otherwise mark the entry unsafe and fallback.

Preferred implementation:

```python
entry_content = str(content or "")
method = "edit"
continuation_ids = tuple(getattr(result, "continuation_message_ids", ()) or ())
if continuation_ids:
    chunks = split_chunks_for_adapter(adapter, entry_content)
    if len(chunks) > 1:
        entry_content = chunks[-1]
        method = "edit_continuation_tail"
    else:
        method = "edit_continuation_unsafe"
```

Reject `edit_continuation_unsafe` in `_safe_edit_entry()`.

**Step 4: Verify pass**

```bash
python -m pytest tests/test_context_notifier.py::test_observed_edit_records_telegram_overflow_final_continuation -q
```

Expected: PASS.

---

### Task 5: Add integration-style regression for long Slack response editing final chunk

**Objective:** Prove that a Slack-like adapter-internal split edits only the final chunk and does not send a context-only side message.

**Files:**
- Test: `tests/test_context_notifier.py`

**Step 1: Write failing test**

This unit test intentionally uses a simplified splitter. The implementation must still call the real adapter's `format_message()`/`truncate_message()` path where available; manual live validation checks real Slack chunk text and `msg_too_long` behavior.

```python
@pytest.mark.asyncio
async def test_slack_internal_split_notice_edits_last_chunk_not_full_response(tmp_path, monkeypatch):
    monkeypatch.setattr(hcn, "CACHE_PATH", tmp_path / "cache.json")

    class SlackSplitAdapter(DummyAdapter):
        MAX_MESSAGE_LENGTH = 20

        def truncate_message(self, content, max_length, **_):
            return [content[:max_length], content[max_length:]]

        async def send(self, chat_id, content, metadata=None):
            self.sent.append((chat_id, content, metadata))
            return SimpleNamespace(success=True, message_id="last")

    adapter = SlackSplitAdapter()
    source = SimpleNamespace(platform=SimpleNamespace(value="slack"), chat_id="C1", thread_id="T1")
    event = SimpleNamespace(source=source, message_id="msg-1", metadata={"thread_ts": "T1"})
    entry = SimpleNamespace(session_key="session", session_id="sid")
    session_store = SimpleNamespace(get_or_create_session=lambda src: entry)
    compressor = SimpleNamespace(last_prompt_tokens=158_000, context_length=272_000)
    agent = SimpleNamespace(context_compressor=compressor, reasoning_config={"enabled": True, "effort": "medium"})
    gateway = SimpleNamespace(_running_agents={"session": agent}, _agent_cache={}, adapters={"slack": adapter})
    full_response = "a" * 20 + "b" * 90

    hcn.capture_gateway_context(event=event, gateway=gateway, session_store=session_store)
    await adapter.send("C1", full_response, metadata={"thread_ts": "T1"})
    hcn.post_llm_call(session_id="sid", model="gpt-5.5", platform="slack", assistant_response=full_response)
    adapter._post_delivery_callbacks["session"]()
    await asyncio.sleep(0.01)

    assert adapter.edits[-1][1] == "last"
    assert adapter.edits[-1][2] == ("b" * 90) + "\n\n:straight_ruler: Context: 55% (158K/272K), gpt-5.5 medium"
    assert len(adapter.sent) == 1
```

**Step 2: Run to verify failure before implementation or pass after earlier tasks**

```bash
python -m pytest tests/test_context_notifier.py::test_slack_internal_split_notice_edits_last_chunk_not_full_response -q
```

Expected after Tasks 1-3: PASS.

---

### Task 6: Run focused and full validation

**Objective:** Confirm no regressions and plugin discovery still works.

**Files:**
- No code changes beyond prior tasks.

**Step 1: Run focused tests**

```bash
python -m pytest tests/test_context_notifier.py -q
```

Expected: all tests pass.

**Step 2: Run full plugin tests and syntax check**

```bash
python -m pytest -q
python -m py_compile __init__.py hermes_context_notifier.py tests/test_context_notifier.py
```

Expected: all tests pass and compile succeeds.

**Step 3: Run whitespace diff check**

```bash
git diff --check
```

Expected: no output.

**Step 4: Verify plugin discovery from Hermes source tree**

```bash
cd ~/.hermes/hermes-agent
python - <<'PY'
from hermes_cli.plugins import PluginManager
pm = PluginManager()
pm.discover_and_load(force=True)
loaded = pm._plugins.get('hermes-context-notifier')
print('found=', bool(loaded))
print('enabled=', getattr(loaded, 'enabled', None))
print('error=', getattr(loaded, 'error', None))
print('hooks=', sorted(getattr(loaded, 'hooks_registered', []) or []))
PY
```

Expected:

```text
found= True
enabled= True
error= None
hooks= ['post_llm_call', 'pre_gateway_dispatch']
```

---

### Task 7: Independent review and commit

**Objective:** Review the implementation before landing.

**Files:**
- Modified implementation and tests.

**Step 1: Collect diff**

```bash
git diff -- hermes_context_notifier.py tests/test_context_notifier.py
```

**Step 2: Run independent reviewer**

Use `delegate_task` or Codex read-only review. Required review prompt focus:

- Does this avoid editing wrong messages for Discord-like first-id split adapters?
- Does this avoid `msg_too_long` by editing only the final chunk for Slack-like last-id split adapters?
- Does it avoid platform history APIs?
- Does it keep message bodies in memory only?
- Does it preserve adapter return values and exceptions?

Expected: no blocking security or logic issues.

**Step 3: Commit**

```bash
git add hermes_context_notifier.py tests/test_context_notifier.py .hermes/plans/2026-05-18_225500-final-split-chunk-context-notice.md
git commit -m "fix: edit context notice into final split chunk"
```

Do not push unless explicitly requested.

---

## Manual Validation After Gateway Restart

After implementation commit and gateway restart:

1. Short Slack response crossing a 5% bucket:
   - Expected: notice appended to final assistant message.
2. Long Slack response that triggers adapter/internal split:
   - Expected: notice appended to the last Slack chunk, not a side-message.
   - No `msg_too_long` in `~/.hermes/logs/gateway.log` for the context-notifier edit.
3. Discord long response:
   - Expected in plugin-only slice: no wrong first-message edit; fallback side-message is acceptable.
4. Telegram long edit overflow if practical:
   - Expected: if `continuation_message_ids` and chunk reconstruction are available, notice appends to final continuation; otherwise fallback.

Useful log checks:

```bash
grep -iE 'context-notifier|Context:|msg_too_long|Failed to edit|continuation' ~/.hermes/logs/gateway.log | tail -120
```

---

## Follow-up Plan: Core/Adapter Delivery Parts Contract

This is intentionally out of scope for the immediate plugin-only fix. Create a separate plan before implementation.

**Goal:** Standardize adapter delivery metadata so plugins and gateway callbacks can identify every visible platform message produced by a send/edit operation.

Proposed contract:

```python
@dataclass
class DeliveryPart:
    message_id: str
    content: str
    index: int
    total: int

@dataclass
class SendResult:
    success: bool
    message_id: Optional[str] = None  # last visible message id after standardization
    delivery_parts: tuple[DeliveryPart, ...] = ()
    continuation_message_ids: tuple[str, ...] = ()  # legacy/compat
```

Follow-up tasks:

1. Add `delivery_parts` to `gateway/platforms/base.py::SendResult`.
2. Update Slack/Mattermost/Matrix/WhatsApp/Feishu/Discord/Telegram adapters to populate `delivery_parts` when splitting.
3. Standardize `message_id` to the last visible message id where safe, or document exceptions.
4. Update `stream_consumer.py` to consume `delivery_parts` where useful.
5. Update `hermes-context-notifier` to prefer `delivery_parts[-1]` over adapter-specific reconstruction.
6. Add cross-platform tests in Hermes core and plugin.

Entry criteria for this follow-up:

- Plugin-only fix is landed and live-tested.
- At least Slack and Discord desired behavior is agreed.
- The user explicitly approves core/adapter changes.

---

## Risk Register

- **Slack formatting changes chunk length:** Use adapter `format_message()` followed by `truncate_message()` where possible instead of manual slicing. If formatting/splitting cannot be reproduced, mark the split entry unsafe and fallback.
- **Reconstructed chunks may differ from actual sent chunks:** Unit tests use simplified splitters; live Slack validation must verify no `msg_too_long` and that the notice is appended to the actual final visible chunk.
- **Telegram UTF-16 length:** Telegram uses UTF-16 length semantics for message limits. Use the adapter/core UTF-16 length helper when reconstructing chunks; if unavailable, prefer `continuation_message_ids` but mark content reconstruction unsafe and fallback.
- **Discord wrong-message edit:** Treat Discord internal split as unsafe until core exposes all message ids/content.
- **Telegram continuation content unknown:** Use chunk reconstruction; if unavailable, fallback.
- **Short final chunk under 80 chars:** Current conservative suffix rule may fallback. This is acceptable unless UX demands otherwise.
- **Adapter private behavior changes:** Keep platform allowlists explicit and tests platform-specific.
- **Long response still exceeds final edit limit:** Final chunk + notice should be smaller than limit; if not, fallback.
- **WhatsApp bridge delivery variance:** Even when the adapter returns the last id, bridge/device sync behavior may be less deterministic than Slack; keep live validation optional and fallback safe.

---

## Completion Criteria

- Tests cover Slack-like last-id internal split, Discord-like first-id unsafe split, and Telegram continuation ids.
- Short/medium notice editing still works.
- Unsafe/unsupported platforms still fall back.
- Full test suite passes.
- Plugin discovery passes.
- Independent review passes.
- Gateway restart and live Slack smoke test confirm final-chunk edit for long split response when practical.
