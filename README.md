# Hermes Context Notifier

A standalone plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that adds short context-window notices to messaging gateway conversations before the thread runs out of room.

<!-- README-I18N:START -->

**English** | [日本語](./README.ja.md)

<!-- README-I18N:END -->

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Install](#install)
- [Usage](#usage)
- [Supported platforms](#supported-platforms)
- [How it works](#how-it-works)
- [Development](#development)
- [Repository layout](#repository-layout)
- [Runtime state](#runtime-state)
- [License](#license)

## Features

- **Gateway-first notices:** Works where Hermes CLI context indicators are not visible: Slack, Telegram, Discord, Mattermost, Matrix, WhatsApp, Signal, Feishu, DingTalk, and BlueBubbles/iMessage.
- **Inline when safe:** Tracks adapter deliveries in memory and tries to append the notice to the final editable assistant message.
- **Side-message fallback:** Sends the same short notice after the main reply when edit support is missing or unsafe.
- **Exact usage only:** Reads `agent.context_compressor.last_prompt_tokens / context_length` and skips the turn when Hermes cannot expose exact usage.
- **Bucket dedupe:** Starts at 50%, fires once per 5% bucket per `session_key`, and re-arms after compression lowers usage.
- **Private data restraint:** Stores only dedupe state in `cache.json`; message bodies and platform payloads stay out of persistent storage.

```text
:warning: Context: 85% (230K/270K), gpt-5.5 medium
```

## Requirements

- Hermes Agent with standalone plugin loading enabled.
- Python 3.11 or newer.
- A Hermes gateway conversation on one of the supported platforms.

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

Restart the Hermes gateway after enabling or changing the plugin. Hermes imports plugin code when the gateway process starts.

## Usage

The plugin runs through Hermes hooks; it does not add a CLI command. Send messages through a supported gateway surface and the plugin adds a notice after the main assistant response when context usage crosses a bucket.

Example notices:

```text
:straight_ruler: Context: 50% (135K/270K)
:warning: Context: 85% (230K/270K), gpt-5.5 medium
:rotating_light: Context: 90% (243K/270K), gpt-5.5 medium
:warning: Context: 85% (850K/1M), gpt-5.5 medium
```

Emoji levels:

| Usage bucket | Emoji |
| --- | --- |
| 50-65% | `:straight_ruler:` |
| 70-85% | `:warning:` |
| 90%+ | `:rotating_light:` |

## Supported platforms

Enabled by default:

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

The plugin intentionally excludes Email, SMS, Webhook, API Server, Home Assistant, WeCom, Weixin, QQBot, and Yuanbao until each surface has platform-specific validation.

## How it works

`pre_gateway_dispatch` captures the current gateway conversation metadata and installs idempotent observers around future adapter `send()` and `edit_message()` calls. `post_llm_call` reads exact context usage from the live or cached agent, evaluates the next notification bucket, then chains a post-delivery callback so existing callbacks run first.

When a safe final assistant delivery is available, the plugin edits that message and appends the context notice. If editing fails, the adapter cannot edit, or the final message cannot be identified safely, it sends a separate notice to the same conversation with preserved thread/topic metadata.

The plugin reads a few Hermes gateway private attributes because Hermes hooks do not yet expose exact context usage or post-delivery callback composition as public plugin APIs. If those internals change, update this plugin rather than patching Hermes core.

## Development

Run checks from the repository root:

```bash
python -m pytest -q
python -m py_compile __init__.py hermes_context_notifier.py tests/test_context_notifier.py
```

Check plugin discovery from a Hermes Agent checkout:

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

Expected hooks:

```text
['post_llm_call', 'pre_gateway_dispatch']
```

## Repository layout

- `plugin.yaml`: plugin manifest.
- `__init__.py`: Hermes plugin entrypoint.
- `hermes_context_notifier.py`: hook handlers, delivery observers, bucket logic, cache handling, and notice delivery.
- `tests/test_context_notifier.py`: regression tests for formatting, usage extraction, bucket dedupe, metadata preservation, callback chaining, split messages, and edit fallback behavior.
- `AGENTS.md`: working notes for coding agents.

## Runtime state

`cache.json` stores per-session dedupe state next to the plugin and is ignored by git. `cache.json.tmp` is the atomic write temp file. The delivery ledger used for edit selection lives in process memory and clears on gateway restart.

## License

[MIT](LICENSE)
