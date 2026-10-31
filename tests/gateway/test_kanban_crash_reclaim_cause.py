"""The crash path and the reclaim path must name the REAL cause.

Two gaps, one card:

1. **The ordinary crash named no cause.** The gateway said "its worker stopped
   unexpectedly" and the TUI said "worker crashed (pid gone)" for EVERY
   crash. Both are true and both are useless — they are what you write when
   you know nothing, while the board knows several concrete things at crash
   time. The reaper classifies the death and puts the classification on the
   payload as ``exit_kind``/``exit_code`` (``nonzero_exit``, ``signaled``,
   ``clean_exit``, ``rate_limited``, ``unknown``), and NO renderer read them.

2. **The reclaim named no cause — and was never even delivered.** The
   ``reclaimed`` event was in NEITHER ``TERMINAL_KINDS`` (gateway) nor
   ``_KANBAN_NOTIFY_KINDS`` (TUI), so a task that hung for 2.6 hours and was
   then reclaimed produced ZERO notification. That is the SAME silent-stall
   shape ``t_f4c4ba10`` fixed for ``protocol_violation``, one event kind over.

Measured on the live boards before the fix:

* ``gamedev``: 3387 ``crashed`` events (payload keys ``pid``/``claimer``;
  ``worker_output`` on 31; ``exit_kind``/``exit_code`` on ZERO) and 387
  ``reclaimed`` events, none of them ever delivered.
* ``techdad``: 460 ``crashed`` events, 13 ``reclaimed`` events, same shape.

The rule these tests lock in, in both renderers:

* a cause key in the payload -> name that cause;
* NO cause key -> say the cause is UNKNOWN, never invent an exit code;
* a quota wall is NOT a crash;
* ``worker_output`` never appears verbatim in a message;
* ``pid``/``claimer`` stay hidden;
* a ``reclaimed`` event produces exactly ONE notification.

Both renderers are covered here, because a fix in only one of them is the
same silent half-fix ``t_f4c4ba10`` had to clean up.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.kanban_watchers_notifier import (
    TERMINAL_KINDS,
    _EVENT_FORMATTERS,
    _WAKE_KINDS,
)
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _names(task_id="T-123"):
    return SimpleNamespace(
        task_id=task_id, head=f"[board] Kanban {task_id}", title="Ship it", board_tag="[board] "
    )


def _event(**payload):
    return SimpleNamespace(payload=payload)


def _gateway_msg(kind: str, payload: dict) -> str:
    msg, _wake, _reason = _EVENT_FORMATTERS[kind](_event(**payload), _names())
    return msg


def _tui_text(kind: str, payload: dict) -> str:
    from tui_gateway.session_notifications import _format_kanban_event_text

    ev = SimpleNamespace(kind=kind, payload=payload)
    task = SimpleNamespace(title="t", assignee="worker", result=None)
    return _format_kanban_event_text({"task_id": "t_abc"}, task, ev, "")


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)
        event._gateway_accepted = True


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Fresh HERMES_HOME with an initialized default board."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "crash-reclaim.db"))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _subscribed_task(conn, *, delivery_mode="notify+wake", title="hangs"):
    tid = kb.create_task(conn, title=title, assignee="worker")
    kbn.add_notify_sub(
        conn, task_id=tid, platform="telegram", chat_id="chat-1",
        delivery_mode=delivery_mode,
    )
    return tid


# ---------------------------------------------------------------------------
# 1. A named exit code is named (AC1)
# ---------------------------------------------------------------------------


def test_a_nonzero_exit_names_the_exit_code_in_both_renderers():
    """AC1: ``exit_kind='nonzero_exit'`` + ``exit_code=1`` renders the code."""
    payload = {"pid": 4242, "claimer": "host:1", "exit_kind": "nonzero_exit", "exit_code": 1}

    gateway_msg = _gateway_msg("crashed", payload)
    assert "exited with code 1" in gateway_msg
    assert "retried automatically" in gateway_msg

    tui_text = _tui_text("crashed", payload)
    assert "exited with code 1" in tui_text
    assert "will retry" in tui_text


def test_the_two_renderers_agree_on_the_named_cause():
    """The gateway and the TUI must not drift: same cause, same wording.

    Two copies of the wording is how they drift apart — the TUI copy was
    hardcoded to "gave up after repeated spawn failures" while the gateway
    read the payload. The shared helper is the fix; this is the lock.
    """
    for payload in (
        {"exit_kind": "nonzero_exit", "exit_code": 1},
        {"exit_kind": "signaled", "exit_code": 9},
        {"exit_kind": "clean_exit", "exit_code": 0},
        {"exit_kind": "rate_limited", "exit_code": 75},
        {"exit_kind": "unknown"},
        {},
    ):
        gateway_msg = _gateway_msg("crashed", payload)
        tui_text = _tui_text("crashed", payload)
        cause = gateway_msg.split("— ", 1)[1].split("; it will be retried")[0]
        assert cause in tui_text, (payload, cause, tui_text)


# ---------------------------------------------------------------------------
# 2. A signal is named (AC2)
# ---------------------------------------------------------------------------


def test_a_signaled_worker_names_the_signal_in_both_renderers():
    """AC2: ``exit_kind='signaled'`` names the signal number."""
    payload = {"pid": 4242, "claimer": "host:1", "exit_kind": "signaled", "exit_code": 9}

    gateway_msg = _gateway_msg("crashed", payload)
    assert "killed by signal 9" in gateway_msg
    assert "signal" in gateway_msg.lower()

    tui_text = _tui_text("crashed", payload)
    assert "killed by signal 9" in tui_text


def test_a_clean_exit_is_not_called_a_random_crash():
    """``clean_exit`` is a protocol violation, not a random crash.

    The worker finished (or died) but never reported a result. Saying "it
    crashed" hides the one thing that matters: the worker misbehaved.
    """
    payload = {"pid": 4242, "exit_kind": "clean_exit", "exit_code": 0}

    gateway_msg = _gateway_msg("crashed", payload)
    assert "without reporting a result" in gateway_msg
    assert "stopped unexpectedly" not in gateway_msg

    tui_text = _tui_text("crashed", payload)
    assert "without reporting a result" in tui_text


def test_a_quota_wall_is_not_called_a_crash():
    """``rate_limited`` is a quota wall — NOT a failure.

    The dispatcher requeues a rate-limited worker WITHOUT counting a failure,
    so announcing it as a crash would misdescribe a quota window as a bug.
    """
    payload = {"pid": 4242, "exit_kind": "rate_limited", "exit_code": 75}

    gateway_msg = _gateway_msg("crashed", payload)
    assert "quota" in gateway_msg.lower()
    assert "rate limit" in gateway_msg.lower()

    tui_text = _tui_text("crashed", payload)
    assert "quota" in tui_text.lower()


# ---------------------------------------------------------------------------
# 3. No cause key -> no invented cause (AC3, the t_94a810ea lesson)
# ---------------------------------------------------------------------------


def test_a_crash_without_exit_keys_keeps_a_truthful_generic_message():
    """AC3: no exit keys -> a truthful generic message, no invented code.

    This is the LIVE-BOARD shape: 3387 of 3387 ``crashed`` events on
    ``gamedev`` carried ``pid``/``claimer`` and no exit keys, because every
    one of them was classified ``unknown`` (the pid was absent from the exit
    registry). The board genuinely does not know how those workers died, so
    the message must say so rather than imply a crash it cannot substantiate
    or invent an exit code.
    """
    payload = {"pid": 4242, "claimer": "host:1"}

    gateway_msg = _gateway_msg("crashed", payload)
    assert "cause is unknown" in gateway_msg
    assert "retried automatically" in gateway_msg
    # No invented code.
    for forbidden in ("code 0", "code 1", "exit code", "signal"):
        assert forbidden not in gateway_msg, (forbidden, gateway_msg)

    tui_text = _tui_text("crashed", payload)
    assert "cause is unknown" in tui_text
    for forbidden in ("code 0", "code 1", "exit code", "signal"):
        assert forbidden not in tui_text, (forbidden, tui_text)


def test_an_unknown_exit_kind_says_the_cause_is_unknown():
    """``exit_kind='unknown'`` -> the cause is unknown, plainly."""
    payload = {"pid": 4242, "exit_kind": "unknown"}

    gateway_msg = _gateway_msg("crashed", payload)
    assert "cause is unknown" in gateway_msg
    assert "code" not in gateway_msg.lower()

    tui_text = _tui_text("crashed", payload)
    assert "cause is unknown" in tui_text


def test_a_missing_code_never_leaks_the_machine_token():
    """``exit_kind`` without a code -> the half-cause, in human words.

    The exit kind is an internal token (``nonzero_exit``); a human message
    must not print it. The cause is only half known without the code, so the
    phrase says the half that is true and stops there.
    """
    for kind in ("nonzero_exit", "signaled"):
        payload = {"pid": 4242, "exit_kind": kind}
        for message in (_gateway_msg("crashed", payload), _tui_text("crashed", payload)):
            assert kind not in message, message
            assert "code None" not in message, message
            assert "exited abnormally" in message or "killed by a signal" in message, message


def test_a_legacy_crash_names_the_exit_from_its_error_text():
    """Legacy events carry the exit only in their error text.

    The producer gained ``exit_kind``/``exit_code`` later, so already-queued
    events name the exit in ``error`` ("pid 12345 exited with code 1").
    Parse it so those events render their real cause too.
    """
    gateway_msg = _gateway_msg(
        "crashed", {"pid": 4242, "error": "pid 12345 exited with code 1"})
    assert "exited with code 1" in gateway_msg

    tui_text = _tui_text(
        "crashed", {"pid": 4242, "error": "pid 12345 killed by signal 9"})
    assert "killed by signal 9" in tui_text


def test_a_malformed_payload_never_raises():
    """A notification must never raise on a malformed payload."""
    for payload in (
        {"exit_kind": "nonzero_exit"},
        {"exit_kind": "signaled", "exit_code": "not-a-number"},
        {"exit_kind": None, "exit_code": None},
        {"exit_code": True},
        {"exit_kind": 12345},
        {},
    ):
        gateway_msg = _gateway_msg("crashed", payload)
        assert gateway_msg, payload
        tui_text = _tui_text("crashed", payload)
        assert tui_text, payload


# ---------------------------------------------------------------------------
# 4. A reclaim notifies exactly once and names the reason (AC4)
# ---------------------------------------------------------------------------


def test_reclaimed_is_in_every_kind_list():
    """The event must be CLAIMED, or the branch is dead code.

    The link that was broken: ``reclaimed`` was in no subscriber's kind list,
    so the transition that reclaimed a hung task produced zero notification.
    """
    assert "reclaimed" in TERMINAL_KINDS
    assert "reclaimed" in _WAKE_KINDS

    from tui_gateway.session_notifications import _KANBAN_NOTIFY_KINDS

    assert "reclaimed" in _KANBAN_NOTIFY_KINDS


def test_a_reclaimed_event_names_the_reclaim_reason():
    """AC4: the reclaim reason reaches the reader."""
    payload = {
        "manual": True,
        "reason": "watchdog: hard reclaim of stuck task (running 2.1h with no completion)",
    }

    gateway_msg = _gateway_msg("reclaimed", payload)
    assert "watchdog: hard reclaim of stuck task" in gateway_msg
    assert "2.1h" in gateway_msg

    tui_text = _tui_text("reclaimed", payload)
    assert "watchdog: hard reclaim of stuck task" in tui_text


def test_a_reclaimed_event_without_a_reason_still_describes_the_reclaim():
    """No reason text -> a truthful description, never an invented one."""
    for payload in (
        {"manual": True, "reason": None},
        {"stale_lock": "host:1", "heartbeat_stale": True},
        {"stale_lock": "host:1", "heartbeat_stale": False},
        {},
    ):
        gateway_msg = _gateway_msg("reclaimed", payload)
        assert "reclaim" in gateway_msg.lower(), payload
        tui_text = _tui_text("reclaimed", payload)
        assert "reclaim" in tui_text.lower(), payload


def test_a_reclaimed_event_produces_exactly_one_notification(kanban_home, monkeypatch):
    """AC4: the reclaim reaches the subscriber — exactly once.

    Before the fix this produced ZERO notifications: the kind was in no
    subscriber's kind list, so the claim returned nothing.
    """
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kanban_home / "crash-reclaim.db"))
    kb.init_db()

    with kbc.connect() as conn:
        tid = _subscribed_task(conn)
        kb._append_event(
            conn, tid, "reclaimed",
            {"manual": True, "reason": "watchdog: stale running task (heartbeat age 4079s)"},
        )

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1, (
        f"a reclaim must produce exactly one notification; got {len(adapter.sent)}"
    )
    text = adapter.sent[0]["text"]
    assert tid in text
    assert "reclaim" in text.lower()
    assert "watchdog" in text.lower()

    # The cursor advanced: the event is claimed and not re-delivered.
    with kbc.connect() as conn:
        _, remaining = kbn.unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            kinds=list(TERMINAL_KINDS),
        )
    assert remaining == [], "the claimed event must not be re-delivered"


def test_a_reclaimed_event_wakes_the_origin(kanban_home, monkeypatch):
    """A reclaimed task is requeued, but a human may want to know it hung.

    The reclaim hands a decision back to the origin, which must take a turn.
    """
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kanban_home / "crash-reclaim.db"))
    kb.init_db()

    with kbc.connect() as conn:
        tid = _subscribed_task(conn, delivery_mode="notify+wake")
        kb._append_event(
            conn, tid, "reclaimed",
            {"manual": True, "reason": "watchdog: hard reclaim of stuck task (running 2.6h with no completion)"},
        )

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.handled) == 1, (
        f"a reclaim must wake the origin; got {len(adapter.handled)} wake turns"
    )
    wake_text = getattr(adapter.handled[0], "text", "") or ""
    assert tid in wake_text


# ---------------------------------------------------------------------------
# 5. worker_output never appears verbatim (AC5)
# ---------------------------------------------------------------------------


def test_worker_output_never_appears_verbatim_in_a_message():
    """AC5: the raw worker output stays in the payload and the log.

    ``worker_output`` is the worker's own last words ("Provider said: HTTP
    429: The usage limit has been reached"). It is raw: thousands of
    characters, full of ANSI escape codes. A message that quotes it verbatim
    is unreadable in a chat. The precedent the ``gave_up`` copy already sets
    is to CLIP it.
    """
    raw = (
        "\x1b[2;3mInitializing agent...\x1b[0m Error: Unknown skill(s): "
        "gameqa-strato-ship-verify " * 40
    )
    payload = {"pid": 4242, "exit_kind": "nonzero_exit", "exit_code": 1,
               "worker_output": raw}

    for message in (
        _gateway_msg("crashed", payload),
        _tui_text("crashed", payload),
        _gateway_msg("reclaimed", dict(payload)),
        _tui_text("reclaimed", dict(payload)),
    ):
        assert raw not in message
        # The raw ANSI must not survive either.
        assert "\x1b[" not in message
        # And the message must stay short — a quoted dump is the failure.
        assert len(message) < 600, len(message)


def test_the_worker_output_excerpt_is_short_and_readable():
    """The excerpt is cleaned and capped, so it can be quoted at all."""
    raw = "\x1b[38;2;255;0;0mProvider said: HTTP 429: The usage limit has been reached\x1b[0m"
    payload = {"worker_output": raw}

    gateway_msg = _gateway_msg("crashed", payload)
    assert "HTTP 429" in gateway_msg
    assert "\x1b[" not in gateway_msg


def test_a_long_worker_output_is_clipped_not_dumped():
    """A thousand-char worker output must not become a thousand-char ping."""
    payload = {"worker_output": "x" * 5000}

    gateway_msg = _gateway_msg("crashed", payload)
    assert len(gateway_msg) < 600, len(gateway_msg)

    tui_text = _tui_text("crashed", payload)
    assert len(tui_text) < 600, len(tui_text)


# ---------------------------------------------------------------------------
# 6. pid / claimer stay hidden (AC6)
# ---------------------------------------------------------------------------


def test_pid_and_claimer_stay_hidden():
    """AC6: a pid is noise to a human reader."""
    payload = {
        "pid": 4242,
        "claimer": "DESKTOP-7G7SGH3:18860",
        "exit_kind": "nonzero_exit",
        "exit_code": 1,
    }

    for message in (
        _gateway_msg("crashed", payload),
        _tui_text("crashed", payload),
        _gateway_msg("reclaimed", payload),
        _tui_text("reclaimed", payload),
    ):
        assert "4242" not in message, message
        assert "DESKTOP-7G7SGH3" not in message, message
        assert "claimer" not in message, message
        assert "pid" not in message, message


def test_the_reclaim_reason_does_not_leak_the_internal_lock():
    """The reclaim reason is operator text and is clipped to one line."""
    payload = {
        "reason": "watchdog: stale running task " + ("very long detail " * 100),
        "stale_lock": "DESKTOP-7G7SGH3:18860",
        "worker_pid": 4242,
    }

    gateway_msg = _gateway_msg("reclaimed", payload)
    assert "\n" not in gateway_msg
    assert len(gateway_msg) < 600, len(gateway_msg)
    assert "DESKTOP-7G7SGH3" not in gateway_msg
    assert "4242" not in gateway_msg


# ---------------------------------------------------------------------------
# 7. The i18n wake key exists for the new kind (parity is asserted elsewhere)
# ---------------------------------------------------------------------------


def test_the_reclaimed_wake_key_exists_in_every_locale():
    """A kind in ``_WAKE_KINDS`` needs an i18n key.

    The wake text is built from ``gateway.kanban.wake.<kind>``; a missing key
    renders as the bare key string. ``tests/agent/test_i18n.py`` asserts
    key-set parity with English, so a missing key fails that suite — this
    test names the requirement locally.
    """
    from agent.i18n import SUPPORTED_LANGUAGES
    from agent.i18n import t as translate

    for lang in SUPPORTED_LANGUAGES:
        value = translate("gateway.kanban.wake.reclaimed", lang=lang)
        assert value != "gateway.kanban.wake.reclaimed", (
            f"{lang}.yaml is missing the reclaimed wake key"
        )
        assert value.strip(), lang


# ---------------------------------------------------------------------------
# 8. The LIVE TUI path (the split-module rebinding trap)
# ---------------------------------------------------------------------------


def test_the_live_tui_path_renders_the_cause_without_a_name_error():
    """The TUI module is SPLIT: its bodies are rebound onto server.py's globals.

    ``method_ctx.bind_module`` rewrites every function onto ``server``'s
    globals, so an imported helper referenced by its BARE name resolves in
    ``server``'s namespace — where it does not exist. The failure is a
    ``NameError`` at CALL time, not import time, so imports and module-level
    checks both pass and the bug only shows up when a crash is actually
    rendered.

    The house pattern is an ALIASED import referenced under the alias. This
    test exercises the REAL caller (``server._format_kanban_event_text``,
    the rebound function), not the submodule's own copy.
    """
    import tui_gateway.server as server

    task = SimpleNamespace(title="t", assignee="worker", result=None)
    for kind, payload in (
        ("crashed", {"exit_kind": "nonzero_exit", "exit_code": 1}),
        ("crashed", {"pid": 4242, "claimer": "host:1"}),
        ("reclaimed", {"reason": "watchdog: stale running task (heartbeat age 4079s)"}),
    ):
        ev = SimpleNamespace(kind=kind, payload=payload)
        text = server._format_kanban_event_text({"task_id": "t_abc"}, task, ev, "board")
        assert text, (kind, payload)
        assert "t_abc" in text, (kind, payload)
    assert "exited with code 1" in server._format_kanban_event_text(
        {"task_id": "t_abc"}, task,
        SimpleNamespace(kind="crashed", payload={"exit_kind": "nonzero_exit", "exit_code": 1}),
        "board",
    )
