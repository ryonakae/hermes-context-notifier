# Multi-Platform Context Notifier Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Extend `hermes-context-notifier` from Slack-only context usage notices to supported Hermes messaging platforms while preserving the current safe side-message behavior.

**Architecture:** Keep this as a standalone plugin. Generalize the existing Slack-specific metadata capture into platform-neutral session capture, then send a short post-delivery side-message through the active platform adapter. Do not edit Hermes core or mutate the final Hermes response; use `register_post_delivery_callback` exactly like the current Slack implementation.

**Tech Stack:** Python 3.11, Hermes Agent gateway hooks (`pre_gateway_dispatch`, `post_llm_call`), `BasePlatformAdapter.register_post_delivery_callback`, pytest.

---

##調査結果

### Hermes が対応している platform enum

`/Users/ryo.nakae/.hermes/hermes-agent/gateway/config.py` の `Platform` enum:

- `local`
- `telegram`
- `discord`
- `whatsapp`
- `slack`
- `signal`
- `mattermost`
- `matrix`
- `homeassistant`
- `email`
- `sms`
- `dingtalk`
- `api_server`
- `webhook`
- `feishu`
- `wecom`
- `wecom_callback`
- `weixin`
- `bluebubbles`
- `qqbot`
- `yuanbao`

User-facing messaging platform として対象にするのは、`local` / `api_server` / `webhook` / `homeassistant` に加えて、Email/SMS や実機制約が強い platform を除いた以下:

- Slack
- Telegram
- Discord
- WhatsApp
- Signal
- Mattermost
- Matrix
- DingTalk
- Feishu
- BlueBubbles / iMessage

今回やらない対象:

- Email / SMS: 通知が別メール・別SMSになってうるさい、または課金が絡む。
- WeCom / WeCom Callback / Weixin / QQBot / Yuanbao: platform 固有制約や実機確認が必要。
- API Server / Webhook / HomeAssistant: 通常の会話 UI というより integration surface。

### Hermes 応答の最後に通知メッセージを出せるか

結論: **別メッセージとしてなら、原則ほぼ全 gateway platform で可能**。

根拠:

- `gateway/run.py` は user-originated message に対して `pre_gateway_dispatch` hook を呼ぶ。
- `run_agent.py` は turn 完了後に `post_llm_call` hook を呼び、`session_id`, `model`, `platform` を渡す。
- `gateway/platforms/base.py` は message processing の `finally` で `pop_post_delivery_callback(session_key, generation=...)` を呼び、callback を実行する。
- 各 adapter は `send(chat_id, content, reply_to=None, metadata=None)` を実装している。
- 現在の Slack 実装と同じく、plugin 側で post-delivery callback に `adapter.send(...)` を登録すれば「Hermes の main response の後」に side-message を出せる。

注意: 「最後の Hermes メッセージを編集して末尾に追記」は今回やらない。Slack/Telegram/Discord/Mattermost/Matrix/Feishu/DingTalk/WhatsApp など `edit_message` を持つ adapter はあるが、post-delivery callback には最後の message id と最終本文が渡らない。plugin 単体で monkey patch して追跡するのは streaming / 長文分割 / media / footer と衝突しやすいので避ける。

### platform 別の実装見込み

| Platform | side-message | 同じ conversation/thread への送信 | 備考 |
| --- | --- | --- | --- |
| Slack | 実装済み | `metadata.thread_id` / `thread_ts` | 現行基準 |
| Telegram | 可能 | `metadata.thread_id` / `message_thread_id` | forum topic を維持するには metadata が重要 |
| Discord | 可能 | thread channel / metadata / chat_id | thread 内イベントなら chat_id 自体が thread channel の可能性あり |
| WhatsApp | 可能 | chat_id | edit_message はあるが使わない |
| Signal | 可能 | chat_id | thread概念は薄い |
| Mattermost | 可能 | `reply_to` / root post | root_id 周りは platform 固有確認が必要 |
| Matrix | 可能 | room_id / thread metadata | thread relation は adapter 側に任せる |
| DingTalk | 可能 | chat_id | edit_message はあるが使わない |
| Feishu | 可能 | chat_id | edit_message はあるが使わない |
| BlueBubbles | 可能 | chat_id | iMessage なので短く |

実装対象は **Slack + Telegram + Discord + Mattermost + Matrix + Feishu + DingTalk + WhatsApp + Signal + BlueBubbles** に限定する。Email/SMS/HomeAssistant/Webhook/API Server/Yuanbao/QQBot/WeCom/WeCom Callback/Weixin はこの計画では実装しない。

---

## 実装方針

### 方針

1. plugin 名は `hermes-context-notifier` のまま。
2. 通知は別 side-message のまま。
3. Hermes core は触らない。
4. platform 対応は allowlist 方式にする。
5. allowlist は今回の対象 platform だけに固定する。Email/SMS/WeCom/Weixin/QQBot/Yuanbao などの opt-in 拡張は、この計画には含めない。
6. formatter は既存のまま: `⚠️ Context: 85% (230K/270K), gpt-5.5`。
7. platform 固有 thread metadata は小さな helper に閉じ込める。

### なぜ allowlist 方式か

`BasePlatformAdapter` の post-delivery callback は広く使えるが、Email/SMS/Weixin/Yuanbao などは user experience や送信制約が platform ごとに違う。いきなり全 platform で通知すると、SMS 課金やメール増殖のような副作用が出る。この計画では、通常の chat UI として扱いやすい platform だけを対象にする。

---

## Task 1: platform-neutral metadata capture helper を追加する

**Objective:** Slack 固定の `is_slack_event` / `capture_gateway_context` を platform-neutral にする。

**Files:**
- Modify: `hermes_context_notifier.py`
- Modify: `tests/test_context_notifier.py`

**Step 1: failing tests を追加**

`tests/test_context_notifier.py` に以下を追加する。

```python
def test_capture_gateway_context_supports_telegram_and_keeps_thread_metadata():
    adapter = DummyAdapter()
    source = SimpleNamespace(
        platform=SimpleNamespace(value="telegram"),
        chat_id="chat-1",
        thread_id="topic-42",
    )
    event = SimpleNamespace(source=source, message_id="msg-1", metadata={"message_thread_id": "topic-42"})
    entry = SimpleNamespace(session_key="key", session_id="sid")
    session_store = SimpleNamespace(get_or_create_session=lambda src: entry)
    gateway = SimpleNamespace(adapters={"telegram": adapter})

    meta = hcn.capture_gateway_context(event=event, gateway=gateway, session_store=session_store)

    assert meta["platform"] == "telegram"
    assert meta["chat_id"] == "chat-1"
    assert meta["thread_id"] == "topic-42"
    assert meta["metadata"] == {"message_thread_id": "topic-42"}
```

**Step 2: test failure を確認**

Run:

```bash
python -m pytest -q tests/test_context_notifier.py::test_capture_gateway_context_supports_telegram_and_keeps_thread_metadata
```

Expected: FAIL。現状は Slack 以外を無視する。

**Step 3: 実装**

`hermes_context_notifier.py` に以下の helper を追加する。

```python
DEFAULT_SUPPORTED_PLATFORMS = {
    "slack",
    "telegram",
    "discord",
    "mattermost",
    "matrix",
    "whatsapp",
    "signal",
    "feishu",
    "dingtalk",
    "bluebubbles",
}


def is_supported_event(event: Any) -> bool:
    source = getattr(event, "source", None)
    return _platform_value(getattr(source, "platform", None)) in DEFAULT_SUPPORTED_PLATFORMS
```

既存 `is_slack_event` は互換用に残すか、テストを更新して削除する。`capture_gateway_context` は `platform = _platform_value(source.platform)` を保存する。

**Step 4: adapter lookup を汎用化する**

既存の Slack fallback を以下のようにする。

```python
def _adapter_for_platform(gateway: Any, platform_key: Any, platform: str) -> Any:
    adapters = getattr(gateway, "adapters", {}) or {}
    try:
        adapter = adapters.get(platform_key)
    except TypeError:
        adapter = None
    if adapter is not None:
        return adapter
    for key, candidate in adapters.items():
        if _platform_value(key) == platform:
            return candidate
    return None
```

**Step 5: tests を実行**

```bash
python -m pytest -q tests/test_context_notifier.py
```

Expected: PASS。

---

## Task 2: side-message metadata builder を追加する

**Objective:** `adapter.send()` に渡す `metadata` を platform ごとに安全に作る。

**Files:**
- Modify: `hermes_context_notifier.py`
- Modify: `tests/test_context_notifier.py`

**Step 1: failing tests を追加**

```python
def test_notice_metadata_for_slack_uses_thread_id():
    meta = {"platform": "slack", "thread_id": "177.1", "metadata": {}}
    assert hcn.notice_send_metadata(meta) == {"thread_id": "177.1"}


def test_notice_metadata_for_telegram_preserves_message_thread_id():
    meta = {"platform": "telegram", "thread_id": "42", "metadata": {"message_thread_id": "42"}}
    assert hcn.notice_send_metadata(meta) == {"thread_id": "42", "message_thread_id": "42"}


def test_notice_metadata_returns_none_when_no_thread_context():
    meta = {"platform": "signal", "thread_id": None, "metadata": {}}
    assert hcn.notice_send_metadata(meta) is None
```

**Step 2: 実装**

```python
def notice_send_metadata(meta: dict[str, Any]) -> dict[str, Any] | None:
    original = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    metadata: dict[str, Any] = {}

    thread_id = meta.get("thread_id")
    if thread_id:
        metadata["thread_id"] = thread_id

    # Telegram forum topics / adapter-specific metadata should survive.
    for key in ("message_thread_id", "thread_ts", "root_id"):
        value = original.get(key)
        if value:
            metadata[key] = value

    return metadata or None
```

**Step 3: `_send_later` を変更**

`_send_later(adapter, chat_id, text, thread_id, loop)` を `meta` 受け取りに変更する。

```python
def _send_later(adapter: Any, chat_id: str, text: str, meta: dict[str, Any], loop: asyncio.AbstractEventLoop | None) -> None:
    metadata = notice_send_metadata(meta)
    async def _send() -> None:
        await adapter.send(chat_id, text, metadata=metadata)
```

**Step 4: callback tests を更新**

既存 `test_register_post_delivery_notice_*` を `meta={...}` 形式に更新する。

---

## Task 3: cache record を platform-aware にする

**Objective:** cache の session record に platform を保存し、session_key 単位の dedupe を platform 横断で安全にする。

**Files:**
- Modify: `hermes_context_notifier.py`
- Modify: `tests/test_context_notifier.py`

**Step 1: record update の Slack 固定を外す**

現在:

```python
"platform": "slack",
```

変更:

```python
"platform": meta.get("platform") or platform or "",
```

**Step 2: test を追加**

```python
def test_post_llm_call_records_non_slack_platform(tmp_path, monkeypatch):
    # cache path monkeypatch or tmp file pattern を使う。
    # telegram meta を _SESSION_CONTEXT_BY_ID に入れ、evaluate_notification が発火する usage を返す。
    # record["platform"] == "telegram" を確認する。
```

既存構造では `CACHE_PATH` が定数なので、`monkeypatch.setattr(hcn, "CACHE_PATH", tmp_path / "cache.json")` を使う。

---

## Task 4: platform allowlist を明示する

**Objective:** 対象 platform を明示し、Email/SMS や実機制約が強い platform をこの実装範囲から外す。

**Files:**
- Modify: `hermes_context_notifier.py`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Test: `tests/test_context_notifier.py`

**Step 1: allowlist を定数として明示する**

YAGNI 優先で、plugin repo 直下 `config.json` は作らない。定数 allowlist で始める。

今回は `DEFAULT_SUPPORTED_PLATFORMS` のみ。Email/SMS/WeCom/WeCom Callback/Weixin/QQBot/Yuanbao は allowlist に入れない。

**Step 2: README に supported / out-of-scope を明記**

README には以下を書く。

```markdown
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
- BlueBubbles

Out of scope for this plan:
- Email and SMS, because context notices would create extra emails/texts.
- Webhook, API Server, and Home Assistant, because they are integration surfaces rather than normal chat UI.
- WeCom, WeCom Callback, Weixin, QQBot, and Yuanbao because they need platform-specific validation.
```

---

## Task 5: post-delivery callback registration を汎用化する

**Objective:** `register_post_delivery_notice` の Slack assumptions を消す。

**Files:**
- Modify: `hermes_context_notifier.py`
- Test: `tests/test_context_notifier.py`

**Step 1: signature を変更**

```python
def register_post_delivery_notice(
    *,
    adapter: Any,
    session_key: str,
    chat_id: str,
    text: str,
    meta: dict[str, Any],
    loop: asyncio.AbstractEventLoop | None,
    generation: int | None = None,
) -> None:
```

**Step 2: callback 内の send を meta-based にする**

```python
_send_later(adapter, chat_id, text, meta, loop)
```

**Step 3: post_llm_call で渡す**

```python
register_post_delivery_notice(
    adapter=adapter,
    session_key=session_key,
    chat_id=chat_id,
    text=notice["text"],
    meta=meta,
    loop=meta.get("loop"),
    generation=_adapter_generation(adapter, session_key),
)
```

---

## Task 6: docs と plan を実態に合わせる

**Objective:** Slack-only 表現を platform-aware 表現へ更新する。

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `.hermes/plans/2026-04-29_223558-multi-platform-context-notifier.md` if implementation diverges

**README 更新ポイント:**

- 冒頭: `Slack is the only supported platform today` を変更。
- Runtime behavior: `Slack gateway conversations` を `supported gateway conversations` へ。
- Private API: `_running_agents`, `_agent_cache`, `_active_sessions`, `_post_delivery_callbacks` は維持。
- Platform support matrix を追加。

**AGENTS 更新ポイント:**

- `pre_gateway_dispatch` metadata capture が multi-platform になったこと。
- 新しい helper 名。
- Email/SMS/WeCom/Weixin/QQBot/Yuanbao は out of scope の理由。

---

## Task 7: validation

**Objective:** 実装が plugin として load され、既存 Slack 動作を壊していないことを確認する。

**Commands:**

```bash
cd /Users/ryo.nakae/.hermes/plugins/hermes-context-notifier
python -m pytest -q
python -m py_compile __init__.py hermes_context_notifier.py tests/test_context_notifier.py
```

Plugin discovery:

```bash
cd /Users/ryo.nakae/.hermes/hermes-agent
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

Expected:

```text
found= True
enabled= True
error= None
hooks= ['post_llm_call', 'pre_gateway_dispatch']
```

Manual validation:

1. Restart gateway from normal shell if Safehouse cannot access LaunchAgents.
2. Send a Slack message in a thread and confirm existing behavior still works.
3. If Telegram or Discord credentials are configured, send a long-context test message and temporarily lower threshold in test branch only, or seed cache/usage via unit tests rather than real spam.

---

## Recommended rollout

### Phase 1: Thread/chat platforms

Implement Tasks 1-5 with:

```python
{
    "slack",
    "telegram",
    "discord",
    "mattermost",
    "matrix",
}
```

This covers the main thread/chat platforms with similar UX and low surprise.

### Phase 2: Direct-message style chat platforms

After Phase 1 is stable, add:

```python
{
    "whatsapp",
    "signal",
    "feishu",
    "dingtalk",
    "bluebubbles",
}
```

These are still in scope, but manual UX validation matters.

### Explicitly out of scope

Do not implement these in this plan:

```python
{
    "email",
    "sms",
    "wecom",
    "wecom_callback",
    "weixin",
    "qqbot",
    "yuanbao",
    "api_server",
    "webhook",
    "homeassistant",
}
```

Reasons:

- Email/SMS can be noisy or costly.
- Webhook/API Server/HomeAssistant are integration surfaces rather than normal chat UI.
- Weixin/WeCom may have reply-window or bot mode constraints.
- Yuanbao has `group_code` in `send` signature, so plain `chat_id` may be insufficient.
- QQBot group/topic behavior should be manually checked.

---

## Acceptance criteria

- Existing Slack notice behavior remains unchanged.
- Unit tests cover at least Slack, Telegram, Discord-style generic metadata, and unsupported platform skip.
- `post_llm_call` no longer writes `platform: slack` unconditionally.
- Side-message delivery continues to use post-delivery callback; final Hermes message editing is not implemented.
- README states exactly which platforms are supported by default and which are out of scope.
- `python -m pytest -q` and `py_compile` pass.
- Plugin discovery reports enabled with no error.
