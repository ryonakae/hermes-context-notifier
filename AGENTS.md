# AGENTS.md

`hermes-context-notifier` is a standalone Hermes Agent plugin for non-CLI gateway conversations. It posts short context-usage side-messages for selected messaging platforms: Slack, Telegram, Discord, Mattermost, Matrix, WhatsApp, Signal, Feishu, DingTalk, and BlueBubbles/iMessage.

## Start here

- Runtime entrypoint: `__init__.py`
- Plugin logic: `hermes_context_notifier.py`
- Manifest: `plugin.yaml`
- Tests: `tests/test_context_notifier.py`
- Original implementation plan: `.hermes/plans/2026-04-28_163039-hermes-context-notifier-plugin.md`
- Multi-platform implementation plan: `.hermes/plans/2026-04-29_223558-multi-platform-context-notifier.md`

## Common commands

Run from the repository root:

```bash
python -m pytest -q
python -m py_compile __init__.py hermes_context_notifier.py tests/test_context_notifier.py
```

Check Hermes plugin discovery from the Hermes Agent source tree:

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

## Validation

Before committing code changes, run:

```bash
python -m pytest -q
python -m py_compile __init__.py hermes_context_notifier.py tests/test_context_notifier.py
```

When touching plugin registration, `plugin.yaml`, or hook names, also run the plugin discovery check above.

Manual gateway validation requires a Hermes gateway restart after enabling or changing this plugin. The gateway imports plugin code at process start.

## Runtime behavior

- `pre_gateway_dispatch` captures supported platform event metadata, gateway, adapter, session key, session id, chat id, thread id, original metadata, and the gateway event loop.
- `post_llm_call` reads context usage from the live or cached agent: `agent.context_compressor.last_prompt_tokens / context_length`, and appends the hook-provided model name when available. It appends reasoning effort from the hook, active agent, or gateway config when available.
- If usage cannot be read, skip the turn. Do not estimate from cumulative token counters.
- Notifications start at 50% and fire once per 5% bucket per `session_key`.
- If usage jumps across multiple buckets, send only the current bucket notification.
- If usage drops after compression/reset, lower `last_notified_bucket` without sending a drop notification.
- Send after the main messaging-platform reply by chaining `register_post_delivery_callback`. Existing callbacks must run first.
- Do not make CLI emit these notifications; CLI already has its own context/status surfaces.
- Do not enable Email, SMS, Webhook, API Server, Home Assistant, WeCom, Weixin, QQBot, or Yuanbao in this plugin without a fresh plan and platform-specific validation.

## Important paths

- `cache.json`: runtime dedupe state, ignored by git.
- `cache.json.tmp`: atomic write temp file, ignored by git.
- `~/.hermes/config.yaml`: local enablement lives here, not in this repo.

Enablement example:

```yaml
plugins:
  enabled:
    - hermes-context-notifier
```

## Coding guidelines

- Keep the plugin dependency-free and Python 3.11 compatible.
- Keep `__init__.py` as a thin plugin entrypoint that works under Hermes plugin loading and direct pytest import.
- Prefer small pure functions for bucket, formatting, cache, and usage extraction logic. Cover them in `tests/test_context_notifier.py`.
- Keep platform allowlisting explicit in `DEFAULT_SUPPORTED_PLATFORMS`; do not switch to "all Hermes platforms" by default.
- Treat Hermes gateway private attributes as fragile: `_running_agents`, `_agent_cache`, `_active_sessions`, and `_post_delivery_callbacks` may change upstream.
- Do not edit Hermes core for this feature. If internals change, update this plugin.
- Do not add `/ctx` or `/context`; manual inspection belongs to Hermes `/usage`.

## Workflow notes

- Keep messaging notifications short, for example `:warning: Context: 85% (230K/270K), gpt-5.5 (medium)`; for million-token windows use `1M`, not `1000K`.
- The notification target is the current gateway conversation. Preserve platform thread/topic metadata via `notice_send_metadata()`.
- Do not store message bodies, secrets, or raw platform payloads in `cache.json`.
- Restart the gateway before expecting Slack or other gateway surfaces to use new plugin code.
