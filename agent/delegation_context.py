"""Context-local state for delegate_task child execution.

A Hermes process may itself be a Kanban dispatcher worker with HERMES_KANBAN_* in
os.environ. In-process delegate_task children and cron jobs fired via
``cronjob(action="run")`` are NOT dispatcher-owned, so identity gates must fail
closed for them without mutating the process-global environment.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator, Mapping, MutableMapping, overload

_DELEGATED_CHILD_CONTEXT: ContextVar[bool] = ContextVar("hermes_delegated_child_context", default=False)
# Any in-process execution that is NOT the dispatcher-owned worker (cron jobs). Kept separate
# so delegate_task-specific behaviour (subprocess env scrubbing, its error strings) is unchanged.
_NON_DISPATCHER_OWNED_CONTEXT: ContextVar[bool] = ContextVar("hermes_non_dispatcher_owned_context", default=False)

DELEGATED_CHILD_ENV_MARKER = "HERMES_DELEGATED_CHILD_CONTEXT"

KANBAN_ENV_KEYS: tuple[str, ...] = (
    "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_KANBAN_GOAL_MODE", "HERMES_KANBAN_GOAL_MAX_TURNS",
)

# Board-LOCATION pins, as opposed to worker IDENTITY. A worker is pinned to ONE
# board so it cannot see the others; a control plane (the gateway) dispatches
# EVERY board, so inheriting these makes it resolve every slug to the pinned DB.
KANBAN_BOARD_PIN_ENV_KEYS: tuple[str, ...] = (
    "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_WORKSPACE",
)


@contextmanager
def delegated_child_context(session_id: str | None = None) -> Iterator[None]:
    """Mark child execution and isolate its task-local session identity. Even a context
    entered without an id must restore the parent's session ContextVar (child
    construction calls ``set_current_session_id``)."""
    token = _DELEGATED_CHILD_CONTEXT.set(True)
    try:
        from gateway.session_context import scoped_current_session_id  # lazy: it calls is_delegated_child_context()

        with scoped_current_session_id(session_id):
            yield
    finally:
        _DELEGATED_CHILD_CONTEXT.reset(token)


def is_delegated_child_context() -> bool:
    """Return True while code is running for a delegate_task child."""
    return bool(_DELEGATED_CHILD_CONTEXT.get())


def enter_non_dispatcher_owned_context() -> Token[bool]:
    """Token form of :func:`non_dispatcher_owned_context` for long try/finally scopes."""
    return _NON_DISPATCHER_OWNED_CONTEXT.set(True)


def exit_non_dispatcher_owned_context(token: Token[bool]) -> None:
    """Restore the flag saved by :func:`enter_non_dispatcher_owned_context`."""
    _NON_DISPATCHER_OWNED_CONTEXT.reset(token)


@contextmanager
def non_dispatcher_owned_context() -> Iterator[None]:
    """Mark in-process execution that does NOT own the dispatcher's Kanban task; without it
    a cron agent run inside a worker is misread as that worker (kanban toolset force-added,
    ``kanban_complete`` defaulting to its task). ContextVar-scoped rather than clearing
    os.environ, which the worker's claim heartbeat and concurrent readers share."""
    token = enter_non_dispatcher_owned_context()
    try:
        yield
    finally:
        exit_non_dispatcher_owned_context(token)


def is_dispatcher_owned_worker_context() -> bool:
    """The single predicate every ``HERMES_KANBAN_*`` identity gate should use."""
    return not (is_delegated_child_process_context() or _NON_DISPATCHER_OWNED_CONTEXT.get())


def is_delegated_child_process_context() -> bool:
    """Return True in this process or a subprocess spawned by a child."""
    return bool(_DELEGATED_CHILD_CONTEXT.get()) or bool(os.environ.get(DELEGATED_CHILD_ENV_MARKER))


def _fenced_kanban_root() -> str:
    """The board root this process's Kanban lineage lives under (``kanban_home()``); ``"1"`` when it
    cannot be resolved, which readers treat as "fence every board" (the pre-path marker)."""
    try:
        from hermes_cli.kanban_db import kanban_home
        return str(kanban_home())
    except Exception:
        return "1"


def scrub_kanban_env(env: Mapping[str, str] | MutableMapping[str, str]) -> dict[str, str]:
    """Remove worker identity, retaining board/location and an inherited write fence.

    TASK absence alone would promote a descendant to an orchestrator. The marker
    survives later execs, including scripts that remove TASK themselves. This is
    cooperative runtime scoping, not confinement of code with direct SQLite access.

    The marker's value is the fenced board ROOT, so the fence applies to the lineage's
    board and not to every Kanban DB the descendant touches: a child running a repro
    against a temp ``HERMES_HOME`` got a silently read-only board there. An inherited
    path-valued marker is kept (a grandchild that moved HERMES_HOME must not re-fence
    onto its scratch root and unfence the real one).
    """
    cleaned = {k: v for k, v in env.items() if k not in KANBAN_ENV_KEYS}
    inherited = str(env.get(DELEGATED_CHILD_ENV_MARKER) or "")
    cleaned[DELEGATED_CHILD_ENV_MARKER] = inherited if inherited and inherited != "1" else _fenced_kanban_root()
    return cleaned


def strip_delegation_fence(
    env: Mapping[str, str] | MutableMapping[str, str],
) -> dict[str, str]:
    """Return a copy of *env* with the delegation fence REMOVED.

    The opposite of :func:`scrub_kanban_env`: that helper PRESERVES an inherited
    fence (it re-stamps the current root), which is right for a delegate_task
    descendant and wrong for a gateway. A gateway is the top-level control plane,
    not a delegate child — it OWNS the boards and must be able to mutate them.

    A gateway respawn inherits ``os.environ`` verbatim from whatever CLI asked
    for the restart. When that CLI was a ``delegate_task`` child (a kanban
    worker restarting the gateway), the fence marker rode along into the new
    gateway. The gateway then treated its own board DB as a descendant's
    read-only target: every ``write_txn`` was refused, and because the reclaim
    phase runs FIRST in the dispatch tick, the tick aborted before the spawn
    phase — so no ready card was ever dispatched, on any board.

    The marker is removed (not blanked): an empty value would still be a
    present-but-empty marker, and ``kanban_path_is_fenced`` treats a legacy
    ``"1"`` as "fence everything". Callers pass the marker through
    :func:`strip_delegation_fence` on every path that spawns a gateway.

    Only the fence marker is touched. ``HERMES_KANBAN_DB`` and friends are
    board PINNING, not identity: a gateway that inherited them would resolve
    every board slug to the pinned DB (see ``_board_path``), so those are
    stripped by the caller when the spawn targets the control plane.
    """
    cleaned = dict(env)
    cleaned.pop(DELEGATED_CHILD_ENV_MARKER, None)
    return cleaned


def control_plane_env(
    env: Mapping[str, str] | MutableMapping[str, str],
) -> dict[str, str]:
    """Return a copy of *env* suitable for spawning a CONTROL PLANE process.

    Strips two classes of inherited Kanban env:

    1. The delegation fence (:func:`strip_delegation_fence`). A control plane
       is not a delegate child; it owns the boards and must be able to mutate
       them.
    2. Worker board-LOCATION pins (``HERMES_KANBAN_DB`` and friends). These
       pin a worker to ONE board so it cannot see the others — but
       ``_board_path`` consults ``HERMES_KANBAN_DB`` BEFORE the ``board=``
       argument, so a control plane that inherited the pin resolves EVERY
       board slug to that one DB. The gateway dispatches all boards, so an
       inherited pin makes it dispatch the wrong board's cards (and, on a
       fenced board, no cards at all).

    ``HERMES_KANBAN_BOARD`` is cleared too: ``get_current_board`` reads it as
    the active-board override, and the gateway enumerates boards itself.
    """
    cleaned = strip_delegation_fence(env)
    for key in KANBAN_BOARD_PIN_ENV_KEYS:
        cleaned.pop(key, None)
    return cleaned


def kanban_path_is_fenced(path: "os.PathLike[str] | str") -> bool:
    """Whether Kanban mutations at *path* (a board DB or board-metadata root) are denied for this
    process: always for an in-process delegate child (the parent's own board); for a spawned
    descendant only when *path* is the dispatcher-pinned ``HERMES_KANBAN_DB`` or lies under the
    fenced root the marker carries. A legacy ``"1"`` marker fences everything."""
    if _DELEGATED_CHILD_CONTEXT.get():
        return True
    marker = os.environ.get(DELEGATED_CHILD_ENV_MARKER, "")
    if not marker:
        return False
    if marker == "1":
        return True
    from pathlib import Path
    target = Path(path).expanduser().resolve()
    pinned = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if pinned and target == Path(pinned).expanduser().resolve():
        return True
    try:
        target.relative_to(Path(marker).expanduser().resolve())
    except ValueError:
        return False
    return True


@overload
def delegated_child_subprocess_env(env: Mapping[str, str]) -> dict[str, str]: ...


@overload
def delegated_child_subprocess_env(env: None = None) -> dict[str, str] | None: ...


def delegated_child_subprocess_env(
    env: Mapping[str, str] | MutableMapping[str, str] | None = None,
) -> dict[str, str] | None:
    """Carry worker/delegate descendant denial across a real process spawn.

    Location and credentials are untouched; callers retain their existing secret policy.
    Dispatcher workers and supervised tool transports grant their own explicit scope.
    """
    if not (is_delegated_child_process_context() or os.environ.get("HERMES_KANBAN_TASK")
            or (env and (env.get("HERMES_KANBAN_TASK") or env.get(DELEGATED_CHILD_ENV_MARKER)))):
        return None if env is None else dict(env)
    return scrub_kanban_env(os.environ if env is None else env)
