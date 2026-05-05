# Hermes Context Notifier

`hermes-context-notifier` is a [Hermes Agent](https://hermes-agent.nousresearch.com/) plugin for messaging platforms. It watches a gateway conversation's context window and posts a short note when usage crosses a threshold.

It is built for non-CLI conversations where the usual terminal context display is not visible. It supports Slack, Telegram, Discord, Mattermost, Matrix, WhatsApp, Signal, Feishu, DingTalk, and BlueBubbles/iMessage gateway conversations.

```text
⚠️ Context: 85% (230K/270K), gpt-5.5 (medium)
```

It does not patch Hermes core. The plugin listens to gateway hooks, reads context usage from the active agent, and sends the note after the main platform reply has gone out.

## What it does

- Supports selected messaging gateway conversations. CLI is intentionally out of scope.
- Uses a platform-neutral capture/send path, with an allowlist for platforms where a short side-message fits the UX.
- Reads usage from `agent.context_compressor.last_prompt_tokens / context_length`.
- Adds the active model name when Hermes exposes it to the hook. Provider/path prefixes are shortened to the last component.
- Adds the reasoning effort after the model name when it is available from the active agent, for example `gpt-5.5 (medium)`.
- Formats large context windows as `1M` instead of `1000K`.
- Skips a turn when exact usage is unavailable.
- Starts at 50% and checks every 5% bucket.
- Sends one note per bucket for each `session_key`.
- If usage jumps from, say, 49% to 72%, it sends the 70% note only.
- If compression drops usage, it lowers the stored bucket so later growth can notify again.
- Chains `register_post_delivery_callback` so existing post-delivery work runs first.

Supported by default:

- Slack
- Telegram
- Discord
- Mattermost
- Matrix
- WhatsApp
- Signal
- Feishu
- DingTalk
- BlueBubbles / iMessage

Out of scope for this plugin for now:

- Email and SMS, because context notices would create extra emails/texts.
- Webhook, API Server, and Home Assistant, because they are integration surfaces rather than normal chat UI.
- WeCom, WeCom Callback, Weixin, QQBot, and Yuanbao because they need platform-specific validation.

Emoji ranges:

| Usage bucket | Emoji |
| --- | --- |
| 50-65% | 📏 `:straight_ruler:` |
| 70-85% | ⚠️ `:warning:` |
| 90%+ | 🚨 `:rotating_light:` |

Examples:

```text
📏 Context: 50% (135K/270K), gpt-5.5 (medium)
⚠️ Context: 85% (230K/270K), gpt-5.5 (medium)
🚨 Context: 90% (243K/270K), gpt-5.5 (medium)
⚠️ Context: 85% (850K/1M), gpt-5.5 (medium)
```

## Install

Clone the plugin into your Hermes plugins directory:

```bash
git clone https://github.com/ryonakae/hermes-context-notifier.git ~/.hermes/plugins/hermes-context-notifier
```

Enable it in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - hermes-context-notifier
```

Restart the Hermes gateway after enabling or changing the plugin.

## Development

Run the checks from the repository root:

```bash
python -m pytest -q
python -m py_compile __init__.py hermes_context_notifier.py tests/test_context_notifier.py
```

Check plugin discovery from the Hermes Agent checkout:

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

## Files

- `plugin.yaml`: plugin manifest.
- `__init__.py`: thin plugin entrypoint.
- `hermes_context_notifier.py`: hook handlers and notification logic.
- `tests/test_context_notifier.py`: tests for formatting, compact token counts, model-name shortening, bucket logic, cache writes, usage extraction, platform capture, metadata preservation, and callback chaining.
- `AGENTS.md`: notes for coding agents working in this repo.

## Runtime state

The plugin writes dedupe state to `cache.json` next to the plugin. Git ignores the file. It stores session metadata and bucket state, not message bodies or raw platform payloads.

## License

MIT.

## Why it uses private Hermes attributes

Hermes hooks do not currently expose exact context-window usage or post-delivery callback composition as public plugin APIs. This plugin reads private gateway attributes such as `_running_agents`, `_agent_cache`, `_active_sessions`, and `_post_delivery_callbacks` to avoid modifying Hermes core.

If those internals change, update this plugin rather than carrying a Hermes core patch.
