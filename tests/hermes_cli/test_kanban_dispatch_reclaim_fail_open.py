"""Regression tests: a failing reclaim phase must NOT stop the dispatcher.

The dispatch tick runs the reclaim phase FIRST and the spawn phase SECOND. A
reclaim-phase exception therefore aborted the tick BEFORE any worker was
spawned, and the only outward signal was one log line per board per minute —
so a board could silently stop dispatching forever.

That is exactly what a fenced gateway did: a leaked
``HERMES_DELEGATED_CHILD_CONTEXT`` made every ``write_txn`` on the board
raise ``PermissionError`` inside ``detect_crashed_workers``, and ready cards
sat in ``ready`` on gamedev, default AND techdad for as long as the gateway
kept that environment.

The reclaim phase now fails OPEN: the exception is logged and the tick
continues to the spawn phase. A broken reclaim means stale/crashed tasks are
not reclaimed THIS tick — degraded bookkeeping, not a dead dispatcher.
"""

from __future__ import annotations

from pathlib import Path

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


def _spawn_spy(calls: list):
    def spy_spawn(task, workspace_path, board=None):
        calls.append(getattr(task, "id", task))
        return 999999
    return spy_spawn


def test_reclaim_failure_still_spawns_a_ready_task(conn, monkeypatch, all_assignees_spawnable):
    """A reclaim phase that raises must not prevent the spawn phase.

    This is the regression for the fenced-gateway outage: the reclaim phase
    raised PermissionError, the tick died before spawning, and ready cards
    were never dispatched on ANY board.
    """
    kb.create_task(conn, title="spawn me", assignee="worker")

    def boom(*args, **kwargs):
        raise PermissionError("delegate_task child contexts cannot mutate Kanban tasks or boards")

    monkeypatch.setattr(kbd, "_run_reclaim_phase", boom)

    spawn_calls: list = []
    result = kbd.dispatch_once(conn, spawn_fn=_spawn_spy(spawn_calls))

    assert len(spawn_calls) == 1, "the spawn phase must still run after a reclaim failure"
    assert len(result.spawned) == 1


def test_reclaim_failure_does_not_raise_out_of_the_tick(conn, monkeypatch, all_assignees_spawnable):
    """The tick itself must not raise — it degrades, it does not die."""
    kb.create_task(conn, title="t", assignee="worker")

    def boom(*args, **kwargs):
        raise RuntimeError("reclaim exploded")

    monkeypatch.setattr(kbd, "_run_reclaim_phase", boom)

    # Must not raise.
    kbd.dispatch_once(conn, spawn_fn=_spawn_spy([]))


def test_reclaim_failure_is_logged(conn, monkeypatch, all_assignees_spawnable, caplog):
    """A silent failure is what made this outage invisible for an hour.

    The reclaim failure must be LOGGED so an operator can see the board is
    degraded even while dispatching continues.
    """
    kb.create_task(conn, title="t", assignee="worker")

    def boom(*args, **kwargs):
        raise RuntimeError("reclaim exploded")

    monkeypatch.setattr(kbd, "_run_reclaim_phase", boom)

    with caplog.at_level("ERROR"):
        kbd.dispatch_once(conn, spawn_fn=_spawn_spy([]))

    assert any("reclaim phase failed" in rec.getMessage() for rec in caplog.records)


def test_healthy_reclaim_still_works(conn, all_assignees_spawnable):
    """The happy path is unchanged: a healthy reclaim still spawns."""
    kb.create_task(conn, title="t", assignee="worker")
    spawn_calls: list = []
    result = kbd.dispatch_once(conn, spawn_fn=_spawn_spy(spawn_calls))
    assert len(spawn_calls) == 1
    assert len(result.spawned) == 1
