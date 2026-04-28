# Hermes context notifier plugin plan

## Goal

Hermes 本体を変更せず、`~/.hermes/plugins/hermes-context-notifier/` 配下のローカルプラグインとして、Slack gateway の現在の会話先にコンテクスト使用率の side-message を出す。

通知はシンプルにする。

```text
:warning: Context: 85% (230K/270K)
```

## Requirements

- Hermes core は編集しない。
- Slack gateway の現在の会話先を対象にする。
  - thread なら thread に出す。
  - DM なら DM に出す。
  - channel 通常発言ならその会話先に出す。
- 50%以上から通知を開始する。
- 5%刻みの bucket 到達時に1回だけ通知する。
  - 50, 55, 60, 65, 70, 75, 80, 85, 90, 95...
- 同じ session / bucket では重複通知しない。
- 使用率が複数bucketを一気に飛び越えた場合も、出す通知は現在bucketの1通だけにする。
  - 例: 49% → 72% なら `70%` の1通だけ出し、内部の `last_notified_bucket` も70へ進める。
- 表示文は短くする。
- 絵文字は使用率で切り替える。
  - 50〜65%: `:straight_ruler:`
  - 70〜85%: `:warning:`
  - 90%以上: `:rotating_light:`
- `/ctx` / `/context` は実装しない。既存 `/usage` があるため手動確認コマンドは不要。
- cache はプラグイン配下に保存する。

## Repository

Local directory:

```text
/Users/ryo.nakae/.hermes/plugins/hermes-context-notifier
```

Remote:

```text
git@github.com:ryonakae/hermes-context-notifier.git
```

## Plugin directory

Target directory:

```text
~/.hermes/plugins/hermes-context-notifier/
```

Expected files:

```text
~/.hermes/plugins/hermes-context-notifier/
├── plugin.yaml
├── __init__.py
└── cache.json
```

Plan file location:

```text
/Users/ryo.nakae/.hermes/plugins/hermes-context-notifier/.hermes/plans/2026-04-28_163039-hermes-context-notifier-plugin.md
```

## Existing Hermes behavior to rely on

### Plugin hook: `pre_gateway_dispatch`

This hook receives:

```python
def pre_gateway_dispatch(event, gateway, session_store, **kwargs):
    ...
```

Use it to:

- detect Slack messages
- get the current session entry
- capture `gateway`, `session_store`, `event.source`, `chat_id`, `thread_id`
- store enough session metadata for `post_llm_call` to send post-delivery notifications
- do not implement custom `/ctx` or `/context`; use existing `/usage` for manual inspection

### Plugin hook: `post_llm_call`

This hook receives:

```python
def post_llm_call(session_id, user_message, assistant_response, conversation_history, model, platform, **kwargs):
    ...
```

It does not receive usage directly. Instead, use state captured from `pre_gateway_dispatch` to find the live gateway agent:

```python
agent = gateway._running_agents.get(session_key)
```

Then read usage from the live/cached agent where available:

```python
ctx = agent.context_compressor
used = ctx.last_prompt_tokens
limit = ctx.context_length
```

Usage source priority:

1. `agent.context_compressor.last_prompt_tokens` and `agent.context_compressor.context_length` from the live running agent.
2. If the live running agent is unavailable, optionally try the cached agent in `gateway._agent_cache[session_key]`.
3. If both are unavailable, skip notification for that turn rather than guessing from cumulative session tokens.

Do not use `agent.session_prompt_tokens` as a context-window usage substitute; it is session/API usage accounting and can diverge from actual current context size.

Private attributes are acceptable here because the goal is to avoid core changes. If Hermes internals change, only this plugin needs repair.

## Data model

Store cache in:

```text
~/.hermes/plugins/hermes-context-notifier/cache.json
```

Cache / dedupe key:

- Use `session_key` as the primary key.
- Store `session_id` as metadata only.
- Do not key by `session_id`, `chat_id`, or `thread_id`; those can diverge from Hermes gateway's actual session boundary.

Suggested shape:

```json
{
  "sessions": {
    "<session_key>": {
      "session_id": "...",
      "platform": "slack",
      "chat_id": "C...",
      "thread_id": "177...",
      "used": 230000,
      "limit": 270000,
      "percent": 85.1,
      "bucket": 85,
      "last_notified_bucket": 85,
      "model": "...",
      "updated_at": "2026-04-28T16:30:39+09:00"
    }
  }
}
```

Write atomically:

1. write `cache.json.tmp`
2. `Path.replace()` to `cache.json`

## Notification logic

Compute bucket:

```python
bucket = int(percent // 5) * 5
```

Skip if:

- platform is not Slack
- `limit <= 0`
- `used <= 0`
- `bucket < 50`
- `bucket <= last_notified_bucket`

If usage jumps across multiple buckets, send only the current bucket notification and set `last_notified_bucket = bucket`. Do not backfill skipped bucket notifications.

Emoji:

```python
if bucket <= 65:
    emoji = ":straight_ruler:"
elif bucket <= 85:
    emoji = ":warning:"
else:
    emoji = ":rotating_light:"
```

Format:

```python
text = f"{emoji} Context: {bucket}% ({used_k}K/{limit_k}K)"
```

Use rounded K values. Keep it simple; do not include remaining tokens unless requested later.

Examples:

```text
:straight_ruler: Context: 50% (135K/270K)
:straight_ruler: Context: 65% (176K/270K)
:warning: Context: 70% (189K/270K)
:warning: Context: 85% (230K/270K)
:rotating_light: Context: 90% (243K/270K)
```

## Sending Slack side-message

Use the gateway adapter captured from `pre_gateway_dispatch`.

Because `post_llm_call` runs in the agent thread, schedule adapter send on the gateway event loop:

```python
asyncio.run_coroutine_threadsafe(
    adapter.send(chat_id, text, metadata={"thread_id": thread_id} if thread_id else None),
    loop,
)
```

Thread selection:

- Prefer `event.source.thread_id`
- Fallback to `event.message_id` for Slack thread reply behavior, if available
- Store this during `pre_gateway_dispatch`

Ordering / delivery completion:

- `post_llm_call` fires after model response generation, but before the final Slack delivery is necessarily complete.
- Hermes gateway adapters already have a one-shot `register_post_delivery_callback(session_key, callback, generation=...)` mechanism.
- Base adapter fires this callback in `_process_message_background` after response/media delivery handling and `on_processing_complete`.
- Use this callback to send the context side-message after the main assistant response is delivered, instead of using arbitrary sleep.
- Caveat: the callback slot is single-value and Hermes core already uses it for deferred background-review notifications. The plugin must wrap/chain any existing callback rather than overwrite it.

## Manual inspection

Do not implement custom `/ctx` or `/context` commands.

Rationale:

- The plugin is for automatic threshold notification only.
- Hermes already has `/usage` for manual inspection.
- Keeping the plugin narrow reduces surface area and command conflicts.

## Reset / compression handling

Reset conditions:

- session id changes
- current bucket is lower than cached `last_notified_bucket`
- used tokens drop significantly after compression

Proposed behavior:

- If `session_id` changed, initialize a new cache record.
- If `bucket < last_notified_bucket`, lower `last_notified_bucket` to the current bucket without sending an immediate notification.
- This allows notifications again after compression or reset-like context shrinkage.
  - Example: notified at 85%, compression drops usage to 35%, then usage rises to 50% → send 50% notification again.
- Next upward crossing will notify again.

## Files likely to change / create

Create only under:

```text
~/.hermes/plugins/hermes-context-notifier/
```

Files:

- `plugin.yaml`
- `__init__.py`
- `cache.json` generated at runtime
- optional `README.md` if useful

No Hermes core files should be edited.

## Implementation steps

1. Create plugin directory.
2. Add `plugin.yaml` with plugin metadata and provided hooks.
3. Add `__init__.py`.
4. In `register(ctx)`, register hooks:
   - `pre_gateway_dispatch`
   - `post_llm_call`
5. Implement module-level state:
   - session id → gateway/session metadata
   - session key → latest cache record
6. Implement cache load/save helpers with atomic write.
7. Implement Slack detection helper.
8. Implement usage extraction helper:
   - first try `gateway._running_agents[session_key].context_compressor.last_prompt_tokens/context_length`
   - optionally fallback to `gateway._agent_cache[session_key][0].context_compressor.last_prompt_tokens/context_length`
   - skip notification if context compressor data is unavailable; do not substitute cumulative session token counters
9. Implement bucket and emoji formatting.
10. Implement notification dedupe by `last_notified_bucket`.
11. In `post_llm_call`, compute usage and cache the notification candidate.
12. Register a post-delivery callback on the Slack adapter so the context side-message is sent after the main assistant response delivery completes.
    - Use `adapter.register_post_delivery_callback(session_key, callback, generation=...)` when possible.
    - Preserve and chain any existing callback in `adapter._post_delivery_callbacks[session_key]`; do not overwrite background-review delivery.
    - If generation cannot be resolved safely, fall back to immediate scheduling or a short delay only as a secondary path.
13. Do not add `/ctx` or `/context`; rely on existing `/usage` for manual inspection.
14. Enable plugin explicitly in Hermes config:
    - `plugins.enabled` must include `hermes-context-notifier`.
    - User/local standalone plugins under `~/.hermes/plugins/` are opt-in; simply placing `plugin.yaml` is not enough.
15. Restart gateway after enabling or changing plugin.

## Validation plan

Manual validation:

1. Start/restart gateway.
2. Confirm plugin loads from logs.
3. Send Slack message in a thread.
4. Run a conversation long enough to collect token usage.
5. Confirm threshold side-message appears only at configured buckets.
6. Simulate cache records around thresholds and trigger `post_llm_call` helper if possible.
   - 49%: no notification
   - 50%: notification
   - 53%: no duplicate
   - 55%: notification
   - 85%: warning emoji
   - 90%: rotating light emoji
8. Confirm duplicate bucket is not sent twice.
9. Confirm new session or lowered bucket resets dedupe behavior.

Potential automated tests, if later desired:

- unit test formatting helper
- unit test bucket calculation
- unit test cache read/write
- unit test notification dedupe state

## Risks and tradeoffs

- Uses private gateway attributes: `_running_agents`, `_agent_cache`.
  - Tradeoff accepted to avoid Hermes core changes.
- `post_llm_call` may run before the assistant response is delivered to Slack.
  - Side-message ordering is best-effort.
- If the live agent is not available, usage may be one turn stale.
  - Acceptable for threshold notification.
- Streaming mode may alter ordering.
  - Keep side-message short and infrequent.
- Multiple concurrent Slack sessions require session-keyed cache.
  - Do not use a single global bucket.

## Open questions

- Should notifications be sent after final assistant delivery with a delay?
  - Decision: use `post_delivery_callback`; delay is fallback only.
- Should bucket use actual percent or rounded percent?
  - Recommendation: bucket floor by 5%, display bucket percent.
