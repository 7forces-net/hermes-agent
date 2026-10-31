"""Regression tests: a ``timed_out`` event must name its REAL cause.

The board notified "⏱ ... timed out (max_runtime=0s); will retry" for EVERY
budget-exhausted worker. No task has a 0-second runtime — the real cause was
the iteration budget (256/256 API calls). The producer dropped
``event_payload_extra`` on the below-threshold ``timed_out`` branch, so the
event carried no cause at all and the notifiers fell back to
``limit_seconds`` -> absent -> ``or 0`` -> 0.

Three tests here lock the three links of that chain:

1. the PRODUCER carries the cause keys into the event payload;
2. the TUI notifier renders the iteration budget;
3. the gateway notifier renders the iteration budget.

Plus the guard that no rendered message ever claims a 0-second runtime, and
the control that the genuine SIGKILL wall-clock path still renders its real
runtime.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Fresh HERMES_HOME with an initialized default board."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def _budget_event_payload(conn, task_id: str) -> dict:
    """The payload of the ``timed_out`` event the budget path emitted."""
    events = kb.list_events(conn, task_id)
    timed_out = [e for e in events if e.kind == "timed_out"]
    assert timed_out, f"no timed_out event was emitted: {[e.kind for e in events]}"
    return timed_out[-1].payload or {}


# ---------------------------------------------------------------------------
# 1. The producer: the cause keys must survive into the event payload
# ---------------------------------------------------------------------------


def test_budget_exhaustion_event_carries_the_budget_cause(conn):
    """``event_payload_extra`` must reach the emitted event payload.

    The below-threshold branch used to append a bare
    ``{error, failures, retry_status}``, dropping the cause — so the
    notifiers had nothing to name and invented a 0-second runtime.
    """
    tid = kb.create_task(conn, title="budget burn", assignee="worker")
    kb.claim_task(conn, tid)

    kbd._record_task_failure(
        conn,
        tid,
        error=(
            "Iteration budget exhausted (256/256) — "
            "task could not complete within the allowed iterations"
        ),
        outcome="timed_out",
        release_claim=True,
        end_run=True,
        event_payload_extra={"budget_used": 256, "budget_max": 256},
    )

    payload = _budget_event_payload(conn, tid)
    assert payload.get("budget_max") == 256
    assert payload.get("budget_used") == 256
    # The pre-existing keys must survive the merge.
    assert payload.get("failures") == 1
    assert payload.get("retry_status") == "ready"
    assert "Iteration budget exhausted" in (payload.get("error") or "")


def test_budget_exhaustion_via_the_turn_finalizer_carries_the_cause(conn, monkeypatch):
    """The real producer (``turn_finalizer``) must produce a nameable event.

    This exercises ``_record_kanban_budget_exhausted`` — the exact function a
    worker calls when it runs out of API calls — rather than re-implementing
    its call.
    """
    from agent import turn_finalizer

    tid = kb.create_task(conn, title="budget burn", assignee="worker")
    kb.claim_task(conn, tid)

    monkeypatch.setattr(
        "hermes_cli.kanban_db_connect.connect", lambda *a, **k: _Borrowed(conn)
    )
    turn_finalizer._record_kanban_budget_exhausted(
        tid, 256, 256, SimpleNamespace(warning=lambda *a, **k: None)
    )

    payload = _budget_event_payload(conn, tid)
    assert payload.get("budget_max") == 256
    assert payload.get("budget_used") == 256


class _Borrowed:
    """A connection-like wrapper that leaves the real connection open."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


# ---------------------------------------------------------------------------
# 2. The TUI notifier
# ---------------------------------------------------------------------------


def _tui_text(payload: dict) -> str:
    from tui_gateway.session_notifications import _format_kanban_event_text

    ev = SimpleNamespace(kind="timed_out", payload=payload)
    task = SimpleNamespace(title="t", assignee="worker", result=None)
    return _format_kanban_event_text({"task_id": "t_abc"}, task, ev, "")


def test_tui_names_the_iteration_budget():
    """A budget-exhausted event must render the iteration budget."""
    text = _tui_text({"budget_used": 256, "budget_max": 256})
    assert "iteration budget 256/256" in text
    assert "max_runtime=0s" not in text


def test_tui_never_renders_a_zero_second_runtime():
    """A task with no wall-clock limit must not be announced as "0s".

    ``max_runtime_seconds IS NULL`` means there IS no runtime limit, so
    "max_runtime=0s" is a category error, not a rounded value.
    """
    for payload in (
        {},
        {"error": "something went wrong"},
        {"budget_used": 256, "budget_max": 256},
        {"limit_seconds": 0},
    ):
        text = _tui_text(payload)
        assert "max_runtime=0s" not in text, payload
        assert "timed out" in text, payload


def test_tui_keeps_the_real_runtime_for_the_wall_clock_timeout():
    """The genuine SIGKILL path (carrying ``limit_seconds``) is unchanged."""
    text = _tui_text({"limit_seconds": 1800, "elapsed_seconds": 1807, "sigkill": True})
    assert "max_runtime=1800s" in text
    assert "max_runtime=0s" not in text


def test_tui_names_the_budget_for_legacy_events_without_cause_keys():
    """Events already written to the board name the budget in the error text.

    Those events have no ``budget_max`` key, so they must still not render
    a bogus "max_runtime=0s".
    """
    text = _tui_text(
        {
            "error": (
                "Iteration budget exhausted (256/256) — "
                "task could not complete within the allowed iterations"
            ),
            "failures": 4,
            "retry_status": "ready",
        }
    )
    assert "iteration budget 256/256" in text
    assert "max_runtime=0s" not in text


def test_tui_survives_a_malformed_payload():
    """A malformed payload must not raise — the poller runs in the TUI loop."""
    for payload in (
        {"limit_seconds": "not-a-number"},
        {"budget_max": "not-a-number", "budget_used": "nope"},
        {"limit_seconds": None},
        {"budget_max": 0},
    ):
        text = _tui_text(payload)
        assert "timed out" in text, payload
        assert "max_runtime=0s" not in text, payload


# ---------------------------------------------------------------------------
# 3. The gateway notifier
# ---------------------------------------------------------------------------


def _gateway_msg(payload: dict) -> str:
    from gateway.kanban_watchers_notifier import _EVENT_FORMATTERS

    ev = SimpleNamespace(payload=payload)
    n = SimpleNamespace(
        task_id="t_abc",
        head="[board] Kanban t_abc",
        title="Ship it",
        board_tag="[board] ",
    )
    msg, _wake, _reason = _EVENT_FORMATTERS["timed_out"](ev, n)
    return msg


def test_gateway_names_the_iteration_budget():
    msg = _gateway_msg({"budget_used": 256, "budget_max": 256})
    assert "iteration budget" in msg
    assert "256/256" in msg
    assert "retried automatically" in msg


def test_gateway_never_claims_a_zero_second_runtime():
    msg = _gateway_msg({})
    assert "0-second" not in msg
    assert "0 minute" not in msg
    assert "retried automatically" in msg


def test_gateway_keeps_the_real_runtime_for_the_wall_clock_timeout():
    msg = _gateway_msg({"limit_seconds": 1800})
    assert "30-minute" in msg
    assert "retried automatically" in msg


def test_gateway_names_the_budget_for_legacy_events_without_cause_keys():
    msg = _gateway_msg(
        {
            "error": (
                "Iteration budget exhausted (256/256) — "
                "task could not complete within the allowed iterations"
            ),
        }
    )
    assert "iteration budget" in msg
    assert "256/256" in msg


def test_gateway_survives_a_malformed_payload():
    for payload in (
        {"limit_seconds": "not-a-number"},
        {"budget_max": "not-a-number"},
        {"budget_max": 0},
    ):
        msg = _gateway_msg(payload)
        assert "retried automatically" in msg, payload


# ---------------------------------------------------------------------------
# 4. End-to-end: producer -> event -> TUI rendering
# ---------------------------------------------------------------------------


def test_end_to_end_budget_exhaustion_renders_the_budget(conn):
    """The whole chain: the producer's event must render as the real cause.

    This is the regression that matters — the two halves (producer and
    notifier) were each individually plausible and only wrong together.
    """
    tid = kb.create_task(conn, title="budget burn", assignee="worker")
    kb.claim_task(conn, tid)

    kbd._record_task_failure(
        conn,
        tid,
        error=(
            "Iteration budget exhausted (256/256) — "
            "task could not complete within the allowed iterations"
        ),
        outcome="timed_out",
        release_claim=True,
        end_run=True,
        event_payload_extra={"budget_used": 256, "budget_max": 256},
    )

    payload = _budget_event_payload(conn, tid)
    text = _tui_text(payload)
    assert "iteration budget 256/256" in text
    assert "max_runtime=0s" not in text
