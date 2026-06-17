# Hermes Context Notifier

Hermes Agent のメッセージングゲートウェイ会話に、コンテキストウィンドウの残り具合を知らせる短い通知を追加するプラグインです。

<!-- README-I18N:START -->

[English](./README.md) | **日本語**

<!-- README-I18N:END -->

## 目次

- [機能](#機能)
- [要件](#要件)
- [インストール](#インストール)
- [使い方](#使い方)
- [対応プラットフォーム](#対応プラットフォーム)
- [仕組み](#仕組み)
- [開発](#開発)
- [リポジトリ構成](#リポジトリ構成)
- [実行時の状態](#実行時の状態)
- [ライセンス](#ライセンス)

## 機能

- **ゲートウェイ向け通知:** Hermes CLI のコンテキスト表示が見えない Slack、Telegram、Discord、Mattermost、Matrix、WhatsApp、Signal、Feishu、DingTalk、BlueBubbles/iMessage で動きます。
- **安全な場合はインライン追記:** アダプターの配信結果をメモリ上で追跡し、最後の編集可能なアシスタントメッセージへ通知を追記します。
- **別メッセージへのフォールバック:** 編集できない場合や安全に対象を特定できない場合は、メイン返信の後に同じ短い通知を送ります。
- **正確な使用量だけを使用:** `agent.context_compressor.last_prompt_tokens / context_length` を読み、正確な値が取れないターンは通知しません。
- **バケット単位の重複抑制:** 50% から 5% ごとに、`session_key` ごと 1 回だけ通知します。圧縮で使用量が下がると、後でまた通知できる状態に戻します。
- **永続データを最小化:** `cache.json` には重複抑制の状態だけを保存します。メッセージ本文やプラットフォームの生データは保存しません。

```text
:warning: Context: 85% (230K/270K), gpt-5.5 medium
```

## 要件

- スタンドアロンプラグインの読み込みが有効な Hermes Agent。
- Python 3.11 以上。
- 対応プラットフォーム上の Hermes ゲートウェイ会話。

## インストール

プラグインを Hermes のプラグインディレクトリへクローンします。

```bash
git clone https://github.com/ryonakae/hermes-context-notifier.git ~/.hermes/plugins/hermes-context-notifier
```

`~/.hermes/config.yaml` で有効化します。

```yaml
plugins:
  enabled:
    - hermes-context-notifier
```

有効化後、またはプラグイン変更後は Hermes ゲートウェイを再起動してください。Hermes はゲートウェイプロセスの起動時にプラグインコードをインポートします。

## 使い方

このプラグインは Hermes のフック経由で動きます。CLI コマンドは追加しません。対応ゲートウェイで会話していると、コンテキスト使用量がバケットを超えたタイミングで、アシスタントのメイン返信後に通知を追加します。

通知例:

```text
:straight_ruler: Context: 50% (135K/270K)
:warning: Context: 85% (230K/270K), gpt-5.5 medium
:rotating_light: Context: 90% (243K/270K), gpt-5.5 medium
:warning: Context: 85% (850K/1M), gpt-5.5 medium
```

絵文字レベル:

| 使用率バケット | 絵文字 |
| --- | --- |
| 50-65% | `:straight_ruler:` |
| 70-85% | `:warning:` |
| 90%+ | `:rotating_light:` |

## 対応プラットフォーム

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

メール、SMS、Webhook、API Server、Home Assistant、WeCom、Weixin、QQBot、Yuanbao は、各プラットフォームで検証が済むまで対象外です。

## 仕組み

`pre_gateway_dispatch` は現在のゲートウェイ会話のメタデータを取得し、以後のアダプター `send()` / `edit_message()` を監視するオブザーバーを重複しないように取り付けます。`post_llm_call` は実行中またはキャッシュ済みのエージェントから正確なコンテキスト使用量を読み、次の通知バケットを判定します。その後、既存のコールバックが先に走るように配信後コールバックを連結します。

安全に編集できる最後のアシスタント配信がある場合、プラグインはそのメッセージにコンテキスト通知を追記します。編集に失敗した場合、アダプターが編集に対応していない場合、または最終メッセージを安全に特定できない場合は、スレッドやトピックのメタデータを保ったまま同じ会話に別メッセージを送ります。

Hermes のフックは、正確なコンテキスト使用量や配信後コールバックの連結を、まだ公開プラグイン API として提供していません。そのため、このプラグインは一部の Hermes ゲートウェイのプライベート属性を読みます。内部実装が変わった場合は Hermes コアをパッチせず、このプラグインを更新してください。

## 開発

リポジトリルートでチェックを実行します。

```bash
python -m pytest -q
python -m py_compile __init__.py hermes_context_notifier.py tests/test_context_notifier.py
```

Hermes Agent のチェックアウトからプラグイン検出を確認します。

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

期待するフック:

```text
['post_llm_call', 'pre_gateway_dispatch']
```

## リポジトリ構成

- `plugin.yaml`: プラグインマニフェスト。
- `__init__.py`: Hermes プラグインのエントリーポイント。
- `hermes_context_notifier.py`: フック処理、配信監視、バケット判定、キャッシュ処理、通知送信。
- `tests/test_context_notifier.py`: 表示形式、使用量取得、バケット重複抑制、メタデータ保持、コールバック連結、分割メッセージ、編集失敗時のフォールバックを確認する回帰テスト。
- `AGENTS.md`: コーディングエージェント向け作業メモ。

## 実行時の状態

`cache.json` はプラグインの隣にセッションごとの重複抑制状態を保存し、Git では無視されます。`cache.json.tmp` はアトミック書き込み用の一時ファイルです。編集対象の選択に使う配信台帳はプロセスメモリ上にだけ存在し、ゲートウェイ再起動で消えます。

## ライセンス

[MIT](LICENSE)
