# Hermes Context Notifier

Hermes Agent の messaging gateway 会話に、コンテキストウィンドウの残り具合が分かる短い通知を追加するプラグインです。

<!-- README-I18N:START -->

[English](./README.md) | **日本語**

<!-- README-I18N:END -->

## 目次

- [機能](#機能)
- [要件](#要件)
- [インストール](#インストール)
- [使い方](#使い方)
- [対応 platform](#対応-platform)
- [仕組み](#仕組み)
- [開発](#開発)
- [リポジトリ構成](#リポジトリ構成)
- [Runtime state](#runtime-state)
- [License](#license)

## 機能

- **Gateway 向け通知:** Hermes CLI のコンテキスト表示が見えない Slack、Telegram、Discord、Mattermost、Matrix、WhatsApp、Signal、Feishu、DingTalk、BlueBubbles/iMessage で動きます。
- **安全ならインライン追記:** adapter delivery をメモリ上で追跡し、最後の編集可能な assistant メッセージへ通知を追記します。
- **別メッセージへのフォールバック:** 編集できない場合や安全に対象を特定できない場合は、メイン返信の後に同じ短い通知を送ります。
- **正確な使用量だけを使用:** `agent.context_compressor.last_prompt_tokens / context_length` を読み、正確な値が取れないターンは通知しません。
- **Bucket 単位の重複抑制:** 50% から 5% ごとに、`session_key` ごと 1 回だけ通知します。圧縮で使用量が下がると再通知できる状態に戻します。
- **永続データを最小化:** `cache.json` には重複抑制の状態だけを保存し、メッセージ本文や platform payload は保存しません。

```text
:warning: Context: 85% (230K/270K), gpt-5.5 medium
```

## 要件

- standalone plugin loading が有効な Hermes Agent。
- Python 3.11 以上。
- 対応 platform 上の Hermes gateway 会話。

## インストール

プラグインを Hermes plugins ディレクトリへ clone します。

```bash
git clone https://github.com/ryonakae/hermes-context-notifier.git ~/.hermes/plugins/hermes-context-notifier
```

`~/.hermes/config.yaml` で有効化します。

```yaml
plugins:
  enabled:
    - hermes-context-notifier
```

有効化後、またはプラグイン変更後は Hermes gateway を再起動してください。Hermes は gateway process の起動時に plugin code を import します。

## 使い方

このプラグインは Hermes hooks 経由で動きます。CLI command は追加しません。対応 gateway surface で会話していると、コンテキスト使用量が bucket を超えたタイミングで、assistant のメイン返信後に通知を追加します。

通知例:

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

## 対応 platform

デフォルトで有効:

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

Email、SMS、Webhook、API Server、Home Assistant、WeCom、Weixin、QQBot、Yuanbao は、platform ごとの検証が済むまで対象外です。

## 仕組み

`pre_gateway_dispatch` は現在の gateway 会話メタデータを取得し、以後の adapter `send()` / `edit_message()` を見る observer を idempotent に取り付けます。`post_llm_call` は live agent または cached agent から正確な context usage を読み、次の通知 bucket を判定し、既存 callback が先に走るように post-delivery callback を chain します。

安全に編集できる最後の assistant delivery がある場合、プラグインはそのメッセージに context notice を追記します。編集に失敗した場合、adapter が編集非対応の場合、または最終メッセージを安全に特定できない場合は、thread/topic metadata を保持して同じ会話に別メッセージを送ります。

Hermes hooks は正確な context usage や post-delivery callback composition をまだ public plugin API として公開していないため、このプラグインは一部の Hermes gateway private attributes を読みます。internals が変わった場合は Hermes core を patch せず、このプラグインを更新してください。

## 開発

リポジトリルートで checks を実行します。

```bash
python -m pytest -q
python -m py_compile __init__.py hermes_context_notifier.py tests/test_context_notifier.py
```

Hermes Agent checkout から plugin discovery を確認します。

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

期待する hooks:

```text
['post_llm_call', 'pre_gateway_dispatch']
```

## リポジトリ構成

- `plugin.yaml`: plugin manifest。
- `__init__.py`: Hermes plugin entrypoint。
- `hermes_context_notifier.py`: hook handlers、delivery observers、bucket logic、cache handling、notice delivery。
- `tests/test_context_notifier.py`: formatting、usage extraction、bucket dedupe、metadata preservation、callback chaining、split messages、edit fallback behavior の regression tests。
- `AGENTS.md`: coding agents 向け作業メモ。

## Runtime state

`cache.json` は plugin の隣に per-session dedupe state を保存し、git では無視されます。`cache.json.tmp` は atomic write 用の一時ファイルです。edit selection に使う delivery ledger は process memory にだけ存在し、gateway restart で消えます。

## License

[MIT](LICENSE)
