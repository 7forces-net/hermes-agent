"""A ``protocol_violation`` event must notify AND wake — it used to do neither.

A protocol violation is the reaper's clean-exit crash: a worker that exited
rc=0 while the task was still ``running``. It finished (or died) but never
called ``kanban_complete``/``kanban_block``, so the task has no verdict.

``hermes_cli/kanban_db_dispatch.py`` emitted the event of kind
``protocol_violation`` all along, but that kind was in neither
``TERMINAL_KINDS`` nor ``_WAKE_KINDS`` (gateway) nor ``_KANBAN_NOTIFY_KINDS``
(TUI). So NO subscriber ever claimed it: the transition that strands a task
produced zero notification and the task stalled silently. That is the same
shape as the ``block_loop_detected`` gap fixed in PR #62712.

Measured on the live board before the fix: 3387 ``crashed`` events, 0 of any
kind carrying ``protocol_violations``, and 0 events of kind
``protocol_violation`` — the path had simply never fired on that board.

These tests lock all three links of the chain:

1. the PRODUCER emits the event with the cause keys a renderer can name;
2. the gateway notifier delivers it and wakes the origin;
3. the TUI notifier renders it with a real reason.

Plus the controls: the generic ``crashed`` message is unchanged, ``pid``/
``claimer`` stay hidden, and a crash WITHOUT violation keys never claims the
word "violation".
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_notify as kbn


# ---------------------------------------------------------------------------
# Fakes / harness
# ---------------------------------------------------------------------------


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


def _wake_text(adapter):
    assert len(adapter.handled) == 1, (
        f"expected exactly one wake turn, got {len(adapter.handled)}"
    )
    return getattr(adapter.handled[0], "text", "") or ""


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Fresh HERMES_HOME with an initialized default board."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "protocol-violation.db"))
    # The reaper's launch-window grace would swallow an immediately-claimed
    # task; the crash-detection tests pin it to 0 for the same reason.
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# 1. The producer: a clean-exit violation must emit a nameable event
# ---------------------------------------------------------------------------


def _drive_clean_exit(conn, tid, fake_pid):
    """Claim ``tid`` and run one reaper pass for a worker that exited rc=0.

    ``rc=0`` while the task is still ``running`` is exactly the protocol
    violation the reaper is supposed to detect.
    """
    import hermes_cli.kanban_db as _kb

    host_prefix = _kb._claimer_id().split(":", 1)[0]
    claimed = _kb.claim_task(conn, tid, claimer=f"{host_prefix}:mock")
    assert claimed is not None, "task was not claimable for the next attempt"
    kbd._set_worker_pid(conn, tid, fake_pid)
    # POSIX wait status for exit code 0: (code << 8).
    kbd._record_worker_exit(fake_pid, 0)
    original_alive = _kb._pid_alive
    _kb._pid_alive = lambda p: False
    try:
        return kbd.detect_crashed_workers(conn)
    finally:
        _kb._pid_alive = original_alive


def test_clean_exit_emits_a_protocol_violation_event_with_cause_keys(kanban_home):
    """The producer must emit a ``protocol_violation`` event a renderer can name.

    The event kind itself is the load-bearing part: a renderer that only knows
    ``crashed`` can never see this transition, because the reaper books a
    clean-exit violation under its own kind.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="exits without reporting", assignee="worker")
        _drive_clean_exit(conn, tid, 995001)

        events = kb.list_events(conn, tid)
        kinds = [e.kind for e in events]
        assert "protocol_violation" in kinds, (
            f"a clean exit while running must emit a protocol_violation event; got {kinds}"
        )
        # The generic crash kind must NOT also fire for the same run — one
        # transition, one event, or the reader gets two contradictory pings.
        assert "crashed" not in kinds, (
            f"a protocol violation must not also be booked as a plain crash; got {kinds}"
        )

        payload = next(e.payload or {} for e in events if e.kind == "protocol_violation")
        assert payload.get("protocol_violation") is True
        assert payload.get("exit_code") == 0
        assert payload.get("exit_kind") == "clean_exit"


def test_protocol_violation_event_is_claimable_by_a_subscriber(kanban_home):
    """The event must be visible to ``claim_unseen_events_for_sub``.

    This is the link that was broken: the event was written but the kind was
    in no subscriber's kind list, so the claim returned nothing and the task
    stalled with no notification.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="exits without reporting", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        _drive_clean_exit(conn, tid, 995002)

        from gateway.kanban_watchers_notifier import TERMINAL_KINDS

        _, events = kbn.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=list(TERMINAL_KINDS),
        )
        assert [e.kind for e in events] == ["protocol_violation"], (
            "the gateway's terminal-kind list must claim the protocol violation"
        )


# ---------------------------------------------------------------------------
# 2. The gateway notifier: it must deliver AND wake
# ---------------------------------------------------------------------------


def _subscribed_task(conn, *, delivery_mode="notify+wake", title="exits without reporting"):
    tid = kb.create_task(
        conn,
        title=title,
        assignee="worker",
        session_id="agent:main:telegram:dm:chat-1",
    )
    kbn.add_notify_sub(
        conn,
        task_id=tid,
        platform="telegram",
        chat_id="chat-1",
        chat_type="dm",
        delivery_mode=delivery_mode,
    )
    return tid


def test_protocol_violation_notifies_the_subscriber(kanban_home, monkeypatch):
    """The transition must produce exactly one notification.

    Regression for the silent-stall gap: before ``protocol_violation`` joined
    the terminal kinds, this event produced zero notifications and the task
    stalled with nobody noticing.
    """
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kanban_home / "protocol-violation.db"))
    kb.init_db()

    with kbc.connect() as conn:
        tid = _subscribed_task(conn)
        kb._append_event(
            conn,
            tid,
            "protocol_violation",
            {"pid": 4242, "claimer": "host:1", "exit_code": 0,
             "exit_kind": "clean_exit", "protocol_violation": True},
        )

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1, (
        f"a protocol violation must produce exactly one notification; got {len(adapter.sent)}"
    )
    text = adapter.sent[0]["text"]
    assert tid in text
    # The message must NAME the cause, not fall through to the generic crash copy.
    assert "without reporting a result" in text, text
    assert "stopped unexpectedly" not in text, (
        "the protocol violation must not borrow the generic crash wording"
    )
    # The internals stay hidden — a pid is noise to a human reader.
    assert "4242" not in text
    assert "claimer" not in text
    assert "host:1" not in text


def test_protocol_violation_wakes_the_origin_session(kanban_home, monkeypatch):
    """The origin must be woken: the task is stuck with no verdict.

    A protocol violation leaves the task ``running`` with no verdict, so
    nobody moves it forward unless somebody is told. Waking is what turns a
    silent stall into a noticed one.
    """
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kanban_home / "protocol-violation.db"))
    kb.init_db()

    with kbc.connect() as conn:
        tid = _subscribed_task(conn)
        kb._append_event(
            conn,
            tid,
            "protocol_violation",
            {"pid": 4242, "claimer": "host:1", "exit_code": 0,
             "exit_kind": "clean_exit", "protocol_violation": True},
        )

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    wake = _wake_text(adapter)
    assert tid in wake
    # The wake text names the reason in plain words, not the event kind.
    assert "without reporting a result" in wake, wake


def test_protocol_violation_does_not_wake_a_notify_only_subscription(
    kanban_home, monkeypatch,
):
    """``delivery_mode`` still decides whether a wake-worthy kind wakes at all."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kanban_home / "protocol-violation.db"))
    kb.init_db()

    with kbc.connect() as conn:
        tid = _subscribed_task(conn, delivery_mode="notify")
        kb._append_event(
            conn,
            tid,
            "protocol_violation",
            {"pid": 4242, "claimer": "host:1", "exit_code": 0,
             "exit_kind": "clean_exit", "protocol_violation": True},
        )

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert adapter.handled == [], (
        "a notify-only subscription must not be woken"
    )


def test_protocol_violation_is_delivered_once_and_the_cursor_advances(
    kanban_home, monkeypatch,
):
    """The event is claimed exactly once — the cursor is the dedup mechanism."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kanban_home / "protocol-violation.db"))
    kb.init_db()

    with kbc.connect() as conn:
        tid = _subscribed_task(conn)
        kb._append_event(
            conn,
            tid,
            "protocol_violation",
            {"pid": 4242, "claimer": "host:1", "exit_code": 0,
             "exit_kind": "clean_exit", "protocol_violation": True},
        )

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.sent) == 1

    with kbc.connect() as conn:
        _, remaining = kbn.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["protocol_violation"],
        )
    assert remaining == [], "the claimed event must not be re-delivered"


# ---------------------------------------------------------------------------
# 3. The renderers: name the cause, keep the internals hidden
# ---------------------------------------------------------------------------


def _gateway_msg(kind: str, payload: dict) -> str:
    from gateway.kanban_watchers_notifier import _EVENT_FORMATTERS

    ev = SimpleNamespace(payload=payload)
    n = SimpleNamespace(
        task_id="t_abc",
        head="[board] Kanban t_abc",
        title="Ship it",
        board_tag="[board] ",
    )
    msg, _wake, _reason = _EVENT_FORMATTERS[kind](ev, n)
    return msg


def _tui_text(kind: str, payload: dict) -> str:
    from tui_gateway.session_notifications import _format_kanban_event_text

    ev = SimpleNamespace(kind=kind, payload=payload)
    task = SimpleNamespace(title="t", assignee="worker", result=None)
    return _format_kanban_event_text({"task_id": "t_abc"}, task, ev, "")


def test_gateway_names_the_protocol_violation_cause():
    """The gateway copy must name the real cause and hide the internals."""
    msg = _gateway_msg(
        "protocol_violation",
        {"pid": 4242, "claimer": "host:1", "exit_code": 0,
         "exit_kind": "clean_exit", "protocol_violation": True},
    )
    assert "without reporting a result" in msg
    assert "retried automatically" in msg
    assert "4242" not in msg
    assert "host:1" not in msg
    assert "claimer" not in msg


def test_tui_names_the_protocol_violation_cause():
    """The TUI copy must name the real cause, not "worker crashed (pid gone)"."""
    text = _tui_text(
        "protocol_violation",
        {"pid": 4242, "claimer": "host:1", "exit_code": 0,
         "exit_kind": "clean_exit", "protocol_violation": True},
    )
    assert "without reporting a result" in text
    assert "will retry" in text
    assert "4242" not in text
    assert "host:1" not in text


def test_the_generic_crash_message_is_unchanged():
    """The control: an ordinary crash keeps its existing wording.

    The protocol-violation branch must not have swallowed the generic crash
    copy — a plain crash still says the worker stopped unexpectedly.
    """
    gateway_msg = _gateway_msg("crashed", {"pid": 4242, "claimer": "host:1"})
    assert "stopped unexpectedly" in gateway_msg
    assert "retried automatically" in gateway_msg
    assert "without reporting a result" not in gateway_msg

    tui_text = _tui_text("crashed", {"pid": 4242, "claimer": "host:1"})
    assert "crashed" in tui_text
    assert "will retry" in tui_text


def test_a_crash_without_violation_keys_never_mentions_violations():
    """No cause keys -> no invented cause.

    A plain crash carries no ``protocol_violation`` marker, so neither
    renderer may use the word "violation" for it. The reverse of the
    ``max_runtime=0s`` bug: no cause key -> no cause named.
    """
    gateway_msg = _gateway_msg("crashed", {"pid": 4242, "claimer": "host:1"})
    assert "violation" not in gateway_msg.lower()

    tui_text = _tui_text("crashed", {"pid": 4242, "claimer": "host:1"})
    assert "violation" not in tui_text.lower()


def test_the_violation_streak_is_named_when_the_payload_carries_it():
    """A payload carrying the streak names it; one without it does not."""
    with_streak = _gateway_msg(
        "protocol_violation",
        {"protocol_violation": True, "protocol_violations": 3},
    )
    assert "3 protocol violations" in with_streak

    without_streak = _gateway_msg("protocol_violation", {"protocol_violation": True})
    assert "violation" not in without_streak.lower().replace("protocol violation", "")


def test_the_violation_streak_is_named_in_the_tui_copy():
    with_streak = _tui_text(
        "protocol_violation",
        {"protocol_violation": True, "protocol_violations": 3},
    )
    assert "3 protocol violations" in with_streak

    without_streak = _tui_text("protocol_violation", {"protocol_violation": True})
    assert "3 protocol violations" not in without_streak


def test_both_renderers_survive_a_malformed_payload():
    """A notification must never raise on a malformed payload."""
    for payload in (
        {"protocol_violations": "not-a-number"},
        {"protocol_violations": None},
        {"protocol_violations": 0},
        {"protocol_violations": -1},
        {},
    ):
        gateway_msg = _gateway_msg("protocol_violation", payload)
        assert "without reporting a result" in gateway_msg, payload
        tui_text = _tui_text("protocol_violation", payload)
        assert "without reporting a result" in tui_text, payload
