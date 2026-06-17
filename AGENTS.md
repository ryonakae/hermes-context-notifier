# AGENTS.md

`hermes-context-notifier` is a standalone Hermes Agent plugin for messaging gateway conversations. It watches context-window usage on supported non-CLI platforms and adds a short notice after the assistant reply, preferably by editing the final safe assistant message.

## Start here

- Runtime entrypoint: `__init__.py`
- Plugin implementation: `hermes_context_notifier.py`
- Manifest: `plugin.yaml`
- Tests: `tests/test_context_notifier.py`
- User docs: `README.md` and `README.ja.md`
- Design history: `.hermes/plans/`

## Common commands

Run from the repository root:

```bash
python -m pytest -q
python -m py_compile __init__.py hermes_context_notifier.py tests/test_context_notifier.py
```

When plugin registration, hook names, or `plugin.yaml` change, also check discovery from the Hermes Agent checkout:

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

Manual gateway validation requires a Hermes gateway restart after enabling or changing this plugin.

## Runtime behavior to preserve

- `pre_gateway_dispatch` captures platform metadata, gateway, adapter, session key/id, chat id, thread id, original metadata, and the gateway event loop.
- Adapter observer wrapping must stay idempotent. Preserve original `send()` / `edit_message()` return values and exceptions.
- `post_llm_call` must use exact usage from `agent.context_compressor.last_prompt_tokens / context_length`; do not estimate from cumulative token counters.
- Notifications start at 50% and fire once per 5% bucket per `session_key`.
- If usage jumps across buckets, send only the current bucket. If compression lowers usage, lower `last_notified_bucket` without sending a drop notice.
- Chain `register_post_delivery_callback` so existing callbacks run first.
- Prefer editing the final safe assistant message through `_DELIVERY_LEDGER_BY_SESSION`; fall back to a side-message when editing is unavailable, unsafe, or failed.
- Preserve platform thread/topic metadata through `notice_send_metadata()`.

## Scope boundaries

- Keep the platform allowlist explicit in `DEFAULT_SUPPORTED_PLATFORMS`.
- Do not add CLI notifications; CLI already has context/status surfaces.
- Do not add platform history lookup fallback without a fresh plan.
- Do not enable Email, SMS, Webhook, API Server, Home Assistant, WeCom, Weixin, QQBot, or Yuanbao without platform-specific validation.
- Do not edit Hermes core for this feature. If Hermes internals change, update the plugin.
- Do not add `/ctx` or `/context`; manual inspection belongs to Hermes `/usage`.

## State and privacy

- `cache.json` stores runtime dedupe state and is ignored by git.
- `cache.json.tmp` is the atomic write temp file and is ignored by git.
- Delivery bodies may exist in `_DELIVERY_LEDGER_BY_SESSION` process memory only.
- Do not persist message bodies, secrets, or raw platform payloads in `cache.json`, tests, docs, or logs.

## Coding guidelines

- Keep the plugin dependency-free and Python 3.11 compatible.
- Keep `__init__.py` as a thin entrypoint that works under Hermes plugin loading and direct pytest import.
- Prefer small pure functions for bucket, formatting, cache, usage extraction, and candidate-selection logic.
- Cover behavior changes in `tests/test_context_notifier.py`.
- Keep notification text short: `:warning: Context: 85% (230K/270K), gpt-5.5 medium`.
- Use `1M` for million-token windows, not `1000K`.
