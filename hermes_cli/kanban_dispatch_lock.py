"""The machine-global singleton dispatcher lock.

One dispatcher per machine.

The kanban board has TWO different locks and they are NOT interchangeable:

* ``<board>.db.dispatch.lock`` — **per-board**. Serialises the *write* tick
  so two dispatchers never race a write on WAL frames. Keyed on the resolved
  DB path, so unrelated boards tick in parallel.
  (``hermes_cli/kanban_db_connect.py::_dispatch_tick_lock``.)
* ``<kanban_home>/kanban/.dispatcher.lock`` — **machine-global**. Serialises
  *dispatching* across every board and every process on the host, so only one
  dispatcher is ever spawning workers at a time.

The board lock was never enough. Measured on the live ``gamedev`` board
(card ``t_616ce70a``): 869 distinct CLI dispatcher processes issued 2342 of
the board's 4996 ``claimed`` events (46.9 %) — every one a short-lived
``hermes kanban dispatch`` that spawns a worker and exits immediately. The
board lock allowed exactly that, because it only prevents two dispatchers
from writing at the SAME instant; it does not stop a CLI tick from spawning
workers while the gateway's tick is spawning workers.

The gateway has taken the machine-global lock since the single-dispatcher
backstop was introduced (``gateway/kanban_watchers.py``). The one-shot
``hermes kanban dispatch`` — the exact command the local watchdogs run — did
NOT, and silently opted out. This module is the shared primitive both sides
use, so they contend by construction rather than by coincidence.

**The lock is non-blocking and advisory.** A caller that cannot take it must
SKIP its tick, never wait: the gateway's async watcher must never stall, and a
watchdog must never hang. On a host with no gateway at all the lock is free,
so the CLI still works — the rule is "skip if another dispatcher holds it",
not "refuse to run".

**Byte-range locks are per-offset on Windows.** Two processes locking
DIFFERENT byte ranges of the same file do not conflict, so the CLI and the
gateway must use the SAME offset for the lock to mean anything. That is why
the primitive lives here rather than being written twice.

Re-entrancy
-----------
The gateway holds this lock for its whole process lifetime. The gateway also
serves ``/kanban dispatch`` as a slash command, which runs *inside* that same
process. A plain OS lock attempt from inside the gateway would contend with
the gateway's own lock and report a false "another dispatcher is active".

So this module keeps a process-local record of the lock this process holds.
A re-entrant acquire returns ``"already_held"`` instead of touching the OS
lock, and the caller must NOT release it. The record is only ever set by an
acquire that actually took the OS lock, so it can never claim a lock this
process does not hold.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import Optional

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import msvcrt
else:
    import fcntl

# Windows byte-range locks are taken at an offset well past any payload the
# file may hold, so a reader can read the file while another process holds the
# lock. The value MUST match the one the gateway uses: the dispatcher lock is
# meaningless if the two sides lock different byte ranges of the same file.
# ``gateway/kanban_watchers_common.py`` imports this constant rather than
# defining its own.
WINDOWS_LOCK_OFFSET = 1024 * 1024

# The lock THIS process holds, if any: ``(path, handle)``. Process-local on
# purpose — it records what this process owns, not what the host owns.
_PROCESS_HELD_LOCK: "Optional[tuple[Path, object]]" = None


def try_acquire_file_lock(handle) -> bool:
    """Take the exclusive, NON-BLOCKING advisory lock on ``handle``.

    Windows: a 1-byte ``msvcrt.locking`` range at :data:`WINDOWS_LOCK_OFFSET`.
    POSIX: an exclusive ``flock``. Returns False when the lock is held
    elsewhere, and never blocks.

    This mirrors ``gateway/status.py::_try_acquire_file_lock`` (which locks a
    DIFFERENT file — the gateway runtime lock). It is duplicated rather than
    imported so that importing this module never drags the gateway package
    into the CLI's import graph: ``gateway/__init__.py`` pulls in the whole
    gateway, which costs ~0.6 s of CLI startup.
    """
    try:
        if _IS_WINDOWS:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write("\n")
                handle.flush()
            handle.seek(WINDOWS_LOCK_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (BlockingIOError, OSError):
        return False


def release_file_lock(handle) -> None:
    """Release a lock taken by :func:`try_acquire_file_lock` (same byte range)."""
    with contextlib.suppress(OSError):
        if _IS_WINDOWS:
            handle.seek(WINDOWS_LOCK_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def dispatcher_lock_path() -> Path:
    """The machine-global singleton dispatcher lock file.

    Deliberately NOT under ``kanban/boards/<slug>/``: the lock is machine-
    global (one dispatcher for the whole host), not per-board. The path is
    resolved through :func:`hermes_cli.kanban_db.kanban_home` so the gateway
    and the CLI always agree on it — that function is shared across profiles
    BY DESIGN, so a profile-scoped ``HERMES_HOME`` does not fork the lock.
    """
    from hermes_cli import kanban_db as kb

    return kb.kanban_home() / "kanban" / ".dispatcher.lock"


def acquire_dispatcher_lock(
    lock_path=None, *, reentrant: bool = False
) -> "tuple[Optional[object], str]":
    """Take the exclusive, non-blocking machine-global dispatcher lock.

    Returns ``(handle, "held")`` (release via :func:`release_dispatcher_lock`),
    ``(None, "contended")`` when another process already holds it (the caller
    must NOT dispatch), or ``(None, "unavailable")`` when locking cannot be
    performed at all (read-only FS, permissions) — the caller then falls back
    to the per-board lock alone, which is the same fail-open choice the
    gateway makes.

    With ``reentrant=True``, a lock this process ALREADY holds is reported as
    ``(None, "already_held")`` instead of contending with itself. The caller
    may proceed normally and must NOT release. ``reentrant=False`` (the
    default) always attempts the OS lock, so two acquires in one process
    contend exactly as two processes would.
    """
    global _PROCESS_HELD_LOCK

    path = Path(lock_path) if lock_path is not None else dispatcher_lock_path()

    if reentrant:
        held = _PROCESS_HELD_LOCK
        if held is not None and _same_path(held[0], path):
            # This process owns the lock (e.g. the gateway, which holds it for
            # its whole lifetime, serving a `/kanban dispatch` slash command).
            return None, "already_held"

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(str(path), "a+", encoding="utf-8")
    except OSError:
        return None, "unavailable"
    if not try_acquire_file_lock(handle):
        handle.close()
        return None, "contended"

    _PROCESS_HELD_LOCK = (path, handle)
    return handle, "held"


def release_dispatcher_lock(handle) -> None:
    """Release a lock acquired via :func:`acquire_dispatcher_lock`.

    Passing ``None`` is a no-op — that is what a re-entrant ("already_held")
    acquire returns, and the process-level holder still owns the lock.

    Closing the handle is what actually matters: the OS drops the lock when
    the last handle to the file closes, so a process that dies mid-tick (or
    is killed) never leaves the lock stuck.
    """
    if handle is None:
        return

    global _PROCESS_HELD_LOCK
    held = _PROCESS_HELD_LOCK
    if held is not None and held[1] is handle:
        _PROCESS_HELD_LOCK = None

    release_file_lock(handle)
    with contextlib.suppress(Exception):
        handle.close()


def _same_path(a: Path, b: Path) -> bool:
    """Compare two lock paths tolerantly (unresolvable paths compare by text)."""
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a) == str(b)
