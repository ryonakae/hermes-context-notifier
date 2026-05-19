# Context Notifier Observability Logs Implementation Plan

> **For Hermes:** Use test-driven-development skill for the code slice. This plan is intentionally plugin-only and does not touch Hermes core.

**Goal:** Add safe operational logs to `hermes-context-notifier` so Slack display outcomes can be correlated with notifier decisions, delivery ledger state, edit attempts, fallback sends, and compression-induced bucket resets.

**Architecture:** Use Python logging with logger name `gateway.plugins.hermes_context_notifier` so Hermes gateway mode routes records into `~/.hermes/logs/gateway.log`. Keep logs structured-ish single-line messages and never log message bodies or raw platform payloads. Emit INFO for user-visible decision flow and WARNING only for degraded internal observation/fallback paths.

**Tech Stack:** Python 3.11, standard `logging`, existing pytest suite.

---

## Scope

- Modify only `hermes_context_notifier.py` and tests.
- Add logs for:
  - usage evaluation and skip/notice decision
  - delivery ledger records from send/edit
  - edit candidate result
  - edit success / fallback send
  - observer recording exceptions
  - split reconstruction failures
- Do not log message bodies, assistant response, raw metadata payloads, tokens beyond counts/percent/buckets, or secrets.
- Do not add a separate log file in this slice.

## Non-goals

- No Hermes core logging changes.
- No Slack API/history lookup.
- No behavior changes to notification thresholds, candidate selection, edit/fallback logic, or cache shape.
- No config toggle yet; observe first, then decide whether log volume needs a runtime setting.

---

### Task 1: Add RED tests for logger name and safe usage-decision logs

**Objective:** Prove the plugin logs usage decisions through the gateway-prefixed logger without message bodies.

**Files:**
- Modify: `tests/test_context_notifier.py`

**Steps:**
1. Add a test that uses `caplog` around `post_llm_call` with usage below 50% and asserts a record from `gateway.plugins.hermes_context_notifier` contains `event=usage_decision`, `notice=no`, and `reason=below_threshold`.
2. Assert the record does not include the assistant response body.
3. Run the focused test and confirm it fails because logging is not implemented.

**Command:**

```bash
python -m pytest tests/test_context_notifier.py::test_post_llm_call_logs_usage_decision_without_message_body -q
```

**Expected RED:** FAIL due to missing log record.

---

### Task 2: Add RED tests for edit candidate / fallback observability

**Objective:** Prove edit attempts and fallback sends become observable.

**Files:**
- Modify: `tests/test_context_notifier.py`

**Steps:**
1. Add a caplog-backed test for a successful edit path asserting `event=edit_candidate result=found` and `event=edit_result success=true` are logged.
2. Add or extend the existing failing edit test to assert `event=fallback_send reason=edit_result_failed` is logged.
3. Run focused tests and confirm failure before implementation.

**Command:**

```bash
python -m pytest tests/test_context_notifier.py::test_register_post_delivery_notice_logs_edit_candidate_and_result tests/test_context_notifier.py::test_register_post_delivery_notice_fallback_logs_reason -q
```

**Expected RED:** FAIL due to missing log records.

---

### Task 3: Implement minimal logging helpers

**Objective:** Add safe, reusable logging primitives without changing behavior.

**Files:**
- Modify: `hermes_context_notifier.py`

**Implementation outline:**

```python
import logging

logger = logging.getLogger("gateway.plugins.hermes_context_notifier")


def _log_info(event: str, **fields: Any) -> None:
    logger.info("context_notifier %s", _format_log_fields(event=event, **fields))


def _log_warning(event: str, **fields: Any) -> None:
    logger.warning("context_notifier %s", _format_log_fields(event=event, **fields))
```

`_format_log_fields` should render stable `key=value` tokens, skip `None`, and sanitize values to one line. It must not accept or include message bodies.

**Verification:** Run focused tests from Tasks 1-2.

---

### Task 4: Wire logs into decision and delivery paths

**Objective:** Emit logs at the key points needed to correlate Slack display with notifier decisions.

**Files:**
- Modify: `hermes_context_notifier.py`

**Log points:**
- `post_llm_call`: usage unavailable, notice decision after `evaluate_notification`, adapter missing, notice registered.
- `_record_delivery_from_send`: delivery record with method, platform, split count, message id presence.
- `_record_delivery_from_edit`: delivery record with method and continuation count.
- `select_edit_candidate`: candidate found, stale/unsafe/no-match reasons only when called.
- `_edit_or_send_later`: no assistant response, adapter not editable, no candidate, edit success, edit exception, edit result failure, fallback send success/failure.
- `split_chunks_for_adapter`: warning when formatting/truncation fails.
- observer wrappers: warning when record_delivery raises.

**Safety:** Do not include `content`, `assistant_response`, `metadata`, or raw exceptions with tokens/secrets. Exception class name is acceptable.

---

### Task 5: Verify, review, commit, and push

**Objective:** Land a small verified observability slice.

**Commands:**

```bash
python -m pytest -q
python -m py_compile __init__.py hermes_context_notifier.py tests/test_context_notifier.py
cd ~/.hermes/hermes-agent && python - <<'PY'
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

Then inspect `git diff --check`, commit with a conventional message, and push `main` if verification passes.

**Expected operational follow-up:** Gateway restart is required before Slack uses the new plugin code.
