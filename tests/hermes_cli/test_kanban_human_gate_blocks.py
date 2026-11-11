"""Regression tests — a HUMAN GATE block is never released by an automated
sweeper, and a completed card always records what it did.

THE BUG (techdad board, 2026-11-09)
------------------------------------
Two cards were deliberately blocked with ``kind="needs_input"``, waiting for
the user's sign-off:

* ``t_1ae444b6`` — legal: "DEFERRED by explicit user decision ... waiting on
  user sign-off"
* ``t_ce2bf192`` — shop: "the shop/merch section comes AFTER the user is
  happy with the page + blog look ... awaiting sign-off"

The liveness watchdog cron (``kanban-liveness-all-boards``) rescued both at
+1247s and +1240s — just past its 1200s (20 min) grace — and the cards then
ran to completion WITHOUT the user's sign-off.  The watchdog looked at the
EVENT kind (``blocked``) and ignored the card-level block KIND, so a human
decision was indistinguishable from a worker that merely stalled.

The second bug: all six cards from that day completed with ``result=''``.
``complete_task`` writes the ``result`` COLUMN, but the tool schema makes
``result`` optional and ``summary`` preferred — so the column stayed NULL
even though a rich handoff existed on the run row.  A completed card that
recorded nothing.

These tests pin down:

* ``needs_input`` / ``capability`` blocks are HUMAN GATES: the shipped
  watchdog detects and reports them but never releases them.
* A mechanical block (no kind, or ``transient``) is still recoverable — the
  watchdog must not become a no-op.
* ``block_is_human_gate`` reads BOTH the ``block_kind`` column and the newest
  ``blocked`` event's payload kind.
* ``complete_task`` mirrors ``summary`` into the ``result`` column when no
  explicit result was given, and an explicit result always wins.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_watchdog as kw


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _block(conn, tid, *, kind, reason="waiting on a human"):
    """Block a claimed card with an explicit kind."""
    assert kb.claim_task(conn, tid)
    assert kb.block_task(
        conn, tid,
        reason=reason,
        kind=kind,
        expected_run_id=kb.get_task(conn, tid).current_run_id,
    )


# ---------------------------------------------------------------------------
# block_is_human_gate: the shared discriminator
# ---------------------------------------------------------------------------


def test_needs_input_block_is_a_human_gate(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="legal sign-off")
        _block(conn, tid, kind="needs_input")
        assert kb.block_is_human_gate(conn, tid) is True


def test_capability_block_is_a_human_gate(kanban_home: Path) -> None:
    """A paid filing / an account no agent can open is a human gate."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="DPMA filing")
        _block(conn, tid, kind="capability")
        assert kb.block_is_human_gate(conn, tid) is True


def test_transient_block_is_not_a_human_gate(kanban_home: Path) -> None:
    """A flaky network is mechanical — an automated rescue is safe."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="flaky api")
        _block(conn, tid, kind="transient")
        assert kb.block_is_human_gate(conn, tid) is False


def test_untyped_block_is_not_a_human_gate(kanban_home: Path) -> None:
    """A legacy ``kanban_block(reason=...)`` with no kind is a worker giving
    up, not a human gate. The watchdog must still be able to recover it."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="worker gave up")
        _block(conn, tid, kind=None)
        assert kb.block_is_human_gate(conn, tid) is False


def test_human_gate_detected_from_event_payload_when_column_is_cleared(
    kanban_home: Path,
) -> None:
    """The event payload is a second, independent signal.

    A card whose ``block_kind`` column was reset by a direct DB edit (or by
    an older client that only populated the event) must still be recognised
    as a human gate from the newest ``blocked`` event's payload.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="sign-off via event only")
        _block(conn, tid, kind="needs_input")
        # Simulate the column losing the signal while the event keeps it.
        conn.execute("UPDATE tasks SET block_kind = NULL WHERE id = ?", (tid,))
        conn.commit()
        assert kb.block_is_human_gate(conn, tid) is True


# ---------------------------------------------------------------------------
# The shipped watchdog must not release a human gate
# ---------------------------------------------------------------------------


def test_watchdog_never_releases_a_human_gate(kanban_home: Path) -> None:
    """The card shape from the bug: a card that was blocked for sign-off and
    whose last run crashed. The watchdog must REPORT it and leave it
    blocked — it must not unblock it."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="legal sign-off")
        _block(conn, tid, kind="needs_input", reason="waiting on user sign-off")
        # Make the last run look crashed, which is the precondition the
        # watchdog's recover path acts on.
        conn.execute(
            "UPDATE task_runs SET status = 'crashed', outcome = 'crashed' "
            "WHERE task_id = ?",
            (tid,),
        )
        conn.commit()

        report = kw.WatchdogReport(board="test")
        kw._do_recover(conn, report, kw.WatchdogSettings())

        assert kb.get_task(conn, tid).status == "blocked", (
            "a needs_input block must never be auto-released"
        )
        assert tid not in report.recovered
        stuck = [s for s in report.stuck if s["task_id"] == tid]
        assert stuck, "the human gate must be REPORTED, not silently ignored"
        assert stuck[0]["reason"] == "human_gate"


def test_watchdog_still_recovers_a_mechanical_crash(kanban_home: Path) -> None:
    """The guard must not turn the watchdog into a no-op.

    A card whose status was flipped to ``blocked`` by the circuit breaker
    (which emits ``gave_up``, NOT ``blocked``) carries no human-gate kind, so
    the watchdog still recovers it. That is the mechanical case the rescue
    path exists for.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="crashed implementation")
        assert kb.claim_task(conn, tid)
        # A breaker trip: the card is `blocked`, its last run `crashed`, and
        # there is NO explicit `blocked` event (the breaker emits `gave_up`),
        # so neither the human-gate guard nor the sticky-block guard fires.
        conn.execute(
            "UPDATE tasks SET status = 'blocked', block_kind = NULL WHERE id = ?",
            (tid,),
        )
        conn.execute(
            "UPDATE task_runs SET status = 'crashed', outcome = 'crashed' "
            "WHERE task_id = ?",
            (tid,),
        )
        conn.commit()

        report = kw.WatchdogReport(board="test")
        kw._do_recover(conn, report, kw.WatchdogSettings())

        assert tid in report.recovered, (
            f"a mechanical crash must still be recovered; report={report}"
        )
        assert kb.get_task(conn, tid).status != "blocked"


def test_watchdog_reports_an_untyped_sticky_block_without_releasing_it(
    kanban_home: Path,
) -> None:
    """An untyped ``kanban_block`` is a sticky block: the watchdog REPORTS it
    and leaves it blocked. It is not a human gate (no kind), but it is still
    an explicit park, so only an operator may release it."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="worker parked it")
        _block(conn, tid, kind=None, reason="I need a human")
        conn.execute(
            "UPDATE task_runs SET status = 'crashed', outcome = 'crashed' "
            "WHERE task_id = ?",
            (tid,),
        )
        conn.commit()

        report = kw.WatchdogReport(board="test")
        kw._do_recover(conn, report, kw.WatchdogSettings())

        assert kb.get_task(conn, tid).status == "blocked"
        assert tid not in report.recovered
        stuck = [s for s in report.stuck if s["task_id"] == tid]
        assert stuck and stuck[0]["reason"] == "sticky_block"


# ---------------------------------------------------------------------------
# The completion path must record what the card did
# ---------------------------------------------------------------------------


def test_summary_is_mirrored_into_result(kanban_home: Path) -> None:
    """A worker that passes only ``summary`` (the preferred field) must
    still leave a populated ``result`` column."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ship the thing")
        assert kb.claim_task(conn, tid)
        assert kb.complete_task(
            conn, tid,
            summary="Shipped 10 SKUs to /shop/ — all verified live.",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        task = kb.get_task(conn, tid)
        assert task.status == "done"
        assert task.result, "a completed card must record what it did"
        assert "Shipped 10 SKUs" in task.result


def test_explicit_result_wins_over_summary(kanban_home: Path) -> None:
    """An explicit ``result`` is never overwritten by the summary mirror."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="explicit result")
        assert kb.claim_task(conn, tid)
        assert kb.complete_task(
            conn, tid,
            result="EXPLICIT RESULT TEXT",
            summary="the summary",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        task = kb.get_task(conn, tid)
        assert task.result == "EXPLICIT RESULT TEXT"


def test_completed_event_records_the_result_length(kanban_home: Path) -> None:
    """The ``completed`` event's ``result_len`` must now be non-zero for a
    summary-only completion — that is the field the dashboard renders."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="event payload")
        assert kb.claim_task(conn, tid)
        assert kb.complete_task(
            conn, tid,
            summary="A summary that is definitely not empty.",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        events = [
            e for e in kb.list_events(conn, tid) if e.kind == "completed"
        ]
        assert events, "a completed event must be recorded"
        payload = events[-1].payload
        if isinstance(payload, str):
            payload = json.loads(payload)
        assert payload["result_len"] > 0
