# hermes-context-notifier

Hermes Agent plugin that posts a short context-usage notice to the current Slack conversation after a turn crosses configured context-window buckets.

Example:

```text
:warning: Context: 85% (230K/270K)
```

## Behavior

- Slack only for now.
- Reads actual context-window usage from `agent.context_compressor.last_prompt_tokens / context_length`.
- If usage cannot be read, it skips the turn instead of guessing.
- Starts notifying at 50%.
- Notifies once per 5% bucket per `session_key`.
- If usage jumps across multiple buckets, only the current bucket is notified.
- If usage drops after compression/reset, the dedupe bucket drops too, so later growth can notify again.
- Sends after the main gateway reply by chaining `register_post_delivery_callback`.

Emoji ranges:

- 50–65%: `:straight_ruler:`
- 70–85%: `:warning:`
- 90%+: `:rotating_light:`

## Install

Place this repository under the Hermes plugins directory:

```text
~/.hermes/plugins/hermes-context-notifier
```

Enable it in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - hermes-context-notifier
```

Then restart the Hermes gateway.

## Notes

This plugin intentionally depends on Hermes gateway private attributes such as `_running_agents` and `_agent_cache` because current hook payloads do not expose exact context-window usage. If Hermes internals change, fix the plugin rather than patching Hermes core.

Runtime dedupe state is stored in `cache.json` next to the plugin and is ignored by git.
