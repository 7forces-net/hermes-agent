"""The provider quota wall (``rate_limited``) must be REPORTED, and reported truthfully.

``rate_limited`` is a first-class event kind the board emits
(``hermes_cli/kanban_db_dispatch.py::_classify_dead_worker_exit``), and
NEITHER renderer had a formatter for it — the gateway's ``_EVENT_FORMATTERS``
and the TUI's ``_KANBAN_EVENT_FORMATTERS`` both lacked the kind entirely, and
the kind was in NO subscriber's kind list either (``TERMINAL_KINDS``,
``_KANBAN_NOTIFY_KINDS``). So the transition was not merely mis-worded, it was
INVISIBLE: the 1682 quota-wall events of the 17.8 h dead-quota window produced
zero notifications and zero wakes.

Measured on the live ``gamedev`` board (2026-11-02):

* 1682 ``rate_limited`` events vs 2 ``crashed`` events (838:1);
* 1682 events across 10 cards, 640 of them still unclaimed behind 4
  subscription cursors;
* the payload shape was exactly ``{pid, claimer, exit_code, retry_status}``
  — and **0 of 1682** carried ``worker_output``, because the producer
  skipped it for rate-limited runs (``if task_id and not dead.rate_limited``).
  So the renderer had to treat the provider hint as OPTIONAL, not assume it.

FOLLOW-THROUGH (t_fadaa4aa): the producer now carries the extracted remedy on
the payload as ``provider_hint``, so the hint is live rather than hypothetical.
The renderer reads that key first and keeps ``worker_output`` as the fallback
for payloads written before the change.

The rule these tests lock in, in every renderer:

* the cause is a QUOTA WALL / provider rate limit — never a crash, never a
  worker failure;
* the card was REQUEUED and WILL BE RETRIED, and it did NOT count against the
  failure budget (that is what actually happened);
* the actionable provider hint is surfaced WHEN PRESENT, clipped and
  ANSI-stripped, and absent when it is not;
* no pid, no claimer;
* the gateway and the TUI copy are byte-identical for the same payload;
* the TUI glyph is distinct — not the failure glyph.

And the storm control (the reason this can be shipped at all):

* a quota wall is ONE condition with ONE remedy, so it is reported ONCE PER
  EPISODE, not once per retry. Measured: 1682 events -> 14 episode starts.
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

# The shared wording helper the two renderers call. It does NOT exist on the
# pre-fix tree, so the import is guarded: the negative control must fail on
# the REAL assertions below, not on a collection error (a module that will not
# import proves nothing about the fix).
try:
    from hermes_cli.kanban_notice import rate_limited_cause, rate_limited_detail
except ImportError:  # pragma: no cover - the pre-fix tree
    rate_limited_cause = None
    rate_limited_detail = None


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
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "rate-limited.db"))
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


# The LIVE payload shape, measured on the board: exactly these four keys.
LIVE_PAYLOAD = {
    "pid": 16932,
    "claimer": "DESKTOP-7G7SGH3:18860",
    "exit_code": 75,
    "retry_status": "ready",
}

# The provider's own actionable hint, as the worker prints it.
HINT_RAW = (
    "\x1b[1;38;2;205;127;50m \x1b[0m    Add credits with Custom:Minimax Oauth Fallback."
    "                           \x1b[38;2;205;127;50m \x1b[0m \x1b[38;2;205;127;50m \x1b[0m"
    "    Or switch providers with \x1b[1m/model <model> --provider <provider>\x1b[0m."
)


# ---------------------------------------------------------------------------
# 1. The kind is CLAIMED and RENDERED (AC1)
# ---------------------------------------------------------------------------


def test_rate_limited_is_in_every_kind_list():
    """The event must be CLAIMED, or the renderer branch is dead code.

    This is the link that was broken: the kind was in no subscriber's kind
    list, so the transition that strands a card against a quota wall produced
    ZERO notification — the same silent-stall shape ``t_f4c4ba10`` fixed for
    ``protocol_violation``, one event kind over.
    """
    assert "rate_limited" in TERMINAL_KINDS
    assert "rate_limited" in _WAKE_KINDS

    from tui_gateway.session_notifications import _KANBAN_NOTIFY_KINDS

    assert "rate_limited" in _KANBAN_NOTIFY_KINDS


def test_a_rate_limited_event_renders_in_both_renderers():
    """AC1: both renderers produce a message, and both name the quota wall."""
    gateway_msg = _gateway_msg("rate_limited", dict(LIVE_PAYLOAD))
    tui_text = _tui_text("rate_limited", dict(LIVE_PAYLOAD))

    assert gateway_msg
    assert tui_text

    for message in (gateway_msg, tui_text):
        assert "quota" in message.lower(), message
        assert "rate limit" in message.lower(), message


def test_the_two_renderers_agree_byte_for_byte():
    """AC5: the gateway and the TUI copy are byte-identical for one payload.

    Two copies of the wording is how they drift (the TUI copy was once
    hardcoded to "gave up after repeated spawn failures" while the gateway
    read the payload). The shared helper is the fix; this is the lock.

    The comparison is on the COPY — the shared cause+detail clause — not on
    the whole line: each renderer owns its own prefix (the gateway adds the
    board tag and the task head, the TUI adds the session and the assignee)
    and its own suffix (the gateway appends the log pointer). Those are
    renderer chrome, not wording.
    """
    for payload in (
        dict(LIVE_PAYLOAD),
        {**LIVE_PAYLOAD, "worker_output": HINT_RAW},
        {},
    ):
        gateway_msg = _gateway_msg("rate_limited", payload)
        tui_text = _tui_text("rate_limited", payload)

        # The shared clause, straight from the helper both renderers call.
        shared = f"{rate_limited_cause(payload)}: {rate_limited_detail(payload)}"

        # It appears VERBATIM in both — same bytes, not "the same meaning".
        assert shared in gateway_msg, (payload, shared, gateway_msg)
        assert shared in tui_text, (payload, shared, tui_text)

        # And it is the WHOLE copy on each side: strip the gateway's prefix
        # and its own "Logs: ..." suffix, and the TUI's prefix, and what is
        # left must be the same string.
        gateway_copy = gateway_msg.split("— ", 1)[1]
        gateway_copy = gateway_copy.split(". Logs: `", 1)[0]
        tui_copy = tui_text.split("Kanban t_abc", 1)[1].strip()
        assert gateway_copy == tui_copy, (payload, gateway_copy, tui_copy)


# ---------------------------------------------------------------------------
# 2. It is NOT a crash and NOT a failure (the false-cause class)
# ---------------------------------------------------------------------------


def test_the_copy_never_says_crashed_or_failure():
    """The forbidden-vocabulary lock.

    A quota wall is not a crash and not a worker failure. The copy must not
    contain any of the crash vocabulary, and it must not name a pid or a
    claimer (a pid is noise to a human reader).
    """
    for message in (
        _gateway_msg("rate_limited", dict(LIVE_PAYLOAD)),
        _tui_text("rate_limited", dict(LIVE_PAYLOAD)),
        _gateway_msg("rate_limited", {**LIVE_PAYLOAD, "worker_output": HINT_RAW}),
        _tui_text("rate_limited", {**LIVE_PAYLOAD, "worker_output": HINT_RAW}),
    ):
        low = message.lower()
        for forbidden in (
            "crash",
            "crashed",
            "pid",
            "stopped unexpectedly",
            "pid gone",
            "exited with code",
            "signal",
        ):
            assert forbidden not in low, (forbidden, message)


def test_the_copy_states_the_requeue_and_the_failure_budget():
    """AC2: the message says what actually happened.

    The board requeues a rate-limited worker WITHOUT counting a failure, so
    the breaker does not trip and the card is retried. That is the part that
    distinguishes a quota wall from a failure — and the part that makes the
    board's behaviour legible.
    """
    for message in (
        _gateway_msg("rate_limited", dict(LIVE_PAYLOAD)),
        _tui_text("rate_limited", dict(LIVE_PAYLOAD)),
    ):
        low = message.lower()
        assert "requeued" in low, message
        assert "retried" in low or "retry" in low, message
        assert "does not count as a failure" in low, message


# ---------------------------------------------------------------------------
# 3. The actionable provider hint (AC3)
# ---------------------------------------------------------------------------


def test_the_provider_hint_is_surfaced_when_present():
    """AC3: the operator's call to action reaches the reader."""
    payload = {**LIVE_PAYLOAD, "worker_output": HINT_RAW}

    for message in (
        _gateway_msg("rate_limited", payload),
        _tui_text("rate_limited", payload),
    ):
        assert "Add credits" in message, message
        assert "switch providers" in message, message


def test_the_provider_hint_is_absent_when_the_payload_has_none():
    """AC3: absent when not present — and the LIVE payload has none.

    Measured: 0 of 1682 live ``rate_limited`` payloads carry
    ``worker_output`` (the producer skips it for rate-limited runs), so the
    no-hint case is the COMMON case, not an edge case.
    """
    for message in (
        _gateway_msg("rate_limited", dict(LIVE_PAYLOAD)),
        _tui_text("rate_limited", dict(LIVE_PAYLOAD)),
    ):
        assert "Add credits" not in message, message
        assert "switch providers" not in message, message


# ---------------------------------------------------------------------------
# 3b. The PRODUCER-carried hint (the live path)
#
# The producer now puts the extracted remedy on the payload as
# ``provider_hint`` (t_fadaa4aa). Before that, the payload carried NOTHING
# extractable — 0 of 1682 live ``rate_limited`` payloads had
# ``worker_output`` — so every renderer reported the quota wall WITHOUT the
# remedy, and the operator had to guess which provider was out of credits.
#
# The renderers must read the new key. The ``worker_output`` path stays as
# the fallback for payloads written before the change.
# ---------------------------------------------------------------------------

# The hint as the producer carries it: extracted, ANSI-free, already clipped.
CARRIED_HINT = (
    "Add credits with Custom:Minimax Oauth Fallback. "
    "Or switch providers with /model <model> --provider <provider>."
)


def test_the_producer_carried_hint_reaches_both_renderers():
    """The live path: the payload's ``provider_hint`` is rendered."""
    payload = {**LIVE_PAYLOAD, "provider_hint": CARRIED_HINT}

    for message in (
        _gateway_msg("rate_limited", payload),
        _tui_text("rate_limited", payload),
    ):
        assert "Add credits" in message, message
        assert "switch providers" in message, message
        assert "Custom:Minimax Oauth Fallback" in message, message


def test_the_two_renderers_agree_on_the_carried_hint():
    """The carried hint must not make the two renderers drift.

    The shared clause is the copy both renderers build from the SAME
    helper, so a payload that carries the hint produces byte-identical
    wording on both sides.
    """
    payload = {**LIVE_PAYLOAD, "provider_hint": CARRIED_HINT}

    shared = f"{rate_limited_cause(payload)}: {rate_limited_detail(payload)}"
    gateway_msg = _gateway_msg("rate_limited", payload)
    tui_text = _tui_text("rate_limited", payload)

    assert shared in gateway_msg, (shared, gateway_msg)
    assert shared in tui_text, (shared, tui_text)

    gateway_copy = gateway_msg.split("— ", 1)[1].split(". Logs: `", 1)[0]
    tui_copy = tui_text.split("Kanban t_abc", 1)[1].strip()
    assert gateway_copy == tui_copy, (gateway_copy, tui_copy)


def test_the_carried_hint_is_clipped_and_never_leaks_ansi():
    """A carried hint is bounded and ANSI-free, exactly like an extracted one."""
    payload = {
        **LIVE_PAYLOAD,
        "provider_hint": "\x1b[31mAdd credits with X.\x1b[0m " + "Z" * 5000,
    }

    for message in (
        _gateway_msg("rate_limited", payload),
        _tui_text("rate_limited", payload),
    ):
        assert "\x1b[" not in message, repr(message)
        assert "Z" * 5000 not in message
        assert len(message) < 800, len(message)


def test_the_carried_hint_never_names_a_pid_or_a_claimer():
    """A hint is operator copy; it must not leak the payload's machine keys."""
    payload = {**LIVE_PAYLOAD, "provider_hint": CARRIED_HINT}

    for message in (
        _gateway_msg("rate_limited", payload),
        _tui_text("rate_limited", payload),
    ):
        assert "16932" not in message, message
        assert "DESKTOP-7G7SGH3" not in message, message


def test_a_malformed_carried_hint_never_raises():
    """A malformed ``provider_hint`` must not break the notification."""
    for bad in (None, 12345, [], {"a": 1}, "   "):
        payload = {**LIVE_PAYLOAD, "provider_hint": bad}
        gateway_msg = _gateway_msg("rate_limited", payload)
        assert gateway_msg, bad
        tui_text = _tui_text("rate_limited", payload)
        assert tui_text, bad


def test_the_carried_hint_wins_over_a_stale_worker_output():
    """``provider_hint`` is the extracted remedy; it is the live path.

    When a payload carries both, the carried hint is authoritative — it is
    the producer's own extraction of the LATEST attempt, whereas
    ``worker_output`` may be from an earlier one.
    """
    payload = {
        **LIVE_PAYLOAD,
        "provider_hint": CARRIED_HINT,
        "worker_output": "Add credits with Some Old Provider.",
    }

    for message in (
        _gateway_msg("rate_limited", payload),
        _tui_text("rate_limited", payload),
    ):
        assert "Custom:Minimax Oauth Fallback" in message, message
        assert "Some Old Provider" not in message, message


def test_the_hint_is_ansi_stripped_and_clipped():
    """AC3: the hint never leaks raw ANSI and never becomes a dump."""
    payload = {
        **LIVE_PAYLOAD,
        "worker_output": HINT_RAW + ("\x1b[0m" + "A" * 5000 + ". Add credits with X."),
    }

    for message in (
        _gateway_msg("rate_limited", payload),
        _tui_text("rate_limited", payload),
    ):
        assert "\x1b[" not in message, repr(message)
        assert "A" * 5000 not in message
        assert len(message) < 800, len(message)


def test_the_hint_keeps_the_instruction_not_the_prose_in_front_of_it():
    """The clip must spend its budget on the INSTRUCTION.

    A worker log can glue the provider's hint to a long lead-in with no
    sentence break ("...1500 chars of prose Add credits with Foo."). Clipping
    the whole chunk would spend the whole budget on prose and cut the
    actionable words off — which would leave the operator with exactly the
    non-actionable message this card exists to replace.
    """
    payload = {**LIVE_PAYLOAD, "worker_output": "x" * 9000 + " Add credits with Foo."}

    for message in (
        _gateway_msg("rate_limited", payload),
        _tui_text("rate_limited", payload),
    ):
        # The trailing period is the RENDERER's punctuation — the shared
        # helper strips it so the two copies cannot double it — so the
        # assertion is on the instruction, not on the full stop.
        assert "Add credits with Foo" in message, message
        assert "x" * 9000 not in message
        assert len(message) < 800, len(message)


def test_a_malformed_payload_never_raises():
    """A notification must never raise on a malformed payload."""
    for payload in (
        {},
        {"worker_output": None},
        {"worker_output": 12345},
        {"worker_output": ""},
        {"exit_code": "not-a-number"},
        {"pid": None, "claimer": None},
    ):
        gateway_msg = _gateway_msg("rate_limited", payload)
        assert gateway_msg, payload
        tui_text = _tui_text("rate_limited", payload)
        assert tui_text, payload


# ---------------------------------------------------------------------------
# 4. The TUI glyph is distinct (AC6)
# ---------------------------------------------------------------------------


def test_the_tui_glyph_is_not_the_failure_glyph():
    """AC6: a quota wall is not a failure, so it must not wear the ✖.

    ``✖`` is the failure glyph (``crashed``/``gave_up``/
    ``protocol_violation``). Putting it next to a quota wall says the opposite
    of what the board actually did.
    """
    from tui_gateway.session_notifications import _KANBAN_EVENT_FORMATTERS

    glyph, _fmt = _KANBAN_EVENT_FORMATTERS["rate_limited"]
    assert glyph != "✖", glyph
    assert glyph == "⏳", glyph

    # And the failure kinds keep their own glyph.
    for kind in ("crashed", "gave_up", "protocol_violation"):
        assert _KANBAN_EVENT_FORMATTERS[kind][0] == "✖", kind


# ---------------------------------------------------------------------------
# 5. The wake decision (AC7) — stated, and asserted
# ---------------------------------------------------------------------------


def test_the_wake_decision_is_rate_limited_wakes_once_per_episode():
    """AC7: the decision, stated as a test.

    DECISION: ``rate_limited`` DOES wake the creator, once per EPISODE.

    Why wake: a quota wall needs a HUMAN. No agent can add credits or switch
    the provider, so the condition is operator-actionable by nature. Waking
    never was rejected because it leaves a condition only a human can fix
    completely invisible — which is precisely the failure this card closes.

    Why it is safe: the wake fires once per EPISODE, not once per event. The
    shared claim path suppresses the continuation events of a still-standing
    wall (measured: 1682 events -> 14 episode starts, a 120:1 reduction).
    For scale: the same window already produced 255 ``reclaimed`` wake turns
    on the same 10 cards, so 14 more is noise-level.
    """
    assert "rate_limited" in _WAKE_KINDS


def test_the_wake_i18n_key_exists_in_every_locale():
    """A kind in ``_WAKE_KINDS`` needs an i18n key.

    The wake text is built from ``gateway.kanban.wake.<kind>``; a missing key
    renders as the bare key string. ``tests/agent/test_i18n.py`` asserts
    key-set parity with English, so a missing key fails that suite — this
    test names the requirement locally.
    """
    from agent.i18n import SUPPORTED_LANGUAGES
    from agent.i18n import t as translate

    for lang in SUPPORTED_LANGUAGES:
        value = translate("gateway.kanban.wake.rate_limited", lang=lang)
        assert value != "gateway.kanban.wake.rate_limited", (
            f"{lang}.yaml is missing the rate_limited wake key"
        )
        assert value.strip(), lang


# ---------------------------------------------------------------------------
# 6. The episode filter — the storm control
# ---------------------------------------------------------------------------


def _make_run(conn, task_id, outcome):
    """A closed run row with the given outcome; returns its id."""
    cur = conn.execute(
        "INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, outcome, 1, 2, outcome),
    )
    conn.commit()
    return cur.lastrowid


def _book_rate_limited(conn, task_id):
    """Book a ``rate_limited`` event exactly the way the producer does.

    The producer's shape, measured on the live board (1682 of 1682 events):
    the event's OWN run has outcome ``rate_limited`` — the run that hit the
    wall IS the run the event belongs to. So the run ORDER is what carries
    the episode information, not the event's own run.

    The filter therefore looks at the PREVIOUS run on the card: a wall
    preceded by another wall (or by the reclaim the cooldown produces) is a
    CONTINUATION of the same episode; a wall preceded by anything else is a
    NEW episode.
    """
    run_id = _make_run(conn, task_id, "rate_limited")
    kb._append_event(conn, task_id, "rate_limited", dict(LIVE_PAYLOAD), run_id=run_id)
    return run_id


def test_only_the_first_event_of_a_quota_episode_is_reported(kanban_home, monkeypatch):
    """One wall, one notice — even though the wall books an event per retry.

    This is the storm control: during a quota window the dispatcher requeues
    the card, it re-spawns, it hits the SAME wall and books another event.
    Reporting every one of them would be 1682 notifications and 1682 wake
    turns for ONE condition with ONE remedy.
    """
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kanban_home / "rate-limited.db"))
    kb.init_db()

    with kbc.connect() as conn:
        tid = _subscribed_task(conn, delivery_mode="notify")
        # Three retries against the SAME standing wall. Each books its own
        # rate_limited run + event, exactly as the dispatcher does.
        _book_rate_limited(conn, tid)
        _book_rate_limited(conn, tid)
        _book_rate_limited(conn, tid)

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1, (
        f"one quota wall must produce exactly one notification; got {len(adapter.sent)}"
    )
    text = adapter.sent[0]["text"]
    assert "quota" in text.lower()
    assert tid in text


def test_a_quota_wall_after_a_real_outcome_is_reported_again(kanban_home, monkeypatch):
    """A RECOVERED wall (or a genuinely new one) is reported again.

    The filter suppresses the CONTINUATION of a standing wall. A wall that
    follows something else — a crash, a timeout, a completion — is a NEW
    episode and must reach the operator.
    """
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kanban_home / "rate-limited.db"))
    kb.init_db()

    with kbc.connect() as conn:
        tid = _subscribed_task(conn, delivery_mode="notify")
        # A real crash, THEN a wall: the wall follows something that is not
        # a quota event, so it is a NEW episode and must reach the operator.
        crashed_run = _make_run(conn, tid, "crashed")
        kb._append_event(conn, tid, "crashed", {"exit_kind": "nonzero_exit", "exit_code": 1}, run_id=crashed_run)
        _book_rate_limited(conn, tid)
        # ...and a second wall right after it: a CONTINUATION, suppressed.
        _book_rate_limited(conn, tid)

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # TWO notifications: the crash, and the wall that started after it. The
    # wall's CONTINUATION is suppressed.
    assert len(adapter.sent) == 2, (
        f"expected the crash + the new wall; got {len(adapter.sent)} notifications"
    )
    quota_msgs = [m["text"] for m in adapter.sent if "quota" in m["text"].lower()]
    assert len(quota_msgs) == 1, quota_msgs


def test_the_filter_does_not_touch_other_kinds(kanban_home, monkeypatch):
    """Only ``rate_limited`` is filtered — every other kind still notifies."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kanban_home / "rate-limited.db"))
    kb.init_db()

    with kbc.connect() as conn:
        tid = _subscribed_task(conn, delivery_mode="notify")
        run_a = _make_run(conn, tid, "crashed")
        kb._append_event(conn, tid, "crashed", {"exit_kind": "nonzero_exit", "exit_code": 1}, run_id=run_a)
        run_b = _make_run(conn, tid, "crashed")
        kb._append_event(conn, tid, "crashed", {"exit_kind": "nonzero_exit", "exit_code": 1}, run_id=run_b)
        run_c = _make_run(conn, tid, "crashed")
        kb._append_event(conn, tid, "crashed", {"exit_kind": "nonzero_exit", "exit_code": 1}, run_id=run_c)

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 3, (
        f"every crash must still notify; got {len(adapter.sent)}"
    )


def test_the_suppressed_event_is_still_claimed_and_never_re_delivered(kanban_home, monkeypatch):
    """A suppressed event is CLAIMED: the cursor advances past it.

    The filter decides what gets SENT, not what gets seen. If the cursor did
    not advance, the same wall would be re-examined on every tick forever.
    """
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kanban_home / "rate-limited.db"))
    kb.init_db()

    with kbc.connect() as conn:
        tid = _subscribed_task(conn, delivery_mode="notify")
        _book_rate_limited(conn, tid)
        _book_rate_limited(conn, tid)

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    with kbc.connect() as conn:
        _, remaining = kbn.unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            kinds=list(TERMINAL_KINDS),
        )
    assert remaining == [], "a claimed event must not be re-delivered"

    # And a second tick sends nothing more.
    adapter2 = RecordingAdapter()
    runner2 = _make_runner(adapter2)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner2))
    assert len(adapter2.sent) == 0, "the wall must not be reported twice"


# ---------------------------------------------------------------------------
# 7. A quota wall wakes the creator (the AC7 decision, end to end)
# ---------------------------------------------------------------------------


def test_a_rate_limited_event_wakes_the_origin(kanban_home, monkeypatch):
    """The quota wall reaches the creator's session — once."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kanban_home / "rate-limited.db"))
    kb.init_db()

    with kbc.connect() as conn:
        tid = _subscribed_task(conn, delivery_mode="notify+wake")
        run_id = _make_run(conn, tid, "rate_limited")
        kb._append_event(conn, tid, "rate_limited", dict(LIVE_PAYLOAD), run_id=run_id)

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.handled) == 1, (
        f"a quota wall must wake the origin; got {len(adapter.handled)} wake turns"
    )
    wake_text = getattr(adapter.handled[0], "text", "") or ""
    assert tid in wake_text
    assert "quota" in wake_text.lower(), wake_text


# ---------------------------------------------------------------------------
# 8. The LIVE TUI path (the split-module rebinding trap)
# ---------------------------------------------------------------------------


def test_the_live_tui_path_renders_the_quota_wall_without_a_name_error():
    """The TUI module is SPLIT: its bodies are rebound onto server.py's globals.

    ``method_ctx.bind_module`` rewrites every function onto ``server``'s
    globals, so an imported helper referenced by its BARE name resolves in
    ``server``'s namespace — where it does not exist. The failure is a
    ``NameError`` at CALL time, not import time, so imports and module-level
    checks both pass and the bug only shows up when the event is actually
    rendered.

    The house pattern is an ALIASED import referenced under the alias. This
    test exercises the REAL caller (``server._format_kanban_event_text``), not
    the submodule's own copy.
    """
    import tui_gateway.server as server

    task = SimpleNamespace(title="t", assignee="worker", result=None)
    ev = SimpleNamespace(kind="rate_limited", payload=dict(LIVE_PAYLOAD))
    text = server._format_kanban_event_text({"task_id": "t_abc"}, task, ev, "board")
    assert text, text
    assert "t_abc" in text
    assert "quota" in text.lower(), text
    assert "crash" not in text.lower(), text
