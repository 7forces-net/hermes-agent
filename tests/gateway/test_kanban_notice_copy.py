"""Kanban terminal-event pings must say what state the task is in and name the next command.

`gave_up` means the dispatcher auto-blocked the task (spawn failure, crash or timeout alike) and a
human must act; `crashed`/`timed_out` retry on their own. Contract tests over
`_EVENT_FORMATTERS`, never whole-string snapshots.
"""

from types import SimpleNamespace

from gateway.kanban_watchers_notifier import _EVENT_FORMATTERS


def _names(task_id="T-123"):
    return SimpleNamespace(task_id=task_id, head=f"[board] Kanban {task_id}", title="Ship it", board_tag="[board] ")


def _event(**payload):
    return SimpleNamespace(payload=payload)


def test_gave_up_says_blocked_and_names_unblock_log_reassign():
    msg, _wake, _reason = _EVENT_FORMATTERS["gave_up"](
        _event(failures=3, error="spawn: profile 'coder' not found"), _names())
    assert "blocked" in msg.lower()
    assert "3 times" in msg
    assert "spawn: profile 'coder' not found" in msg
    for cmd in ("hermes kanban unblock T-123", "hermes kanban log T-123", "hermes kanban reassign T-123"):
        assert f"`{cmd}`" in msg
    assert "spawn failures" not in msg  # wrong for crash/timeout-triggered trips


def test_gave_up_names_the_trigger_outcome_not_the_unified_counter():
    """The breaker trip names the cause that tripped it.

    ``failures`` is the UNIFIED ``consecutive_failures`` counter. A
    protocol-violation trip does not tick it, so quoting it as the cause
    would be a false cause (the same class as "max_runtime=0s"). The
    ``trigger_outcome`` in the payload IS the cause of this trip.
    """
    msg, _wake, _reason = _EVENT_FORMATTERS["gave_up"](
        _event(failures=1, trigger_outcome="crashed", error="pid 12152 not alive"), _names())
    assert "crash" in msg.lower()
    assert "1 times in a row" not in msg
    assert "times in a row" not in msg


def test_gave_up_names_the_protocol_violation_streak():
    """A violation trip names the violation streak, not the unified counter."""
    msg, _wake, _reason = _EVENT_FORMATTERS["gave_up"](
        _event(failures=1, trigger_outcome="crashed",
               protocol_violations=3, protocol_violation_limit=3), _names())
    assert "3 protocol violations" in msg
    assert "1 times in a row" not in msg


def test_crashed_and_timed_out_say_retry_and_hide_internals():
    crashed, *_ = _EVENT_FORMATTERS["crashed"](_event(), _names())
    timed_out, *_ = _EVENT_FORMATTERS["timed_out"](_event(limit_seconds=1800), _names())
    for msg in (crashed, timed_out):
        assert "retried automatically" in msg
        assert "pid" not in msg and "max_runtime" not in msg
    assert "30-minute" in timed_out


def test_protocol_violation_names_the_real_cause_and_hides_internals():
    """A protocol violation is not a random crash — say so, and hide the pid.

    The worker exited cleanly (rc=0) while the task was still ``running``: it
    never reported a result. "stopped unexpectedly" hides that the worker
    misbehaved, which is the whole reason the event exists.
    """
    msg, _wake, _reason = _EVENT_FORMATTERS["protocol_violation"](
        _event(pid=4242, claimer="host:1", exit_code=0,
               exit_kind="clean_exit", protocol_violation=True),
        _names(),
    )
    assert "without reporting a result" in msg
    assert "retried automatically" in msg
    # The internals stay hidden: a pid is noise to a human reader.
    assert "4242" not in msg
    assert "host:1" not in msg
    assert "claimer" not in msg
    assert "pid" not in msg
    # It must not borrow the generic crash wording.
    assert "stopped unexpectedly" not in msg
