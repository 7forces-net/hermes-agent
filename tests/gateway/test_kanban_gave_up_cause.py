"""The `gave_up` notification must name the REAL cause of the breaker trip.

`gave_up` means the dispatcher's circuit breaker tripped and the task is now
Blocked, waiting for a human. The message must say WHY.

The bug this card fixes: `_fmt_gave_up` read the payload key `failures` — the
UNIFIED `consecutive_failures` counter — and rendered "it failed N times in a
row". But a protocol-violation trip deliberately does NOT tick that counter
(`_account_crashes` skips `_record_task_failure` for below-budget violations,
"must not consume the unified budget"), so a task that violated the protocol
3 times in a row was announced as "it failed 1 times in a row" — a confident
statement about a number that does not describe the event, the same class of
bug as `t_94a810ea`'s "max_runtime=0s".

Measured on the live `gamedev` board: 1499 of 3203 `gave_up` events carry
`trigger_outcome == "crashed"` with `failures == 1` — 47% of every auto-block
announcement on the board names a count that does not describe its own cause.

The rule these tests lock in:
  * a cause-specific key in the payload -> name that cause (and only it);
  * no cause key -> name NO cause and NO count (never invent a confident one);
  * the TUI copy must read the payload like the gateway copy does — it used to
    hardcode "gave up after repeated spawn failures", which is wrong for every
    trigger except `spawn_failed`.

Both renderers are covered here, because a fix in only one of them is the same
silent half-fix that `t_f4c4ba10` had to clean up.
"""

from types import SimpleNamespace

from gateway.kanban_watchers_notifier import _EVENT_FORMATTERS


def _names(task_id="T-123"):
    return SimpleNamespace(task_id=task_id, head=f"[board] Kanban {task_id}", title="Ship it", board_tag="[board] ")


def _event(**payload):
    return SimpleNamespace(payload=payload)


def _gateway_msg(payload: dict) -> str:
    msg, _wake, _reason = _EVENT_FORMATTERS["gave_up"](_event(**payload), _names())
    return msg


def _tui_text(payload: dict) -> str:
    from tui_gateway.session_notifications import _format_kanban_event_text

    ev = SimpleNamespace(kind="gave_up", payload=payload)
    task = SimpleNamespace(title="t", assignee="worker", result=None)
    return _format_kanban_event_text({"task_id": "t_abc"}, task, ev, "")


# ---------------------------------------------------------------------------
# 1. The protocol-violation trip names the violation streak (both renderers)
# ---------------------------------------------------------------------------


def test_gateway_names_the_protocol_violation_streak():
    """The cause-specific key wins over the unified failure counter."""
    msg = _gateway_msg({
        "failures": 1,
        "effective_limit": 3,
        "trigger_outcome": "crashed",
        "protocol_violations": 3,
        "protocol_violation_limit": 3,
        "error": "Worker exited with code 0 without reporting a result",
    })
    assert "3 protocol violations" in msg
    # The unified counter describes a DIFFERENT budget — it must not be
    # presented as the cause of this trip.
    assert "1 times in a row" not in msg


def test_tui_names_the_protocol_violation_streak():
    text = _tui_text({
        "failures": 1,
        "effective_limit": 3,
        "trigger_outcome": "crashed",
        "protocol_violations": 3,
        "protocol_violation_limit": 3,
        "error": "Worker exited with code 0 without reporting a result",
    })
    assert "3 protocol violations" in text
    assert "1 times in a row" not in text


def test_the_violation_limit_is_named_when_present():
    """The bound is useful context: the reader can see how close it was."""
    msg = _gateway_msg({
        "failures": 1,
        "trigger_outcome": "crashed",
        "protocol_violations": 3,
        "protocol_violation_limit": 3,
    })
    assert "3 protocol violations" in msg
    assert "3" in msg


def test_the_violation_streak_is_named_without_the_limit():
    """The limit is optional — its absence must not suppress the streak."""
    msg = _gateway_msg({
        "failures": 1,
        "trigger_outcome": "crashed",
        "protocol_violations": 2,
    })
    assert "2 protocol violations" in msg


# ---------------------------------------------------------------------------
# 2. The false cause: a crash trip with failures=1 must NOT say "1 times"
# ---------------------------------------------------------------------------


def test_a_crash_trip_does_not_claim_the_unified_failure_count():
    """The live-board shape: crashed + failures=1.

    1499 of 3203 live `gave_up` events have exactly this shape. The unified
    counter did not tick (the violation budget is separate), so naming it
    would be a false cause.
    """
    msg = _gateway_msg({
        "failures": 1,
        "effective_limit": 1,
        "limit_source": "dispatcher",
        "trigger_outcome": "crashed",
        "error": "pid 12152 not alive",
    })
    assert "1 times" not in msg
    assert "times in a row" not in msg
    # The trigger outcome IS in the payload, so the cause can be named.
    assert "crash" in msg.lower()


def test_the_tui_crash_trip_does_not_claim_the_unified_failure_count():
    text = _tui_text({
        "failures": 1,
        "effective_limit": 1,
        "limit_source": "dispatcher",
        "trigger_outcome": "crashed",
        "error": "pid 12152 not alive",
    })
    assert "1 times" not in text
    assert "times in a row" not in text
    assert "crash" in text.lower()


def test_the_crash_trip_never_claims_spawn_failures():
    """`crashed` is not a spawn failure — the TUI copy used to say it was."""
    text = _tui_text({
        "failures": 2,
        "trigger_outcome": "crashed",
        "error": "pid 12152 not alive",
    })
    assert "spawn failure" not in text.lower()


# ---------------------------------------------------------------------------
# 3. The spawn-failure trip still says the spawn-failure thing (that is real)
# ---------------------------------------------------------------------------


def test_a_spawn_failure_trip_still_names_the_spawn_failures():
    """Control: the cause that IS real must keep being named."""
    msg = _gateway_msg({
        "failures": 2,
        "effective_limit": 2,
        "limit_source": "dispatcher",
        "trigger_outcome": "spawn_failed",
        "error": "spawn: profile 'coder' not found",
    })
    assert "spawn" in msg.lower()
    assert "spawn: profile 'coder' not found" in msg


def test_the_tui_spawn_failure_trip_still_names_the_spawn_failures():
    """Control: the TUI copy keeps naming the spawn-failure cause.

    The old TUI string was hardcoded to "gave up after repeated spawn
    failures", which was RIGHT for this trigger and wrong for every other
    one. The cause is still named — it is now read from the payload.
    """
    text = _tui_text({
        "failures": 2,
        "effective_limit": 2,
        "limit_source": "dispatcher",
        "trigger_outcome": "spawn_failed",
        "error": "spawn: profile 'coder' not found",
    })
    assert "spawn" in text.lower()
    assert "spawn: profile 'coder' not found" in text
    # The unified counter is not quoted as the cause of a known-cause trip.
    assert "2 times in a row" not in text


# ---------------------------------------------------------------------------
# 4. The TUI copy stops claiming "repeated spawn failures" for other triggers
# ---------------------------------------------------------------------------


def test_the_tui_gave_up_copy_stops_hardcoding_spawn_failures():
    """The hardcoded TUI string is wrong for EVERY trigger but `spawn_failed`."""
    for outcome in ("crashed", "timed_out"):
        text = _tui_text({
            "failures": 2,
            "trigger_outcome": outcome,
            "error": "pid 12152 not alive",
        })
        assert "spawn" not in text.lower(), outcome


def test_the_tui_gave_up_copy_reads_the_payload_for_each_trigger():
    """A timed-out trip must say it timed out; a crashed trip that it crashed."""
    timed = _tui_text({"failures": 2, "trigger_outcome": "timed_out", "error": "elapsed 626s > limit 600s"})
    assert "timed out" in timed.lower() or "timeout" in timed.lower()

    crashed = _tui_text({"failures": 2, "trigger_outcome": "crashed", "error": "pid 1 not alive"})
    assert "crash" in crashed.lower()


def test_an_iteration_budget_exhaustion_is_not_called_a_timeout():
    """A budget exhaustion is not a wall-clock timeout — name the budget.

    Same class as the ``t_94a810ea`` fix, one branch over: the ``gave_up``
    copy must not call a budget exhaustion "a timeout".
    """
    msg = _gateway_msg({
        "failures": 2,
        "trigger_outcome": "timed_out",
        "budget_used": 60,
        "budget_max": 60,
        "error": "Iteration budget exhausted (60/60)",
    })
    assert "iteration budget" in msg
    assert "60/60" in msg


def test_the_tui_names_the_iteration_budget_too():
    text = _tui_text({
        "failures": 2,
        "trigger_outcome": "timed_out",
        "budget_used": 256,
        "budget_max": 256,
        "error": "Iteration budget exhausted (256/256)",
    })
    assert "iteration budget" in text
    assert "256/256" in text


# ---------------------------------------------------------------------------
# 6. The diagnostics surface ("Agent crash x1: ...") has the same grammar bug
# ---------------------------------------------------------------------------


def test_the_diagnostics_copy_is_grammatical():
    """``hermes kanban doctor`` said "failed 1 times in a row" too.

    Same defect class, same fix: the count is rendered with the right
    singular/plural form.
    """
    from hermes_cli import kanban_diagnostics as kd

    task = {
        "id": "t_demo00", "title": "demo", "assignee": "demo", "status": "ready",
        "consecutive_failures": 1, "last_failure_error": "pid 1 not alive",
    }
    runs = [{"run_id": 1, "outcome": "crashed", "error": "pid 1 not alive", "ended_at": 1}]
    diags = kd.compute_task_diagnostics(task, [], runs, config={"failure_threshold": 1})
    assert diags
    for d in diags:
        assert "1 times" not in d.detail, d.detail
        assert "1 time" in d.detail, d.detail


def test_the_diagnostics_copy_keeps_the_plural_form():
    from hermes_cli import kanban_diagnostics as kd

    task = {
        "id": "t_demo00", "title": "demo", "assignee": "demo", "status": "ready",
        "consecutive_failures": 3, "last_failure_error": "pid 1 not alive",
    }
    runs = [{"run_id": 1, "outcome": "crashed", "error": "pid 1 not alive", "ended_at": 1}]
    diags = kd.compute_task_diagnostics(task, [], runs, config={"failure_threshold": 1})
    assert diags
    for d in diags:
        assert "3 times" in d.detail, d.detail


# ---------------------------------------------------------------------------
# 7. The grammar: "1 times" is broken
# ---------------------------------------------------------------------------


def test_a_legacy_payload_without_a_trigger_still_reports_the_counter():
    """A pre-v0.13 payload has neither trigger nor violation key.

    Version archaeology proves this case is SAFE to quote the counter for:
    ``trigger_outcome`` entered the ``gave_up`` payload in v0.13.0 (2026-05-07)
    and the protocol-violation trip (the only ``force_trip`` caller, and the
    only producer of ``protocol_violations``) only exists since v0.19.0
    (2026-07-20). A payload with NEITHER key therefore cannot be a violation
    trip, so the unified counter IS what tripped the breaker.

    This is the one case where the counter is the honest answer, and the
    grammar is fixed while we are here ("1 time", not "1 times").
    """
    msg = _gateway_msg({"failures": 3, "error": "spawn: profile 'coder' not found"})
    assert "3 times in a row" in msg
    assert "spawn: profile 'coder' not found" in msg


def test_the_legacy_single_failure_is_grammatical():
    """One failure is "1 time", not "1 times"."""
    msg = _gateway_msg({"failures": 1, "error": "boom"})
    assert "1 time in a row" in msg
    assert "1 times" not in msg


def test_the_tui_legacy_payload_still_reports_the_counter():
    text = _tui_text({"failures": 3, "error": "spawn: profile 'coder' not found"})
    assert "3 times in a row" in text


def test_an_empty_payload_names_no_cause():
    """No cause key -> no cause named. Not even a bare number."""
    msg = _gateway_msg({})
    assert "times" not in msg.lower()

    text = _tui_text({})
    assert "times" not in text.lower()


def test_a_malformed_payload_never_invents_a_count():
    """A malformed payload must not make either renderer invent a count.

    Every key here is either absent, non-numeric, zero or negative — so the
    renderer has nothing to report and must not fall back to a plausible
    number. Note that a legacy payload with a VALID ``failures`` count is a
    different case (see the legacy tests above): there the counter is real.
    """
    for payload in (
        {"failures": "many"},
        {"failures": None},
        {"failures": -1},
        {"failures": 0},
        {"protocol_violations": "not-a-number"},
        {"protocol_violations": None},
        {"protocol_violations": 0},
        {"protocol_violations": -1},
        {"protocol_violations": 3, "protocol_violation_limit": "nope"},
    ):
        msg = _gateway_msg(payload)
        assert "times in a row" not in msg, payload
        assert "1 times" not in msg, payload
        text = _tui_text(payload)
        assert "times in a row" not in text, payload
        assert "1 times" not in text, payload


def test_a_malformed_payload_never_raises():
    """A notification must never raise on a malformed payload."""
    for payload in (
        {},
        {"failures": "many"},
        {"protocol_violations": {}},
        {"trigger_outcome": 42},
        {"error": None},
    ):
        _gateway_msg(payload)
        _tui_text(payload)


def test_a_malformed_trigger_outcome_names_no_spawn_failure():
    """A junk trigger outcome must not be read as a spawn failure."""
    msg = _gateway_msg({"failures": 2, "trigger_outcome": 42})
    assert "spawn" not in msg.lower()
    text = _tui_text({"failures": 2, "trigger_outcome": 42})
    assert "spawn" not in text.lower()


def test_an_unknown_trigger_outcome_is_named_verbatim():
    """An unrecognised trigger is named as-is, not guessed at."""
    msg = _gateway_msg({"failures": 2, "trigger_outcome": "quota_wall"})
    assert "quota_wall" in msg
    # ...and the unified counter is still not quoted as the cause.
    assert "2 times in a row" not in msg


# ---------------------------------------------------------------------------
# 6. The grammar: "1 times" is broken
# ---------------------------------------------------------------------------


def test_the_count_is_grammatically_correct():
    """One failure is "1 time", not "1 times"."""
    # A real cause-specific count (the violation streak) must be grammatical.
    msg = _gateway_msg({"trigger_outcome": "crashed", "protocol_violations": 1})
    assert "1 protocol violation" in msg
    assert "1 protocol violations" not in msg


# ---------------------------------------------------------------------------
# 7. The house contract is preserved: the next command is still named
# ---------------------------------------------------------------------------


def test_the_gave_up_message_still_names_the_next_commands():
    """Control: the fix must not cost the reader the recovery path."""
    msg = _gateway_msg({"failures": 3, "trigger_outcome": "spawn_failed", "error": "spawn: profile 'coder' not found"})
    assert "blocked" in msg.lower()
    for cmd in ("hermes kanban unblock T-123", "hermes kanban log T-123", "hermes kanban reassign T-123"):
        assert f"`{cmd}`" in msg


def test_the_gave_up_message_still_carries_the_last_error():
    """Control: the clipped error text is still shown."""
    msg = _gateway_msg({"failures": 1, "trigger_outcome": "crashed", "error": "pid 12152 not alive"})
    assert "pid 12152 not alive" in msg
