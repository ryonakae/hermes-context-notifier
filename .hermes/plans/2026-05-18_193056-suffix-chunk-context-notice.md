# Suffix Chunk Context Notice Implementation Plan

> **For Hermes:** Plugin-only change. Do not modify Hermes core. Use TDD and independent review before commit.

## Goal

When a long assistant response is delivered as multiple platform messages, append the context notice to the final recorded content chunk instead of sending a separate context-only message.

This addresses Slack notification noise while avoiding unsafe platform-history lookup or guessed message IDs.

## Investigation Summary

Checked gateway platform adapters for long-message behavior and edit APIs.

Findings:

- Slack can hit `chat.update` `msg_too_long`, after which the gateway may deliver the final answer in multiple messages.
- Some adapters split long sends internally and return either first or last message id depending on platform.
- A plugin observer generally sees adapter-level `send()` / `edit_message()` calls, not platform API internals.
- Inferring the final chunk from `truncate_message()` and assuming the returned `message_id` points to the final chunk is unsafe for first-id or unknown-id adapters.

Final design decision:

- Do **not** infer internal split chunks.
- Do **not** call platform history APIs.
- Only suffix-edit a delivery entry that the plugin actually observed and recorded.
- Require the candidate delivery to be newer than the session input capture (`delivery_sequence > delivery_start`) to avoid editing stale messages from prior turns.

## Scope

In scope:

- `hermes_context_notifier.py`
- `tests/test_context_notifier.py`
- Plugin-side in-memory ledger only

Out of scope:

- Hermes core changes
- Slack-specific API calls
- Platform history APIs
- Adapter changes
- Guessing split chunks from adapter internals

## Safety Rules

1. Exact full-response match remains allowed, but only for delivery entries recorded after the current session input capture when called from post-delivery notice flow.
2. Suffix match is allowed only on the latest safe delivery entry after `delivery_start`.
3. If the latest safe delivery entry is not an exact match or sufficiently long suffix of `assistant_response`, do not scan older entries; fall back to side-message.
4. Reject tiny suffixes with `MIN_SUFFIX_CANDIDATE_CHARS = 80`.
5. Reject entries without `chat_id`, `message_id`, or content.
6. Reject entries that already contain context notice text or obvious status/progress text.
7. Keep all message bodies in memory only; do not persist them to `cache.json`.

## Implementation Tasks

### Task 1: Add suffix candidate tests

Add tests for:

- latest recorded suffix chunk can be selected when full response match is absent;
- tiny suffix chunks are rejected;
- older suffix chunks are rejected when a newer safe entry exists;
- stale suffix chunks before `delivery_start` are rejected;
- suffix chunks recorded after `delivery_start` are accepted.

### Task 2: Implement conservative candidate selection

Add:

- `MIN_SUFFIX_CANDIDATE_CHARS`
- `_content_is_response_suffix()`
- `_safe_edit_entry()`
- optional `min_delivery_sequence` parameter to `select_edit_candidate()`

Change `select_edit_candidate()` to scan from newest to oldest and stop at the first safe non-split-parent entry:

- return it if exact match or long suffix match;
- otherwise return `None` immediately.

### Task 3: Add delivery sequencing

Add a global in-memory `_DELIVERY_SEQUENCE`.

- `record_delivery_entry()` increments it and writes `delivery_sequence` onto every ledger entry.
- `capture_gateway_context()` stores `delivery_start` in session metadata.
- `_edit_or_send_later()` passes `min_delivery_sequence=meta.get("delivery_start")` to candidate selection.

### Task 4: Add realistic split response regression

Simulate a Slack-like flow where the final response is observed as two separate deliveries:

1. first chunk sent and recorded;
2. final chunk sent and recorded;
3. `post_llm_call(... assistant_response=full_response)` runs;
4. post-delivery callback edits the second message (`m2`) and does not send a context-only side message.

### Task 5: Verification

Run:

```bash
python -m pytest -q
python -m py_compile __init__.py hermes_context_notifier.py tests/test_context_notifier.py
git diff --check
```

Run plugin discovery from Hermes source tree:

```bash
python - <<'PY'
from hermes_cli.plugins import PluginManager
pm = PluginManager(); pm.discover_and_load(force=True)
loaded = pm._plugins.get('hermes-context-notifier')
print('found=', bool(loaded))
print('enabled=', getattr(loaded, 'enabled', None))
print('error=', getattr(loaded, 'error', None))
print('hooks=', sorted(getattr(loaded, 'hooks_registered', []) or []))
PY
```

### Task 6: Independent Review

Use Codex read-only review if delegate infrastructure fails. Review must pass with no blocking security or logic issues.

Reviewer result after final safety revision:

```json
{
  "passed": true,
  "security_concerns": [],
  "logic_errors": []
}
```

## Result

This implementation fixes the safe observable case: when the final chunk is actually recorded as a separate delivery, the notice is edited into that final chunk.

It intentionally does **not** try to solve opaque adapter-internal splitting where the plugin cannot prove that a returned `message_id` belongs to the final chunk. Those cases safely fall back to side-message.
