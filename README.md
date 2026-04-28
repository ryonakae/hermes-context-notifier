# hermes-context-notifier

`hermes-context-notifier` is a standalone Hermes Agent plugin that posts a short Slack notice when the current conversation crosses context-window usage thresholds.

```text
:warning: Context: 85% (230K/270K)
```

The plugin leaves Hermes core untouched. It observes gateway hooks, reads context usage from the active agent when available, and sends a side-message after the main Slack reply has been delivered.

## Behavior

- Slack only for now.
- Uses `agent.context_compressor.last_prompt_tokens / context_length` as the usage source.
- Skips the turn if exact usage cannot be read.
- Starts notifying at 50%.
- Notifies once per 5% bucket per `session_key`.
- Sends only the current bucket if usage jumps across multiple buckets.
- Lowers the dedupe bucket after compression/reset so later growth can notify again.
- Sends after the main gateway reply by chaining `register_post_delivery_callback`.

Emoji ranges:

| Usage bucket | Emoji |
| --- | --- |
| 50-65% | `:straight_ruler:` |
| 70-85% | `:warning:` |
| 90%+ | `:rotating_light:` |

Examples:

```text
:straight_ruler: Context: 50% (135K/270K)
:warning: Context: 85% (230K/270K)
:rotating_light: Context: 90% (243K/270K)
```

## Install

Clone or place this repository under the Hermes plugins directory:

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

Run tests from the repository root:

```bash
python -m pytest -q
python -m py_compile __init__.py hermes_context_notifier.py tests/test_context_notifier.py
```

Check plugin discovery from the Hermes Agent source tree:

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

- `plugin.yaml`: Hermes plugin manifest.
- `__init__.py`: thin plugin entrypoint.
- `hermes_context_notifier.py`: hook handlers and notification logic.
- `tests/test_context_notifier.py`: unit tests for formatting, bucket logic, cache, usage extraction, and callback chaining.
- `AGENTS.md`: instructions for coding agents working in this repo.

## Runtime state

The plugin stores dedupe state in `cache.json` next to the plugin. The file is ignored by git. It stores session metadata and bucket state, not message bodies or raw Slack payloads.

## Notes

This plugin depends on Hermes gateway private attributes such as `_running_agents`, `_agent_cache`, `_active_sessions`, and `_post_delivery_callbacks` because current hook payloads do not expose exact context-window usage or post-delivery composition directly. If Hermes internals change, update this plugin instead of patching Hermes core.
