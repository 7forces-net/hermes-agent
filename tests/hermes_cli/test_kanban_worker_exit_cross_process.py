"""A dead worker's exit status must survive the process that spawned it.

The exit registry (``kanban_db_dispatch._recent_worker_exits``) is an in-memory
dict: only the process that reaped the child knows its exit status. On a board
with more than one dispatcher -- the gateway plus a ``hermes kanban dispatch``
CLI tick, which claims the task, spawns the worker and then EXITS -- the
process that books the crash is usually NOT the one that spawned the worker.

Before this was fixed, the registry lookup missed, ``_classify_worker_exit``
returned ``("unknown", None)``, and the crash event payload carried NO cause
key at all: 0 of 3387 live ``crashed`` events on the ``gamedev`` board had
``exit_kind``/``exit_code``, so every renderer on every surface (gateway, TUI,
desktop) correctly said "the cause is unknown" for every crash.

Two mechanisms make the cause survive the process boundary:

* the worker records its OWN exit status before it exits
  (``hermes_cli.kanban_worker_exit_report``) -- the only process that always
  knows the code, and the only one alive when the worker dies;
* the dispatcher persists the status at reap time
  (``_persist_worker_exit``) -- covers the case where the spawning process is
  still alive to reap the child.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


def _spawn_exit(code: int) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-c", f"raise SystemExit({code})"])  # noqa: S603
    proc.wait()
    return proc


@pytest.fixture()
def board(tmp_path):
    """A real board DB with one claimed task and an open run row."""
    conn = kbc.connect(db_path=tmp_path / "kanban.db")
    task_id = kb.create_task(conn, title="cross-process exit", assignee="gamecode")
    kb.claim_task(conn, task_id)
    run_id = kb._current_run_id(conn, task_id)
    assert run_id is not None
    yield conn, task_id, run_id
    conn.close()


def _attach_worker(conn, task_id: str, run_id: int, pid: int) -> None:
    conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (pid, task_id))
    conn.execute("UPDATE task_runs SET worker_pid = ? WHERE id = ?", (pid, run_id))
    conn.commit()


def _run_worker_subprocess(
    db_path: str, task_id: str, run_id: int, code: int, tmp_path: Path,
) -> subprocess.CompletedProcess:
    """A worker process that self-reports its exit code, then exits with it.

    The delegation marker is popped because the dispatcher pops it for its own
    workers (``_default_spawn``): a Kanban worker owns its task's board row.
    """
    worker_src = textwrap.dedent(
        f"""
        import os, sys
        from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER
        os.environ.pop(DELEGATED_CHILD_ENV_MARKER, None)
        os.environ["HERMES_KANBAN_TASK"] = "{task_id}"
        os.environ["HERMES_KANBAN_RUN_ID"] = "{run_id}"
        os.environ["HERMES_KANBAN_DB"] = r"{db_path}"
        sys.path.insert(0, r"{Path(kbd.__file__).parent.parent}")
        from hermes_cli.kanban_worker_exit_report import install_exit_report_hook
        install_exit_report_hook()
        sys.exit({code})
        """
    )
    script = tmp_path / "worker.py"
    script.write_text(worker_src, encoding="utf-8")
    env = dict(os.environ)
    env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    return subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, env=env, timeout=120,
    )


def _classify_in_another_process(db_path: str, pid: int, task_id: str, tmp_path: Path) -> str:
    """Classify a death from a DIFFERENT process with an EMPTY registry."""
    sweeper = textwrap.dedent(
        f"""
        import sqlite3, sys
        sys.path.insert(0, r"{Path(kbd.__file__).parent.parent}")
        from hermes_cli import kanban_db_dispatch as kbd
        con = sqlite3.connect(sys.argv[1])
        con.row_factory = sqlite3.Row
        assert kbd._recent_worker_exits == {{}}, "the sweeper must start with an empty registry"
        dead = kbd._classify_dead_worker_exit(
            int(sys.argv[2]), "DESKTOP-X:1", conn=con, task_id=sys.argv[3]
        )
        print(dead.kind, dead.code, dead.error_text, dead.event_payload)
        """
    )
    script = tmp_path / "sweeper.py"
    script.write_text(sweeper, encoding="utf-8")
    out = subprocess.run(
        [sys.executable, str(script), db_path, str(pid), task_id],
        capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_worker_self_reports_its_exit_code(board, tmp_path):
    """The worker writes its own exit code before it exits."""
    conn, task_id, run_id = board
    db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    conn.commit()

    result = _run_worker_subprocess(db_path, task_id, run_id, 3, tmp_path)
    assert result.returncode == 3, result.stderr

    row = conn.execute(
        "SELECT worker_exit_kind, worker_exit_code FROM task_runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert row["worker_exit_kind"] == "nonzero_exit"
    assert row["worker_exit_code"] == 3


def test_sweeper_in_another_process_reads_the_self_reported_cause(board, tmp_path):
    """The regression this card fixes: a DIFFERENT process books the crash.

    The sweeper's in-memory registry is empty (it never reaped the worker),
    so the cause must come from the worker's self-report -- otherwise the
    event payload carries no cause key and every renderer says "cause unknown".
    """
    conn, task_id, run_id = board
    db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    _attach_worker(conn, task_id, run_id, 4242)
    conn.commit()

    result = _run_worker_subprocess(db_path, task_id, run_id, 3, tmp_path)
    assert result.returncode == 3, result.stderr

    out = _classify_in_another_process(db_path, 4242, task_id, tmp_path)
    assert "nonzero_exit 3" in out
    # The event payload carries the cause -- the renderers depend on this.
    assert "'exit_kind': 'nonzero_exit'" in out
    assert "'exit_code': 3" in out
    # And it is NOT the anonymous "not alive" text.
    assert "not alive" not in out


def test_worker_self_reports_a_rate_limit_without_booking_a_crash(board, tmp_path):
    """The quota wall must stay a requeue, never a crash."""
    conn, task_id, run_id = board
    db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    _attach_worker(conn, task_id, run_id, 4242)
    conn.commit()

    result = _run_worker_subprocess(
        db_path, task_id, run_id, kb.KANBAN_RATE_LIMIT_EXIT_CODE, tmp_path,
    )
    assert result.returncode == kb.KANBAN_RATE_LIMIT_EXIT_CODE, result.stderr

    out = _classify_in_another_process(db_path, 4242, task_id, tmp_path)
    assert "rate_limited" in out
    assert f"{kb.KANBAN_RATE_LIMIT_EXIT_CODE}" in out
    assert "crashed" not in out


def test_worker_self_reports_a_clean_exit(board, tmp_path):
    """rc=0 while still ``running`` stays a protocol violation, not a crash."""
    conn, task_id, run_id = board
    db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    _attach_worker(conn, task_id, run_id, 4242)
    conn.commit()

    result = _run_worker_subprocess(db_path, task_id, run_id, 0, tmp_path)
    assert result.returncode == 0, result.stderr

    out = _classify_in_another_process(db_path, 4242, task_id, tmp_path)
    assert "clean_exit 0" in out
    assert "protocol_violation" in out or "clean_exit" in out


def test_reaper_persists_exit_status_on_the_run_row(board, monkeypatch):
    """The reaper writes the exit status where another process can read it."""
    conn, task_id, run_id = board
    monkeypatch.setattr(kbd, "_recent_worker_exits", {})
    monkeypatch.setattr(kbd, "_live_worker_procs", {})

    proc = _spawn_exit(3)
    _attach_worker(conn, task_id, run_id, proc.pid)
    kbd._live_worker_procs[proc.pid] = proc

    kbd.reap_worker_zombies(conn)

    row = conn.execute(
        "SELECT worker_exit_kind, worker_exit_code FROM task_runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert row["worker_exit_kind"] == "nonzero_exit"
    assert row["worker_exit_code"] == 3


def test_sweeper_without_a_persisted_status_still_says_unknown(board):
    """No status anywhere -> the cause is genuinely unknown, and the payload
    says so rather than inventing a code (the ``t_94a810ea`` rule)."""
    conn, task_id, run_id = board
    proc = _spawn_exit(3)
    _attach_worker(conn, task_id, run_id, proc.pid)
    conn.commit()

    dead = kbd._classify_dead_worker_exit(
        proc.pid, "DESKTOP-X:1", conn=conn, task_id=task_id,
    )

    assert (dead.kind, dead.code) == ("unknown", None)
    assert dead.event_payload == {"pid": proc.pid, "claimer": "DESKTOP-X:1"}
    assert "not alive" in dead.error_text


def test_persisted_rate_limit_is_never_booked_as_a_crash(board, monkeypatch):
    """A persisted ``rate_limited`` status routes to the SAME ``rate_limited``
    branch as the in-memory path, so it cannot regress into a crash."""
    conn, task_id, run_id = board
    monkeypatch.setattr(kbd, "_recent_worker_exits", {})
    monkeypatch.setattr(kbd, "_live_worker_procs", {})

    proc = _spawn_exit(kb.KANBAN_RATE_LIMIT_EXIT_CODE)
    _attach_worker(conn, task_id, run_id, proc.pid)
    kbd._live_worker_procs[proc.pid] = proc

    kbd.reap_worker_zombies(conn)
    conn.commit()

    # Simulate the sweeper being a different process: empty registry.
    monkeypatch.setattr(kbd, "_recent_worker_exits", {})
    dead = kbd._classify_dead_worker_exit(
        proc.pid, "DESKTOP-X:1", conn=conn, task_id=task_id,
    )

    assert dead.kind == "rate_limited"
    assert dead.code == kb.KANBAN_RATE_LIMIT_EXIT_CODE
    assert dead.rate_limited is True
    assert dead.event_kind == "rate_limited"
    assert dead.run_outcome == "rate_limited"
    assert "crashed" not in dead.event_kind


def test_persisted_clean_exit_is_a_protocol_violation(board, monkeypatch):
    """rc=0 while still ``running`` stays a protocol violation, not a crash."""
    conn, task_id, run_id = board
    monkeypatch.setattr(kbd, "_recent_worker_exits", {})
    monkeypatch.setattr(kbd, "_live_worker_procs", {})

    proc = _spawn_exit(0)
    _attach_worker(conn, task_id, run_id, proc.pid)
    kbd._live_worker_procs[proc.pid] = proc

    kbd.reap_worker_zombies(conn)
    conn.commit()

    monkeypatch.setattr(kbd, "_recent_worker_exits", {})
    dead = kbd._classify_dead_worker_exit(
        proc.pid, "DESKTOP-X:1", conn=conn, task_id=task_id,
    )

    assert dead.kind == "clean_exit"
    assert dead.protocol_violation is True
    assert dead.event_kind == "protocol_violation"
    assert dead.event_payload["exit_kind"] == "clean_exit"
    assert dead.event_payload["exit_code"] == 0


def test_in_memory_registry_still_wins_when_present(board, monkeypatch):
    """The fast path is unchanged: a registry hit classifies without a DB read.

    A live registry entry is authoritative -- the persisted row may be older
    (the same pid could have been reused), so the in-process answer wins.
    """
    conn, task_id, run_id = board
    proc = _spawn_exit(3)
    _attach_worker(conn, task_id, run_id, proc.pid)
    kbd._live_worker_procs[proc.pid] = proc
    kbd.reap_worker_zombies(conn)
    conn.commit()

    # A DIFFERENT status in the registry (e.g. the pid was reused) must win.
    monkeypatch.setattr(
        kbd, "_recent_worker_exits", {proc.pid: ((kb.KANBAN_RATE_LIMIT_EXIT_CODE & 0xFF) << 8, 0.0)},
    )
    dead = kbd._classify_dead_worker_exit(
        proc.pid, "DESKTOP-X:1", conn=conn, task_id=task_id,
    )
    assert dead.kind == "rate_limited"


def test_persist_worker_exit_is_best_effort(board):
    """A failure to persist must never break the reap."""
    conn, task_id, run_id = board
    proc = _spawn_exit(3)
    _attach_worker(conn, task_id, run_id, proc.pid)

    class _Broken:
        def __enter__(self):
            raise RuntimeError("no db")

        def __exit__(self, *a):
            return False

    original = kb.write_txn
    kb.write_txn = lambda *a, **k: _Broken()
    try:
        kbd._persist_worker_exit(conn, proc.pid, (3 & 0xFF) << 8)  # must not raise
    finally:
        kb.write_txn = original
