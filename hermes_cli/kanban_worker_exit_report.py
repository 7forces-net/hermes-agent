"""A Kanban worker records its OWN exit status on the board before it exits.

Why this exists
---------------

The dispatcher's exit registry (``kanban_db_dispatch._recent_worker_exits``) is
an in-memory dict, so only the process that reaped the child knows its exit
status. That is not enough on this board:

* The dispatcher that SPAWNS a worker is often a short-lived ``hermes kanban
  dispatch`` CLI tick (the ``gamedev-watchdog.py`` watchdog runs one every ~5
  minutes). It claims the task, spawns the worker and then EXITS -- long
  before the worker dies. So it can never reap it.
* The process that later books the crash is a different one again (the
  gateway's tick, or the watchdog's next tick).

The result on the live board: 0 of 3387 ``crashed`` events carried
``exit_kind``/``exit_code``, so every renderer (gateway, TUI, desktop)
correctly said "the cause is unknown" for EVERY crash.

The worker itself is the one process that always knows its exit code, and it
already has the board DB in its environment (``HERMES_KANBAN_DB``). So it
writes the status itself on the way out.

How the code is captured
------------------------

``atexit`` does not expose the process exit code, so the worker wraps
``sys.exit`` for the duration of its run: every ``sys.exit(code)`` records
``code`` first. An unhandled exception is reported as code 1 by the
interpreter, and the ``sys.excepthook`` path records that too. A worker that
returns normally records 0 -- which is exactly the ``clean_exit`` case the
dispatcher books as a protocol violation (the worker exited without reporting
a result).

What it does NOT cover
----------------------

A worker that is HARD-killed (OOM, ``taskkill /F``, a container teardown) never
reaches its exit handler, so it cannot self-report. Those crashes stay
``unknown`` -- which is honest: the board genuinely does not know. The
dispatcher's own reap-time persistence (:func:`_persist_worker_exit`) covers
the case where the SPAWNING process is still alive to reap the child.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from typing import Optional

# Exit-status vocabulary, mirroring ``kanban_db_dispatch._decode_wait_status``.
# The producer's vocabulary is the renderers' vocabulary; the desktop pins its
# TS token set against the Python source, so these names must not drift.
EXIT_KIND_CLEAN = "clean_exit"
EXIT_KIND_NONZERO = "nonzero_exit"
EXIT_KIND_RATE_LIMITED = "rate_limited"


def exit_kind_for_code(code: int) -> str:
    """The producer's kind for a plain process exit code."""
    if code == 0:
        return EXIT_KIND_CLEAN
    from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE

    if code == KANBAN_RATE_LIMIT_EXIT_CODE:
        return EXIT_KIND_RATE_LIMITED
    return EXIT_KIND_NONZERO


def record_worker_exit_status(code: int, *, task_id: Optional[str] = None) -> bool:
    """Persist this worker's exit code on its board run row.

    Returns True when the status was written. Never raises: a failure to
    self-report must not change the worker's exit code or block the exit.

    The write targets the run row the worker owns (``HERMES_KANBAN_RUN_ID``),
    falling back to the task's still-open run. Both the run id and the pid are
    written by the dispatcher at spawn time, so the row is findable from
    either.
    """
    try:
        task_id = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip()
        if not task_id:
            return False
        run_id = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
        kind = exit_kind_for_code(int(code))
        from hermes_cli import kanban_db_connect as _kbc

        with _kbc.connect_closing() as conn:
            _write_exit_status(conn, task_id, int(code), kind, run_id or None)
        return True
    except Exception:
        return False


def _write_exit_status(
    conn: sqlite3.Connection, task_id: str, code: int, kind: str, run_id: Optional[str],
) -> int:
    """Write the exit status onto the run row; returns the rows updated.

    Only an OPEN run (``ended_at IS NULL``) is written: a run the dispatcher
    already closed has its own bookkeeping, and a stale status must not
    overwrite a newer attempt's.
    """
    sql = (
        "UPDATE task_runs SET worker_exit_kind = ?, worker_exit_code = ? "
        "WHERE task_id = ? AND ended_at IS NULL AND worker_exit_kind IS NULL"
    )
    params: list = [kind, code, task_id]
    if run_id:
        sql += " AND id = ?"
        params.append(int(run_id))
    with conn:
        cur = conn.execute(sql, params)
    return cur.rowcount


def install_exit_report_hook() -> None:
    """Make this worker record its exit status on every exit path.

    No-op unless ``HERMES_KANBAN_TASK`` is set, so an interactive session
    never writes to a board.
    """
    if not (os.environ.get("HERMES_KANBAN_TASK") or "").strip():
        return
    try:
        _install_sys_exit_wrapper()
        _install_excepthook()
    except Exception:
        pass


def _install_sys_exit_wrapper() -> None:
    """Wrap ``sys.exit`` so the status is recorded before the process leaves.

    Idempotent: a second install does not double-wrap.
    """
    original = sys.exit
    if getattr(original, "_kanban_exit_report_wrapped", False):
        return

    def _exit(code=0):
        try:
            record_worker_exit_status(int(code) if isinstance(code, int) else 0)
        except Exception:
            pass
        return original(code)

    _exit._kanban_exit_report_wrapped = True  # type: ignore[attr-defined]
    _exit._kanban_original_exit = original  # type: ignore[attr-defined]
    sys.exit = _exit  # type: ignore[assignment]


def _install_excepthook() -> None:
    """Record code 1 for an unhandled exception, then chain the old hook."""
    previous = sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        try:
            record_worker_exit_status(1)
        except Exception:
            pass
        if previous is not None:
            previous(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook
