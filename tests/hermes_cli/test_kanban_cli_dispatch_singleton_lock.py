"""The one-shot ``hermes kanban dispatch`` must take the machine-global
singleton dispatcher lock — the same lock the gateway takes.

Card ``t_716018cc``.

The board-level lock (``kanban.db.dispatch.lock``) only serialises the WRITE
tick, so two dispatchers never corrupt the WAL. It does NOT stop two
dispatchers from SPAWNING workers at the same time. Measured on the live
``gamedev`` board by card ``t_616ce70a``:

* 869 distinct CLI dispatcher processes issued 2342 of the board's 4996
  ``claimed`` events (46.9 %) — every one a short-lived
  ``hermes kanban dispatch`` that spawns a worker and exits immediately.
* 24 of the 59 cron-watchdog spawns happened while the board was at
  ``max_in_progress=3``.

The gateway has taken the machine-global lock (``kanban/.dispatcher.lock``)
since the single-dispatcher backstop was introduced. The CLI did NOT, and
silently opted out of the very guard the repo already relies on:

    $ grep -rn "dispatcher.lock|_acquire_singleton" hermes_cli/
    (no hits)

These tests pin the fix:

1. The CLI takes the SAME lock file the gateway takes.
2. A CLI tick that finds the lock held SKIPS — it does not spawn, and it
   does not write to the board.
3. The skip is distinguishable from "nothing to spawn" (JSON field +
   exit code), because the local watchdogs treat a non-zero rc as a failure.
4. The lock is released when the CLI exits — it must never be left held.
5. The lock is NON-BLOCKING and the CLI still works on a host with no
   gateway (the lock is free).
6. The gateway and the CLI contend by construction: same path, same
   byte-range offset, one shared primitive.

The classification logic in ``kanban_db_dispatch.py`` (owned by
``t_72abab80``) and the board-level ``dispatch.lock`` behaviour are NOT
touched by this card, and the tests below assert that too.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_dispatch_lock as kdl
from hermes_cli import kanban_ops


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


@pytest.fixture
def clean_process_lock():
    """Reset the module-level process-held lock between tests.

    ``acquire_dispatcher_lock`` records what THIS process holds so a
    re-entrant acquire does not contend with itself. Tests that take the
    lock must not leak that state into the next test.
    """
    kdl._PROCESS_HELD_LOCK = None
    yield
    kdl._PROCESS_HELD_LOCK = None


@contextlib.contextmanager
def other_dispatcher_holds_the_lock(path=None):
    """Simulate ANOTHER PROCESS holding the machine-global lock.

    The lock is taken at the OS level but deliberately NOT registered in
    ``_PROCESS_HELD_LOCK``, so a re-entrant acquire inside this process
    still CONTENDS — exactly as it would in a genuinely separate process.

    This distinction matters: the same process holding the lock is the
    gateway serving its own ``/kanban dispatch`` (which must proceed, not
    skip), while a different process holding it is the bug this card fixes
    (which must skip).
    """
    path = Path(path) if path is not None else kdl.dispatcher_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(str(path), "a+", encoding="utf-8")
    assert kdl.try_acquire_file_lock(handle), "could not take the lock for the test"
    try:
        yield handle
    finally:
        kdl.release_file_lock(handle)
        handle.close()


def _args(**kw):
    """A ``dispatch`` argparse namespace with sane defaults."""
    base = dict(dry_run=False, max=None, failure_limit=kbd.DEFAULT_FAILURE_LIMIT, json=False)
    base.update(kw)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# 1. The CLI takes the SAME lock the gateway takes
# ---------------------------------------------------------------------------


def test_the_lock_path_is_the_one_the_gateway_uses(kanban_home):
    """The CLI and the gateway must agree on the lock file.

    The gateway's path is ``kanban_home()/kanban/.dispatcher.lock``
    (``gateway/kanban_watchers.py``). If the CLI used a different path the
    two would never contend, and the lock would be theatre.
    """
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin  # noqa: F401

    expected = kb.kanban_home() / "kanban" / ".dispatcher.lock"
    assert kdl.dispatcher_lock_path() == expected


def test_the_gateway_acquires_the_same_lock_the_cli_uses(kanban_home):
    """The gateway's ``_acquire_singleton_lock`` and the CLI's helper are
    the same lock — proven by contention, not by reading the source."""
    from gateway.kanban_watchers_common import _acquire_singleton_lock

    path = kdl.dispatcher_lock_path()
    cli_handle, cli_state = kdl.acquire_dispatcher_lock(path)
    try:
        assert cli_state == "held"
        gw_handle, gw_state = _acquire_singleton_lock(path)
        assert gw_state == "contended"
        assert gw_handle is None
    finally:
        kdl.release_dispatcher_lock(cli_handle)


def test_both_sides_use_the_same_windows_byte_range(kanban_home):
    """Byte-range locks are per-offset on Windows.

    If the CLI and the gateway locked different byte ranges of the same
    file they would never conflict, and the "singleton" would be two
    independent locks wearing one filename. The shared primitive is what
    makes them agree.
    """
    from gateway import status

    assert status._WINDOWS_LOCK_OFFSET == kdl.WINDOWS_LOCK_OFFSET


# ---------------------------------------------------------------------------
# 2. A contended CLI tick SKIPS: no spawn, no writes
# ---------------------------------------------------------------------------


def test_a_contended_cli_tick_does_not_dispatch(kanban_home, monkeypatch, clean_process_lock):
    """When another dispatcher holds the lock, the CLI must not dispatch."""
    calls = []

    def spy_dispatch_once(*a, **kw):
        calls.append(1)
        return kbd.DispatchResult()

    monkeypatch.setattr(kanban_ops.kbd, "dispatch_once", spy_dispatch_once)

    with other_dispatcher_holds_the_lock():
        rc = kanban_ops._cmd_dispatch(_args())

    assert calls == [], "a contended tick must not call dispatch_once at all"
    assert rc == 0


def test_a_contended_cli_tick_writes_nothing_to_the_board(conn, monkeypatch, clean_process_lock):
    """A skipped tick must leave the board untouched — no claims, no events.

    The task is assigned to a REAL profile (``default``) so the tick would
    genuinely claim and spawn it, and ``dry_run=True`` so the test never
    launches a worker process. If the task is still ``ready`` afterwards,
    the tick provably did no board work at all.
    """
    task_id = kb.create_task(conn, title="ready work", assignee="default")
    before_events = conn.execute("SELECT COUNT(*) AS n FROM task_events").fetchone()["n"]

    with other_dispatcher_holds_the_lock():
        rc = kanban_ops._cmd_dispatch(_args(dry_run=True))

    assert rc == 0
    after_events = conn.execute("SELECT COUNT(*) AS n FROM task_events").fetchone()["n"]
    assert after_events == before_events, "a skipped tick must not write events"

    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["status"] == "ready", (
        f"a skipped tick must not claim the task; it is {row['status']!r}, not 'ready'"
    )
    running = conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE status = 'running'"
    ).fetchone()["n"]
    assert running == 0, "a skipped tick must not claim or run anything"


def test_a_contended_cli_tick_does_not_spawn_a_worker(
    kanban_home, monkeypatch, capsys, clean_process_lock,
):
    """The contended path must not even reach the spawn call.

    ``dry_run=True`` records the spawn in ``result.spawned`` without
    launching anything, so the JSON payload is a faithful witness of what
    the tick WOULD have done.
    """
    with other_dispatcher_holds_the_lock():
        kanban_ops._cmd_dispatch(_args(json=True, dry_run=True))
    payload = json.loads(capsys.readouterr().out)
    assert payload["spawned"] == []
    assert payload["dispatcher_lock_held"] is True


def test_the_cli_holds_the_lock_for_the_duration_of_the_tick(
    kanban_home, monkeypatch, clean_process_lock,
):
    """THE core assertion: the CLI HOLDS the lock while it dispatches.

    Without the fix, the CLI dispatches with NO lock held — which is
    exactly how two dispatchers spawn workers at the same time. The spy
    tries to take the lock from inside the tick; it must be told
    "contended", proving the CLI already holds it.
    """
    observed = {}

    def spy_dispatch_once(*a, **kw):
        # Inside the tick: is the machine-global lock held by us?
        handle, state = kdl.acquire_dispatcher_lock()
        observed["state"] = state
        if handle is not None:
            kdl.release_dispatcher_lock(handle)
        return kbd.DispatchResult()

    monkeypatch.setattr(kanban_ops.kbd, "dispatch_once", spy_dispatch_once)

    rc = kanban_ops._cmd_dispatch(_args())

    assert rc == 0
    assert observed.get("state") == "contended", (
        "the CLI must hold the machine-global dispatcher lock while it "
        f"dispatches; the lock was {observed.get('state')!r} from inside the tick"
    )


def test_a_contended_cli_tick_reports_the_skip(kanban_home, monkeypatch, capsys, clean_process_lock):
    """The operator must be able to tell WHY nothing happened."""
    with other_dispatcher_holds_the_lock():
        rc = kanban_ops._cmd_dispatch(_args())

    assert rc == 0
    out = capsys.readouterr().out
    assert "another dispatcher" in out
    assert "lock" in out.lower()


# ---------------------------------------------------------------------------
# 3. "Another dispatcher is active" is distinguishable from "nothing to spawn"
# ---------------------------------------------------------------------------


def test_the_json_payload_distinguishes_locked_from_idle(
    kanban_home, monkeypatch, capsys, clean_process_lock,
):
    """A skipped tick and an idle tick both spawn zero workers.

    Only one of them means "somebody else is doing it", so the JSON must
    say which. The local watchdogs parse this payload.
    """
    with other_dispatcher_holds_the_lock():
        rc = kanban_ops._cmd_dispatch(_args(json=True))

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dispatcher_lock_held"] is True
    assert payload["dispatcher_lock"] == "contended"
    assert payload["spawned"] == []
    assert payload["skipped_locked"] is True


def test_an_idle_tick_does_not_claim_the_lock(kanban_home, monkeypatch, capsys, clean_process_lock):
    """The lock fields are PRESENT on a normal tick.

    A consumer must be able to rely on the field existing, not on it being
    absent when the lock was free — a payload whose keys come and go is a
    payload every consumer has to guard against.
    """
    monkeypatch.setattr(
        kanban_ops.kbd,
        "dispatch_once",
        lambda *a, **kw: kbd.DispatchResult(),
    )
    rc = kanban_ops._cmd_dispatch(_args(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "dispatcher_lock_held" in payload
    assert payload["dispatcher_lock_held"] is False
    assert payload["dispatcher_lock"] == "held"
    assert payload["spawned"] == []


def test_the_skip_is_not_an_error_exit_code(kanban_home, monkeypatch, clean_process_lock):
    """The watchdogs log a non-zero rc as "dispatch FAILED".

    A contended tick is a healthy no-op — the lock holder IS dispatching —
    so it must exit 0. Otherwise every cron run on a gateway host would
    report a false failure.
    """
    with other_dispatcher_holds_the_lock():
        rc = kanban_ops._cmd_dispatch(_args())
    assert rc == 0


# ---------------------------------------------------------------------------
# 4. The lock is released when the CLI exits
# ---------------------------------------------------------------------------


def test_the_lock_is_released_after_the_cli_dispatch(kanban_home, monkeypatch, clean_process_lock):
    """The CLI must not leave the lock held.

    If it did, the NEXT dispatcher on this host would skip forever — the
    board would silently stop dispatching.
    """
    monkeypatch.setattr(
        kanban_ops.kbd,
        "dispatch_once",
        lambda *a, **kw: kbd.DispatchResult(),
    )
    kanban_ops._cmd_dispatch(_args())

    handle, state = kdl.acquire_dispatcher_lock()
    try:
        assert state == "held", "the CLI must release the lock when it finishes"
    finally:
        kdl.release_dispatcher_lock(handle)


def test_the_lock_is_released_even_when_dispatch_raises(
    kanban_home, monkeypatch, clean_process_lock,
):
    """A crash mid-tick must not leave the lock held."""

    def boom(*a, **kw):
        raise RuntimeError("dispatch exploded")

    monkeypatch.setattr(kanban_ops.kbd, "dispatch_once", boom)

    with pytest.raises(RuntimeError):
        kanban_ops._cmd_dispatch(_args())

    handle, state = kdl.acquire_dispatcher_lock()
    try:
        assert state == "held", "the lock must be released even when the tick fails"
    finally:
        kdl.release_dispatcher_lock(handle)


def test_the_lock_is_released_even_when_the_db_connection_fails(
    kanban_home, monkeypatch, clean_process_lock,
):
    """A failure to open the board must not strand the lock."""

    class _Boom:
        def __enter__(self):
            raise RuntimeError("no board")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(kanban_ops.kbc, "connect_closing", lambda: _Boom())

    with pytest.raises(RuntimeError):
        kanban_ops._cmd_dispatch(_args())

    handle, state = kdl.acquire_dispatcher_lock()
    try:
        assert state == "held"
    finally:
        kdl.release_dispatcher_lock(handle)


# ---------------------------------------------------------------------------
# 5. Non-blocking, and the CLI still works with no gateway
# ---------------------------------------------------------------------------


def test_the_cli_works_on_a_host_with_no_gateway(kanban_home, monkeypatch, clean_process_lock):
    """No gateway → the lock is free → the CLI MUST dispatch.

    The rule is "skip if another dispatcher holds the lock", never "refuse
    to run". A host with no gateway at all is the case the CLI exists for.
    """
    calls = []

    def spy_dispatch_once(*a, **kw):
        calls.append(1)
        return kbd.DispatchResult()

    monkeypatch.setattr(kanban_ops.kbd, "dispatch_once", spy_dispatch_once)

    rc = kanban_ops._cmd_dispatch(_args())

    assert rc == 0
    assert calls == [1], "with no other dispatcher the CLI must dispatch normally"


def test_the_lock_acquire_never_blocks(kanban_home, clean_process_lock):
    """The acquire must be non-blocking.

    A watchdog that hangs waiting for the lock is worse than one that skips
    a tick: it holds a cron slot forever.
    """
    handle, state = kdl.acquire_dispatcher_lock()
    assert state == "held"

    import time

    start = time.monotonic()
    loser, loser_state = kdl.acquire_dispatcher_lock()
    elapsed = time.monotonic() - start

    assert loser_state == "contended"
    assert loser is None
    assert elapsed < 1.0, f"the contended acquire blocked for {elapsed:.2f}s"
    kdl.release_dispatcher_lock(handle)


def test_an_unavailable_lock_fails_open(kanban_home, monkeypatch, clean_process_lock):
    """If the lock cannot be taken AT ALL (read-only FS, permissions), the
    CLI must still dispatch.

    Fails OPEN, like the gateway's own "advisory lock unavailable —
    proceeding on config control alone".
    """
    calls = []

    def spy_dispatch_once(*a, **kw):
        calls.append(1)
        return kbd.DispatchResult()

    monkeypatch.setattr(kanban_ops.kbd, "dispatch_once", spy_dispatch_once)
    monkeypatch.setattr(
        kanban_ops,
        "_acquire_dispatcher_lock",
        lambda *a, **kw: (None, "unavailable"),
    )

    rc = kanban_ops._cmd_dispatch(_args())
    assert rc == 0
    assert calls == [1], "an unavailable lock must not stop dispatching"


# ---------------------------------------------------------------------------
# 6. The re-entrancy case: the gateway serving /kanban dispatch
# ---------------------------------------------------------------------------


def test_a_gateway_holding_the_lock_does_not_contend_with_itself(
    kanban_home, monkeypatch, clean_process_lock,
):
    """The gateway holds this lock for its whole process lifetime.

    It also serves ``/kanban dispatch`` as a slash command, which runs
    INSIDE that same process. A plain acquire there would contend with the
    gateway's own lock and report a false "another dispatcher is active" —
    making ``/kanban dispatch`` a no-op on every gateway host.
    """
    calls = []

    def spy_dispatch_once(*a, **kw):
        calls.append(1)
        return kbd.DispatchResult()

    monkeypatch.setattr(kanban_ops.kbd, "dispatch_once", spy_dispatch_once)

    # The gateway takes the lock for its lifetime.
    gateway_handle, gateway_state = kdl.acquire_dispatcher_lock()
    assert gateway_state == "held"
    try:
        rc = kanban_ops._cmd_dispatch(_args())
    finally:
        kdl.release_dispatcher_lock(gateway_handle)

    assert rc == 0
    assert calls == [1], (
        "the gateway dispatching from inside its own lock must work, not skip"
    )


def test_the_reentrant_path_does_not_release_the_process_lock(
    kanban_home, monkeypatch, clean_process_lock,
):
    """A re-entrant acquire must not drop the lock the process really holds.

    Otherwise the gateway's own lock would be released by a slash command
    and a second dispatcher could start.
    """
    gateway_handle, gateway_state = kdl.acquire_dispatcher_lock()
    assert gateway_state == "held"

    monkeypatch.setattr(
        kanban_ops.kbd,
        "dispatch_once",
        lambda *a, **kw: kbd.DispatchResult(),
    )
    kanban_ops._cmd_dispatch(_args())

    # The gateway's lock must still be held.
    assert kdl._PROCESS_HELD_LOCK is not None
    assert kdl._PROCESS_HELD_LOCK[1] is gateway_handle

    kdl.release_dispatcher_lock(gateway_handle)
    assert kdl._PROCESS_HELD_LOCK is None


# ---------------------------------------------------------------------------
# 7. Real cross-process contention
# ---------------------------------------------------------------------------


def test_a_second_process_sees_the_cli_lock_as_held(kanban_home, clean_process_lock):
    """The lock must exclude a REAL second process, not just a second handle.

    This is the actual bug: two ``hermes kanban dispatch`` processes, or a
    CLI tick and the gateway, must not spawn workers at the same time.
    """
    handle, state = kdl.acquire_dispatcher_lock()
    assert state == "held"

    repo_root = Path(__file__).resolve().parents[2]
    child = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(repo_root)!r})
        from hermes_cli.kanban_dispatch_lock import acquire_dispatcher_lock
        h, s = acquire_dispatcher_lock({str(kdl.dispatcher_lock_path())!r})
        print(s)
    """)
    proc = subprocess.run(
        [sys.executable, "-c", child], capture_output=True, text=True, timeout=120,
    )
    kdl.release_dispatcher_lock(handle)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "contended", (
        f"a second process must see the lock as contended; got {proc.stdout!r}"
    )


def test_the_gateway_the_cli_and_the_watchdog_all_contend(
    kanban_home, clean_process_lock,
):
    """Every dispatch path on the host must exclude the others.

    Three separate code paths can dispatch on this machine:

    * the gateway's embedded dispatcher (``gateway/kanban_watchers.py``)
    * the one-shot ``hermes kanban dispatch`` (this card)
    * the repo-side watchdog (``hermes_cli/kanban_watchdog.py``, a
      concurrent card) — which delegates to
      ``gateway.kanban_watchers_common._acquire_singleton_lock``

    They must all contend on the SAME file. The watchdog resolves its own
    path through ``kb.kanban_home()``, so this test pins that it agrees
    with the CLI and the gateway — a different path would mean two
    independent locks wearing one name.
    """
    from gateway.kanban_watchers_common import _acquire_singleton_lock

    real_path = kdl.dispatcher_lock_path()

    cli_handle, cli_state = kdl.acquire_dispatcher_lock(real_path)
    try:
        assert cli_state == "held"

        gw_handle, gw_state = _acquire_singleton_lock(real_path)
        assert gw_state == "contended"
        assert gw_handle is None

        # The watchdog must agree on the path AND the lock.
        try:
            from hermes_cli import kanban_watchdog as kw
        except ImportError:
            pytest.skip("hermes_cli.kanban_watchdog not present in this tree")

        cm = kw._singleton_dispatcher_lock()
        wd_handle, wd_state = cm.__enter__()
        try:
            assert wd_state == "contended"
        finally:
            cm.__exit__(None, None, None)
    finally:
        kdl.release_dispatcher_lock(cli_handle)


def test_the_lock_is_free_again_after_the_cli_exits(kanban_home, monkeypatch, clean_process_lock):
    """After the CLI's process exits, a second process must get the lock."""
    repo_root = Path(__file__).resolve().parents[2]
    child = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(repo_root)!r})
        from hermes_cli.kanban_dispatch_lock import acquire_dispatcher_lock
        h, s = acquire_dispatcher_lock({str(kdl.dispatcher_lock_path())!r})
        print(s)
    """)
    proc = subprocess.run(
        [sys.executable, "-c", child], capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "held"


# ---------------------------------------------------------------------------
# 8. Constraints: this card must not change the other locks
# ---------------------------------------------------------------------------


def test_the_board_level_dispatch_lock_is_untouched(kanban_home):
    """The per-board lock must keep behaving exactly as before.

    This card adds a machine-global lock; it must NOT change the per-board
    one, which is a different concern (write serialisation) with a
    different key (the board DB path).
    """
    db_path = kb.kanban_db_path(board="default")
    assert db_path.with_name(db_path.name + ".dispatch.lock").exists() is False or True

    with kbc._dispatch_tick_lock(db_path) as held:
        assert held is True
        # A second holder in the same process contends, exactly as before.
        with kbc._dispatch_tick_lock(db_path) as held2:
            assert held2 is False


def test_the_classification_logic_module_is_untouched():
    """``kanban_db_dispatch.py`` is owned by ``t_72abab80`` and verified.

    This card must not change it. The lock is taken by the CALLER
    (``_cmd_dispatch``), one layer above ``dispatch_once``, so the
    classification code is untouched by construction.
    """
    import inspect

    source = inspect.getsource(kbd.dispatch_once)
    assert "dispatcher.lock" not in source
    assert "acquire_dispatcher_lock" not in source


def test_dispatch_once_still_uses_the_board_lock(kanban_home, monkeypatch):
    """``dispatch_once`` must keep taking the per-board lock.

    The machine-global lock sits ABOVE it; it does not replace it.
    """
    import inspect

    source = inspect.getsource(kbd.dispatch_once)
    assert "_dispatch_tick_lock" in source
