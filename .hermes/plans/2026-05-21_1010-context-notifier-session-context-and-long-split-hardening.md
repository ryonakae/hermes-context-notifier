# Context Notifier Session Context and Long-Split Hardening Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task after Ryo approves implementation.

**Goal:** Reduce false `missing_session_context` noise and harden/verify `hermes-context-notifier` behavior around Slack long-message splitting without changing Hermes core first.

**Architecture:** Keep the plugin-side delivery ledger design. Treat gateway user turns, cron runs, and delegated child agents as separate hook classes; only gateway user turns should require captured session context. Long-message handling remains plugin-only: use observed delivery entries and adapter split reconstruction, and add observability/tests before behavior changes.

**Tech Stack:** Python 3.11, Hermes standalone plugin hooks (`pre_gateway_dispatch`, `post_llm_call`), pytest, Slack adapter semantics.

---

## Investigation Summary

### Evidence checked

- Plugin discovery is healthy from `~/.hermes/hermes-agent`:
  - `found=True`, `enabled=True`, `error=None`
  - hooks: `['post_llm_call', 'pre_gateway_dispatch']`
- Plugin repository is clean and synced with origin:
  - latest commit: `50ee765 feat: log context notifier decisions`
- Current test suite passes:
  - `python3 -m pytest tests/test_context_notifier.py -q` → `56 passed`
- Runtime log counts in `~/.hermes/logs/gateway.log`:
  - `context_notifier` lines: 4,889+
  - `usage_decision`: 99+
  - `notice_register`: 21+
  - `edit_candidate`: 21+
  - `edit_result`: 21+, all observed `success=true`
- Since the latest relevant gateway restart (`2026-05-19 22:56`):
  - `notice=yes`: 22
  - `edit_result`: 22
  - notifier `edit_result success=false`: 0
  - notifier `side_send`: 0
  - Slack `msg_too_long`: 10 lines, last observed `2026-05-21 02:32`
  - notifier delivery records: `send=342`, `edit=3988`, no observed `send_split_tail` / `edit_continuation_tail` runtime sample yet

### Root cause hypothesis A: `missing_session_context` is mostly non-gateway-agent hook noise

`post_llm_call` is a global Hermes hook fired from `agent/conversation_loop.py` for every AIAgent turn:

- gateway user sessions
- cron jobs
- delegate_task child agents
- auxiliary/background sessions

`hermes-context-notifier` only captures gateway metadata in `pre_gateway_dispatch`, which only runs for non-internal gateway messages in `gateway/run.py`. Therefore any agent turn that did not pass through `pre_gateway_dispatch` will correctly have no `_SESSION_CONTEXT_BY_ID[session_id]` entry.

The recent `missing_session_context` examples support this:

- `cron_*` sessions are cron runs and should not be notifier targets.
- `20260521_094322_b94d9e`, `20260521_094711_3b9999`, `20260521_095256_3530ef`, `20260521_100018_9cdbf3` have `parent_session_id=20260521_082702_701175`, model `gpt-5.4-mini`, source `slack`: these are delegated/child agents inheriting Slack source, not the visible gateway session.
- The actual current Slack thread session key `agent:main:slack:group:C0ATJKG7AER:1779324835.053039` maps to session `20260521_095356_995e7612` in `~/.hermes/sessions/sessions.json`, and the plugin is recording deliveries for that session key.

Current impact: mostly log noise and misleading diagnostics, not proof that normal notifier delivery is broken.

### Root cause hypothesis B: Slack `msg_too_long` is adapter streaming edit behavior, not notifier edit failure

Slack adapter `send()` splits long messages via `truncate_message()` and returns the last chunk `ts`. Slack adapter `edit_message()` currently formats content and calls `chat_update` directly without split-aware handling.

The observed `msg_too_long` errors occur in `gateway.platforms.slack: edit_message`, while notifier logs around notice insertion show `edit_result success=true` and no notifier `side_send` fallback. This points to Slack streaming/progressive edit trying to update an existing message with overlong content before the gateway sends a continuation/final chunk.

Current impact: noisy Slack adapter errors and possible poor streaming UX for very long responses. Not currently observed as notifier inserting a context-only side message.

### Root cause hypothesis C: split-tail code is tested but not proven in live runtime

The plugin has unit tests for:

- stream-consumer visible chunk suffix selection
- Slack-like adapter-internal split returning last id
- Discord-like adapter-internal split fallback
- Telegram overflow continuation tail
- Slack internal split editing last chunk instead of full response

However, runtime logs after the latest restart show only delivery `method=send` and `method=edit`; no `send_split_tail`, `send_split_unsafe`, `edit_continuation_tail`, or `edit_continuation_unsafe` samples were observed. This means the code path is covered by tests but not yet exercised in live traffic, likely because the visible gateway delivery path has mostly been streaming `edit` records or adapter `send()` calls under the split threshold.

Important correction: current logs do **not** contain assistant message bodies, final chunk text, raw visible Slack chunk count, or `split_original_length` when no split method was classified. That is intentional for privacy/body-leak prevention, but it means we cannot reconstruct "which long message was split" from logs alone. We can only infer candidates from surrounding `msg_too_long`, many rapid `delivery_record method=edit` events, `response ready ... response=N chars`, and notifier `usage_decision/edit_result` lines.

Therefore, an unobserved split-tail method is not proof that live split-tail behavior is healthy. A plugin bug could still cause a split response to be recorded as plain `method=send`/`method=edit`, or a streaming edit path could bypass adapter-internal split classification entirely. The right next step is to add body-free diagnostics that record: formatted/raw length buckets, split chunk count, selected candidate method, candidate match class (`exact`, `suffix`, `none`), and whether the edited message was a normal final edit vs. a split-tail edit.

Observed overlap examples:

- `2026-05-20 10:26:44` Slack `msg_too_long` occurred, then at `10:27:09` notifier crossed `63.4% / bucket=60`, registered a notice, found `method=edit`, and `edit_result success=true`. Logs prove a notice was edited after an overlong Slack edit error, but they do **not** prove whether the visible message was split or whether the edited target was the final split chunk.
- `2026-05-21 02:32:12` Slack `msg_too_long` occurred, then at `02:32:14` notifier crossed `82.7% / bucket=80`, registered a notice, found `method=send`, and `edit_result success=true`. This is the strongest overlap candidate. It proves notifier edited some sent message after a long/overlong Slack event, but current logs do **not** prove that this was the final chunk of a split long response.

---

## Non-goals

- Do not modify Hermes core in this slice.
- Do not add platform history lookups such as Slack `conversations.replies`.
- Do not persist message bodies to `cache.json`.
- Do not change notification thresholds or copy.
- Do not suppress real notifier failures; only demote/clarify expected non-gateway-agent skips.

---

## Task 1: Add a regression test for child/cron `post_llm_call` skips

**Objective:** Prove that `post_llm_call` without captured gateway context does not look like a user-visible notifier failure.

**Files:**
- Modify: `tests/test_context_notifier.py`
- Modify later: `hermes_context_notifier.py`

**Step 1: Write failing test**

Add a test that calls `hcn.post_llm_call()` without `capture_gateway_context()` and asserts one of these planned outcomes:

```python
def test_post_llm_without_gateway_context_is_quiet_expected_skip(caplog):
    with caplog.at_level(logging.DEBUG, logger="gateway.plugins.hermes_context_notifier"):
        hcn.post_llm_call(
            session_id="child-session",
            model="gpt-5.4-mini",
            platform="slack",
            assistant_response="child output",
        )

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "gateway.plugins.hermes_context_notifier"
    ]
    assert not any("reason=missing_session_context" in message for message in messages)
```

If we still want an observable trace, assert a DEBUG-level event instead of INFO:

```python
assert any("event=post_llm_skip" in message and "reason=uncaptured_session" in message for message in messages)
```

**Step 2: Run the focused test**

Run:

```bash
cd ~/.hermes/plugins/hermes-context-notifier
python3 -m pytest tests/test_context_notifier.py::test_post_llm_without_gateway_context_is_quiet_expected_skip -q
```

Expected: FAIL with current implementation because `post_llm_skip reason=missing_session_context` is logged at INFO.

---

## Task 2: Classify missing context skips without hiding real gateway misses

**Objective:** Make expected non-gateway-agent skips quiet, while keeping real gateway context misses diagnosable.

**Files:**
- Modify: `hermes_context_notifier.py:897-901`
- Test: `tests/test_context_notifier.py`

**Implementation direction:**

Replace the unconditional INFO log:

```python
if not meta:
    _log_info("post_llm_skip", session_id=session_id, reason="missing_session_context")
    return None
```

with a helper that classifies the skip.

Candidate helper:

```python
def _log_uncaptured_post_llm(session_id: str, platform: str) -> None:
    # `post_llm_call` is global. Cron, delegated children, auxiliary agents,
    # and background agents never pass through `pre_gateway_dispatch`; this is
    # expected and should not read like a visible notifier failure.
    _log_debug(
        "post_llm_skip",
        session_id=session_id,
        platform=platform,
        reason="uncaptured_session",
    )
```

If `_log_debug` does not exist, add it next to `_log_info` / `_log_warning` with the same no-body/no-secret formatting policy.

Use:

```python
if not meta:
    _log_uncaptured_post_llm(session_id, platform)
    return None
```

**Step 1:** Add the helper and make the test pass.

**Step 2:** Run:

```bash
python3 -m pytest tests/test_context_notifier.py::test_post_llm_without_gateway_context_is_quiet_expected_skip -q
```

Expected: PASS.

**Step 3:** Run the full plugin suite:

```bash
python3 -m pytest tests/test_context_notifier.py -q
```

Expected: all tests pass.

---

## Task 3: Add capture/context correlation logs for real gateway turns

**Objective:** Make future diagnosis precise: when a normal visible gateway turn is captured, logs should show the captured `session_id` and `session_key` without message body leakage.

**Files:**
- Modify: `hermes_context_notifier.py:640-678`
- Test: `tests/test_context_notifier.py`

**Step 1: Write failing test**

Add a `caplog` test around `capture_gateway_context()` asserting a correlation event exists. Set the logger/level explicitly so a later INFO→DEBUG decision does not make the test flaky:

```python
def test_capture_gateway_context_logs_session_correlation(caplog):
    adapter = DummyAdapter()
    source = SimpleNamespace(platform=SimpleNamespace(value="slack"), chat_id="C1", thread_id="T1")
    event = SimpleNamespace(source=source, message_id="msg-1", metadata={"thread_ts": "T1"})
    entry = SimpleNamespace(session_key="session", session_id="sid")
    session_store = SimpleNamespace(get_or_create_session=lambda src: entry)
    gateway = SimpleNamespace(adapters={"slack": adapter})

    with caplog.at_level(logging.INFO, logger="gateway.plugins.hermes_context_notifier"):
        hcn.capture_gateway_context(event=event, gateway=gateway, session_store=session_store)

    assert any(
        record.name == "gateway.plugins.hermes_context_notifier"
        and "event=context_capture" in record.getMessage()
        and "session_id=sid" in record.getMessage()
        and "session_key=session" in record.getMessage()
        for record in caplog.records
    )
```

**Step 2: Implement minimal logging**

After `_SESSION_CONTEXT_BY_ID[session_id] = meta`, log:

```python
_log_info(
    "context_capture",
    session_id=session_id,
    session_key=session_key,
    platform=platform,
    has_adapter=adapter is not None,
)
```

Do not log message bodies or metadata payloads.

**Step 3:** Run focused + full tests.

---

## Task 4: Add observation-only coverage for session split / compression visibility

**Objective:** Guard against a true gateway miss where `pre_gateway_dispatch` captures one session id but `post_llm_call` fires under a compressed/new session id, without inventing a plugin-side alias mechanism before there is a reliable signal.

**Files:**
- Modify: `tests/test_context_notifier.py`
- Possibly modify: `hermes_context_notifier.py`

**Test shape:**

Simulate a captured gateway context for `old_sid`, then call `post_llm_call(session_id="new_sid")` while the same `session_key` still has active usage. The desired behavior should be explicit:

Do **not** implement a plugin-side alias in this slice. The plugin currently has no reliable hook/signal that says "old gateway session id became new gateway session id for this same visible turn". A guessed alias would hide a real notifier miss.

Rejected future option unless a reliable signal is exposed:

```python
hcn.capture_gateway_context(... session_id="old_sid" ...)
hcn.alias_session_context("old_sid", "new_sid")
hcn.post_llm_call(session_id="new_sid", ...)
assert notice path runs
```

Current slice behavior: no alias yet, but observable skip:

```python
hcn.post_llm_call(session_id="new_sid", ...)
assert log has reason="uncaptured_session" at DEBUG, not INFO
```

If later logs prove a real visible gateway notification miss across compression, create a separate core/gateway follow-up plan for exposing session split hooks or passing session-key metadata into `post_llm_call`.

---

## Task 5: Add Slack overlong/split classification diagnosis tests

**Objective:** Make clear whether a long/overlong Slack delivery is classified as a split-tail candidate, a plain send/edit candidate, or a notifier edit failure fallback.

**Files:**
- Modify: `tests/test_context_notifier.py`
- Possibly modify: `hermes_context_notifier.py` logging only

**Test shape:**

Add tests for both boundaries:

1. **Notifier edit failure fallback:** a fake adapter whose `edit_message()` returns `success=False, error="msg_too_long"` when content is too long. Verify `register_post_delivery_notice()` fallback behavior still sends a side notice if the notifier itself hits this error.
2. **Split classification:** a Slack-like adapter where a long send/edit is classified as `send_split_tail` / `send_split_unsafe` / plain `edit`, and the selected candidate logs make the classification visible without bodies.

This distinguishes three paths:

1. Slack adapter streaming edit fails before notifier notice callback — plugin should not be blamed.
2. Notifier edit candidate fails — plugin must fallback and log `edit_result success=false` plus `side_send success=true`.
3. Split classification never happens — plugin may be incorrectly recording a split delivery as plain `send`/`edit`, which is exactly the user's concern.

Expected plugin behavior for path 2 should be:

```python
assert adapter.edits
assert adapter.sent[-1][1].startswith(":straight_ruler: Context:")
```

Expected diagnostic behavior for path 3 should be explicit: either the log says `split_considered=true split_chunks=N candidate_method=send_split_tail`, or it says why the selected target stayed plain `method=send|edit`.

---

## Task 6: Improve split-tail runtime observability

**Objective:** When live traffic finally exercises adapter-internal or continuation split paths, logs should prove which path happened, and when it does not, logs should prove whether a split was considered and rejected.

**Files:**
- Modify: `hermes_context_notifier.py:276-394`, `select_edit_candidate()`, and candidate logging around `register_post_delivery_notice()`
- Test: `tests/test_context_notifier.py`

**Implementation direction:**

Current `delivery_record` includes `method` and sometimes `split_chunks`, but live logs did not show split methods. Add body-free diagnostics that can answer Ryo's exact question without storing message content:

- On delivery record:
  - `content_length_bucket` only; do not log exact length in production logs
  - `formatted_length_bucket`
  - `split_considered=true/false`
  - `split_chunks=N` even for rejected/unsafe split paths
  - `method=send|edit|send_split_tail|send_split_unsafe|edit_continuation_tail|edit_continuation_unsafe`
- On candidate selection:
  - `candidate_match=exact|suffix|none`
  - `candidate_method=<method>`
  - `candidate_split_chunks=N` if present
  - `candidate_split_parent=true/false`
  - `candidate_delivery_sequence=N`
  - `candidate_created_after_delivery_start=true/false`
- On edit result:
  - `target_method=<method>`
  - `target_split_chunks=N` if present
  - `target_is_split_tail=true/false`
  - `success=true/false`

Do not log message body, raw metadata, assistant response text, exact content length, or chunk text. Keep `split_original_length` in in-memory ledger if needed, but do not emit it to gateway logs.

Add tests that assert log lines include:

- `method=send_split_tail` and `split_chunks=...`
- `method=send_split_unsafe` for first-id/unknown split platforms
- `method=edit_continuation_tail` for Telegram overflow
- candidate match class for normal `method=edit`, `method=send`, and split-tail methods
- notifier edit after a Slack-like overlong event still reports whether the selected target was split-tail or plain send/edit
- negative privacy tests prove logs do not include body snippets, raw metadata, assistant response text, or exact content lengths

---

## Task 7: Validate with focused smoke after gateway restart

**Objective:** Prove the changed logging and notifier behavior in live gateway traffic.

**Prerequisite:** Ryo approves a gateway restart after code changes.

**Steps:**

1. Run plugin tests:

```bash
cd ~/.hermes/plugins/hermes-context-notifier
python3 -m pytest tests/test_context_notifier.py -q
python3 -m py_compile __init__.py hermes_context_notifier.py tests/test_context_notifier.py
```

2. Commit plugin changes:

```bash
git status --short --branch
git add hermes_context_notifier.py tests/test_context_notifier.py .hermes/plans/2026-05-21_1010-context-notifier-session-context-and-long-split-hardening.md
git commit -m "fix: clarify context notifier gateway session skips"
```

3. Restart gateway using Ryo's normal wrapper, not from inside Safehouse if blocked.

4. After a normal Slack turn, inspect:

```bash
grep -E "context_notifier event=(context_capture|usage_decision|notice_register|edit_candidate|edit_result|post_llm_skip|side_send|delivery_record)" ~/.hermes/logs/gateway.log | tail -120
```

Expected:

- normal visible gateway turn has `context_capture` followed by `usage_decision`
- delegated child/cron sessions no longer produce INFO-level `missing_session_context` noise
- notifier notice edits still show `edit_result success=true`
- no notifier `side_send` unless edit is unsupported/failed

5. For long response smoke, intentionally produce a long Slack answer that crosses a context bucket. Check whether delivery records and candidate/edit logs show `split_considered`, `split_chunks`, `candidate_method`, and `target_is_split_tail`. If they do not, report exactly which diagnostic field is missing; do not claim the split-tail path is proven.

---

## Open questions before implementation

1. Should expected uncaptured sessions be fully silent or DEBUG-level only? Recommendation: DEBUG-level with `reason=uncaptured_session`.
2. Do we want a plugin-side session alias for compression-created session splits? Recommendation: not until a reliable hook/signal exists; start with logging and observability.
3. Should Slack adapter `edit_message()` itself become split-aware? Recommendation: separate Hermes core/adapter follow-up, because it is outside this plugin and can affect streaming behavior globally.

## Recommended next action

Implement Tasks 1-3 first to clean up false `missing_session_context` noise and correlate gateway turns. Then implement Task 6 diagnostics before making any behavior claims about long split messages. Task 5 can be implemented with Task 6 if it stays logging/test-only. Keep Task 4 alias behavior and Slack adapter changes deferred until logs prove a real missed visible gateway notice rather than hook noise.
