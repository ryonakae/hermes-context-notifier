# Delivery Ledger Inline Edit Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Replace trailing context-usage side-messages with best-effort edits to the final Hermes gateway message, using a plugin-side delivery ledger instead of platform history APIs or Hermes core changes.

**Architecture:** Keep `hermes-context-notifier` as a standalone plugin and do not edit Hermes core. The plugin will wrap supported platform adapters' `send()` and `edit_message()` methods at runtime to observe future delivery results, keep an in-memory per-session delivery ledger, then use the existing post-delivery callback to append the context notice to the last editable assistant message. If no safe editable message is found or edit fails, fall back to the existing side-message behavior.

**Tech Stack:** Python 3.11, Hermes Agent standalone plugin hooks (`pre_gateway_dispatch`, `post_llm_call`), `BasePlatformAdapter.register_post_delivery_callback`, async adapter method wrappers, pytest.

---

## Current state and constraints

- Current repo: `/Users/ryo.nakae/.hermes/plugins/hermes-context-notifier`.
- Existing plan directory: `.hermes/plans/`.
- Existing implementation sends context notices as post-delivery side-messages via `register_post_delivery_callback`.
- Before implementation, the only intended uncommitted change is this plan file. Re-read target files before editing and do not resurrect deleted content from stale context.
- Do **not** use platform history APIs such as Slack `conversations.replies` / Discord `history()` for this feature.
- Do **not** edit Hermes core.
- Do **not** store message bodies in `cache.json`; the delivery ledger must be process-local in memory only.
- Do **not** add `/ctx`, `/context`, manual commands, CLI surfaces, cron, or platform-specific API clients.
- Keep the existing supported platform allowlist. Email/SMS/API Server/Webhook/HomeAssistant/WeCom/Weixin/QQBot/Yuanbao remain out of scope.
- `assistant_response` availability is already confirmed in the active Hermes checkout: `agent/conversation_loop.py` invokes `post_llm_call` with `assistant_response=final_response`. If a future runtime stops passing it, fail closed to side-message fallback rather than guessing from unrelated deliveries.

## Desired runtime behavior

### Happy path: editable final message

1. `pre_gateway_dispatch` captures session metadata and installs adapter observers once per adapter instance.
2. During the turn, the wrapped adapter `send()` / `edit_message()` records successful visible deliveries in an in-memory ledger keyed by `session_key`.
3. `post_llm_call` evaluates the context bucket exactly as today.
4. If a notice is due, `post_llm_call` registers a post-delivery callback.
5. After the main Hermes response is delivered, the callback finds the latest safe editable ledger entry for the session and calls `adapter.edit_message(chat_id, message_id, content + "\n\n" + notice)`.
6. Slack/Discord/etc. notification previews show the real assistant reply, not a standalone context notice.

### Fallback path

Use the existing side-message send if:

- no ledger entry exists for the session;
- the latest entry has no `message_id`;
- the platform is explicitly non-editable (`SUPPORTS_MESSAGE_EDITING is False`, e.g. Signal/BlueBubbles);
- `adapter.edit_message(...)` is absent or returns `success=False`;
- the candidate content already contains any context notice line, even for a different bucket;
- the candidate does not look like the current assistant final response and is likely progress/approval/status text.

### Dedupe behavior

- Existing bucket dedupe remains authoritative.
- A notice is still emitted at most once per 5% bucket per `session_key`.
- The delivery ledger must not create extra bucket notifications.
- If edit fails and side-message fallback sends, do not attempt another edit for the same bucket.

---

## Design details

### New in-memory globals

Add near `_SESSION_CONTEXT_BY_ID`:

```python
_DELIVERY_LEDGER_BY_SESSION: dict[str, list[dict[str, Any]]] = {}
_ADAPTER_OBSERVERS: dict[int, dict[str, Any]] = {}
MAX_LEDGER_ENTRIES_PER_SESSION = 20
```

Each ledger entry should be compact and in-memory only:

```python
{
    "session_key": "...",
    "platform": "slack",
    "chat_id": "C123",
    "thread_id": "177...",          # normalized from metadata when available
    "message_id": "177...",         # SendResult.message_id or edit target id
    "content": "assistant text",     # runtime memory only; never cache.json
    "metadata": {"thread_id": "..."},
    "method": "send" | "edit",
    "created_at": "2026-...Z",
}
```

### Adapter observer install

Add `ensure_adapter_observer(adapter)` and call it from `capture_gateway_context()` after resolving the adapter.

Rules:

- Idempotent per adapter object (`id(adapter)`).
- Preserve original call signatures and async behavior.
- Store original methods in `_ADAPTER_OBSERVERS[id(adapter)]`, not in persistent files.
- Observe only successful sends/edits with meaningful text.
- Do not record obvious progress/status messages such as `⏳ Still working...`, inactivity warnings, approval prompts, or standalone context notices. Filter them before they enter the ledger, not only during candidate selection.
- Never swallow adapter exceptions or change return values.
- Do not wrap if `adapter` is `None`.

Pseudo-code shape:

```python
def ensure_adapter_observer(adapter: Any) -> None:
    key = id(adapter)
    if key in _ADAPTER_OBSERVERS:
        return
    original_send = getattr(adapter, "send", None)
    original_edit = getattr(adapter, "edit_message", None)
    if not callable(original_send):
        return

    async def observed_send(chat_id, content, *args, **kwargs):
        result = await original_send(chat_id, content, *args, **kwargs)
        record_delivery_from_send(adapter, chat_id, content, args, kwargs, result)
        return result

    async def observed_edit(chat_id, message_id, content, *args, **kwargs):
        result = await original_edit(chat_id, message_id, content, *args, **kwargs)
        record_delivery_from_edit(adapter, chat_id, message_id, content, args, kwargs, result)
        return result
```

### Mapping deliveries to sessions

Because adapter `send()` does not receive `session_key`, use captured active session metadata:

- Iterate `_SESSION_CONTEXT_BY_ID.values()`.
- Match `adapter is meta["adapter"]`.
- Match `chat_id` string equality.
- Match thread/topic metadata when present:
  - `thread_id`
  - `thread_ts`
  - `message_thread_id`
  - `root_id`
- If metadata lacks thread info, allow chat-only match.
- If multiple sessions match the same chat/thread, choose the most recently captured meta. Add `captured_at` ISO timestamp or monotonic counter to meta during `capture_gateway_context`.
- If no captured session metadata matches a delivery, do not record it. Missing a delivery is safe because the callback can still fall back to side-message.

This is intentionally best-effort but should be deterministic in tests.

### Candidate selection for editing

Add pure helper:

```python
def select_edit_candidate(session_key: str, assistant_response: str, notice_text: str) -> dict[str, Any] | None:
    ...
```

Rules:

1. Read ledger entries for `session_key` newest first.
2. Require `message_id`, `chat_id`, `content`.
3. Skip entries whose content already contains any context notice pattern, not just the current `notice_text`. A 75% notice must not be appended to a message that already contains a 70% notice.
4. Prefer entries whose normalized content equals normalized `assistant_response`.
5. Otherwise accept newest entry whose normalized content is a prefix/suffix-compatible version of the assistant response. This covers streaming cursor cleanup and long final edit paths.
6. Do not select obvious progress/status entries such as `⏳ Still working...`, `⚠️ No activity...`, `Dangerous command requires approval`, or standalone context notices.
7. If no candidate passes, return `None` and side-message fallback.

Keep the helper conservative. A false negative only causes a side-message; a false positive edits the wrong message.

### Editing callback

Replace `register_post_delivery_notice(...)` behavior with a new function such as `register_post_delivery_update(...)`.

Flow inside callback:

1. Run any existing callback first, preserving current callback chain behavior.
2. If adapter is non-editable, send side-message fallback.
3. Select edit candidate from ledger.
4. If no candidate, send side-message fallback.
5. Build `updated_content = candidate["content"].rstrip() + "\n\n" + notice_text`.
6. Schedule `adapter.edit_message(chat_id=candidate["chat_id"], message_id=candidate["message_id"], content=updated_content)` on the captured event loop.
7. If edit returns success, record an edit delivery entry so the ledger reflects the updated content.
8. If edit fails/raises, send side-message fallback.

Implementation note: `_send_later` currently schedules a send and does not observe the result. Add an `_edit_or_send_later(...)` helper that schedules a coroutine which awaits edit result, then optionally awaits fallback send.

The post-delivery callback itself remains synchronous. The edit/fallback coroutine is scheduled onto the captured gateway loop; bucket dedupe stays in `cache.json`, so an edit failure followed by delayed side-message fallback must not create a second bucket notification.

### Platform edit support

Add helper:

```python
def adapter_may_edit(adapter: Any) -> bool:
    if getattr(adapter, "SUPPORTS_MESSAGE_EDITING", True) is False:
        return False
    return callable(getattr(adapter, "edit_message", None))
```

Do not overfit to platform class names. Attempt edit only when the helper allows it; still handle `success=False` as fallback. Known non-editable targets such as Signal and BlueBubbles should fall back immediately when their adapters expose `SUPPORTS_MESSAGE_EDITING = False`; if a future adapter lacks the flag, a failed edit must still cleanly fall back.

### Cache/state

- Keep bucket dedupe in `cache.json` exactly as today.
- Add no message body, raw response, token trace, or platform payload to `cache.json`.
- Ledger is in-memory only and pruned oldest-first/FIFO to `MAX_LEDGER_ENTRIES_PER_SESSION` per session.
- Docs must say that gateway restart clears the delivery ledger, which is fine because it only tracks future deliveries.

---

## Task 1: Add delivery ledger pure helpers

**Objective:** Add data-shaping helpers for recording, pruning, matching, and selecting delivery entries without wrapping adapters yet.

**Files:**
- Modify: `hermes_context_notifier.py`
- Modify: `tests/test_context_notifier.py`

**Step 1: Write failing tests**

Add tests for:

- recording a successful send result appends one ledger entry;
- ledger prunes oldest-first/FIFO to `MAX_LEDGER_ENTRIES_PER_SESSION`;
- candidate selection prefers exact assistant response match;
- candidate selection skips any entry containing any context notice pattern, not only the current notice text;
- candidate selection skips progress/status entries;
- candidate selection returns `None` when only non-matching entries exist.

Example test skeleton:

```python
def test_delivery_ledger_records_and_prunes_entries(monkeypatch):
    monkeypatch.setattr(hcn, "MAX_LEDGER_ENTRIES_PER_SESSION", 2)
    hcn._DELIVERY_LEDGER_BY_SESSION.clear()
    hcn.record_delivery_entry("s", {"message_id": "1", "content": "old"})
    hcn.record_delivery_entry("s", {"message_id": "2", "content": "mid"})
    hcn.record_delivery_entry("s", {"message_id": "3", "content": "new"})
    assert [e["message_id"] for e in hcn._DELIVERY_LEDGER_BY_SESSION["s"]] == ["2", "3"]
```

**Step 2: Run focused tests and verify RED**

Run:

```bash
python -m pytest tests/test_context_notifier.py -q
```

Expected: FAIL because helper functions do not exist.

**Step 3: Implement minimal helpers**

Add:

- `_DELIVERY_LEDGER_BY_SESSION`
- `MAX_LEDGER_ENTRIES_PER_SESSION`
- `record_delivery_entry(session_key, entry)`
- `normalize_delivery_text(text)`
- `is_context_notice_text(text)`
- `is_obvious_status_text(text)`
- `select_edit_candidate(session_key, assistant_response, notice_text)`

Keep functions small and dependency-free.

**Step 4: Run focused tests and verify GREEN**

Run:

```bash
python -m pytest tests/test_context_notifier.py -q
```

Expected: PASS.

---

## Task 2: Install idempotent adapter observers

**Objective:** Wrap adapter `send()` and `edit_message()` once per adapter instance, preserving original behavior while observing successful future deliveries.

**Files:**
- Modify: `hermes_context_notifier.py`
- Modify: `tests/test_context_notifier.py`

**Step 1: Write failing tests**

Add tests for:

- an `autouse` fixture clears `_SESSION_CONTEXT_BY_ID`, `_DELIVERY_LEDGER_BY_SESSION`, and `_ADAPTER_OBSERVERS` before each test;
- `capture_gateway_context()` installs an observer on the resolved adapter;
- calling `capture_gateway_context()` twice does not double-wrap;
- observed `send()` returns the original result unchanged;
- observed `edit_message()` returns the original result unchanged;
- observer does not record failed send/edit results;
- observer does not record progress/status/context-notice messages;
- observer maps delivery to the matching `session_key` by adapter/chat/thread metadata;
- observer safely ignores deliveries that do not match any captured session metadata.

Use a dummy async adapter with `send()` returning `SimpleNamespace(success=True, message_id="m1")` and `edit_message()` returning `SimpleNamespace(success=True, message_id="m1")`.

**Step 2: Run focused tests and verify RED**

```bash
python -m pytest tests/test_context_notifier.py -q
```

Expected: FAIL because observer helpers do not exist.

**Step 3: Implement observer install**

Add:

- `_ADAPTER_OBSERVERS`
- `ensure_adapter_observer(adapter)`
- `_record_delivery_from_send(...)`
- `_record_delivery_from_edit(...)`
- `_matching_session_meta(...)`
- metadata/thread normalization helpers;
- `captured_at` or a monotonic sequence on captured metadata, so multiple same-chat sessions choose the newest matching context deterministically.

Call `ensure_adapter_observer(adapter)` inside `capture_gateway_context()` after `adapter` is resolved.

**Step 4: Run focused tests and verify GREEN**

```bash
python -m pytest tests/test_context_notifier.py -q
```

Expected: PASS.

---

## Task 3: Replace side-message callback with edit-or-fallback callback

**Objective:** Make post-delivery notification append to the last editable assistant message when safe, falling back to the current side-message behavior otherwise.

**Files:**
- Modify: `hermes_context_notifier.py`
- Modify: `tests/test_context_notifier.py`

**Step 1: Write failing tests**

Add tests for:

- existing post-delivery callback still runs before notifier work;
- when a matching ledger entry exists and adapter edit succeeds, no side-message is sent;
- edited content equals `candidate.content.rstrip() + "\n\n" + notice_text`;
- when candidate already contains any context notice line from any bucket, no duplicate edit occurs;
- when edit returns `success=False`, existing side-message fallback sends;
- when `SUPPORTS_MESSAGE_EDITING = False`, existing side-message fallback sends;
- when `assistant_response` is empty/missing, callback fails closed to side-message fallback instead of editing a guessed latest message;
- when `edit_message` has an incompatible signature or raises, side-message fallback sends;
- generation tuple behavior remains preserved.

**Step 2: Run focused tests and verify RED**

```bash
python -m pytest tests/test_context_notifier.py -q
```

Expected: FAIL because callback still sends side-message unconditionally.

**Step 3: Implement edit-or-fallback helper**

Add/replace with:

- `adapter_may_edit(adapter)`
- `_edit_or_send_later(adapter, chat_id, text, meta, loop, session_key, assistant_response)`
- `register_post_delivery_notice(..., assistant_response="")` updated to call edit-or-fallback.

Keep `_send_later` as the fallback primitive so existing side-message tests remain simple.

**Step 4: Update `post_llm_call()` call site**

Pass `assistant_response` from hook kwargs into the callback registration:

```python
def post_llm_call(..., assistant_response: str = "", ...):
    ...
    register_post_delivery_notice(..., assistant_response=assistant_response)
```

This is expected to work on the active Hermes checkout because `agent/conversation_loop.py` invokes `post_llm_call` with `assistant_response=final_response`. Keep the default empty string and fallback behavior for older/future runtimes.

**Step 5: Run focused tests and verify GREEN**

```bash
python -m pytest tests/test_context_notifier.py -q
```

Expected: PASS.

---

## Task 4: Add end-to-end unit tests for realistic send/edit sequences

**Objective:** Prove the intended non-streaming and streaming-like flows without requiring a real gateway or platform API.

**Files:**
- Modify: `tests/test_context_notifier.py`

**Step 1: Add non-streaming simulation test**

Test sequence:

1. `capture_gateway_context(...)` installs observer.
2. Simulate main response delivery by awaiting `adapter.send(chat_id, assistant_response, metadata=thread_metadata)`.
3. Call `post_llm_call(..., assistant_response=assistant_response)` to register callback.
4. Fire the stored post-delivery callback.
5. Await loop drain.
6. Assert `adapter.edits == [(chat_id, message_id, assistant_response + "\n\n" + notice)]` and no side-message context notice was sent.

**Step 2: Add streaming-like edit sequence test**

Test sequence:

1. Observed `send()` records first partial content with `message_id="m1"`.
2. Observed `edit_message()` records final assistant content for `m1`.
3. `post_llm_call(..., assistant_response=final_response)` registers callback.
4. Callback appends notice through another edit to `m1`.

**Step 3: Add fallback sequence test**

Use a dummy adapter whose `edit_message()` returns `success=False`. Assert side-message fallback still preserves thread metadata.

**Step 4: Run tests**

```bash
python -m pytest tests/test_context_notifier.py -q
```

Expected: PASS.

---

## Task 5: Update docs and manifest expectations

**Objective:** Document the new behavior and private API risks so future agents do not reintroduce platform history APIs or core patches.

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `plugin.yaml` only if hook list changes. This plan should not require new hooks.

**Step 1: Update README**

Change runtime description from "sends the note after the main platform reply" to:

- By default, the plugin tries to append the context notice to the final Hermes message by observing adapter deliveries and editing the last safe editable message.
- If edit is unsupported or unsafe, it falls back to a short side-message.
- It does not call platform history APIs.
- It does not patch Hermes core.
- The in-memory delivery ledger is cleared by gateway restart and stores message bodies only in process memory, never in `cache.json`.

**Step 2: Update AGENTS.md**

Add agent-facing notes:

- Adapter observer wrapping is intentional and must remain idempotent.
- Do not add Slack/Discord/Telegram history lookup fallback without a fresh plan.
- Do not store delivery bodies in `cache.json`.
- Before editing docs, re-read README/AGENTS because users may edit them in parallel.
- Expected hooks remain `['post_llm_call', 'pre_gateway_dispatch']` unless implementation adds a new hook.

**Step 3: Run docs-sensitive grep**

```bash
grep -R "conversations.replies\|conversations.history\|channel.history\|platform history" -n README.md AGENTS.md hermes_context_notifier.py tests/test_context_notifier.py .hermes/plans || true
```

Expected: only plan text may mention history APIs as explicitly out of scope.

---

## Task 6: Full validation and plugin discovery smoke

**Objective:** Verify the plugin remains loadable and tests pass.

**Files:**
- No source changes expected unless validation reveals issues.

**Step 1: Run unit tests**

```bash
python -m pytest -q
```

Expected: PASS.

**Step 2: Run py_compile**

```bash
python -m py_compile __init__.py hermes_context_notifier.py tests/test_context_notifier.py
```

Expected: no output / exit 0.

**Step 3: Run plugin discovery from Hermes checkout**

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

**Step 4: Review diff**

```bash
git diff --stat
git diff --check
git status --short --branch
```

Expected: source/tests/docs/plan changes only; no runtime files staged; no whitespace errors.

---

## Review checklist before implementation

- [ ] No Hermes core files are modified.
- [ ] No platform history API is introduced.
- [ ] Adapter wrapping is idempotent and preserves return values/exceptions.
- [ ] Ledger is in-memory only and pruned.
- [ ] Edit candidate selection is conservative.
- [ ] Side-message fallback remains available and tested.
- [ ] Existing callback chain and generation handling still work.
- [ ] Signal/BlueBubbles-style non-editable adapters fall back.
- [ ] README/AGENTS clearly describe private API and gateway restart behavior.

## Suggested commit sequence

1. `feat: add delivery ledger helpers for context notifier`
2. `feat: observe gateway adapter deliveries for context notifier`
3. `feat: append context notice via message edit when safe`
4. `docs: document delivery-ledger context notices`

Do not push until tests and plugin discovery smoke pass.
