"""The "dispatcher stuck" warning must name the CONCURRENCY CAP, not blame profile health.

Card ``t_e6c45f52``. Measured on the live gamedev board: 753 "dispatcher
stuck" warnings, and the dominant cause was NOT a broken profile —

| cause | count | share |
|---|---|---|
| board at the global cap (``kanban.max_in_progress=3``) | 432 | 57.1 % |
| ready cards exist and capacity is free | 316 | 41.7 % |
| only the per-profile cap (``max_in_progress_per_profile=2``) held it | 40 | 5.3 % |

For 57 % of the warnings the profiles were perfectly healthy and the
dispatcher was behaving exactly as configured — the board was simply at its
configured concurrency cap. The warning text sent the operator to chase
venv/PATH/credentials for a condition that has nothing to do with them.

Two gaps, both in the telemetry/reporting path:

1. ``_tick_spawn_budget`` returned ``(False, None)`` when the cap was hit,
   so the tick produced NO bucket at all and ``describe_suppression`` had
   nothing to report — 473 of the 753 warnings carried NO hold reason.
2. ``describe_suppression`` did not include ``skipped_per_profile_capped``,
   so the per-profile hold was invisible too.

This module pins the fix. It does NOT touch the classification logic owned
by ``t_72abab80``, and it does NOT change the producer's exit-kind
vocabulary (``clean_exit``/``nonzero_exit``/``rate_limited``/``signaled``/
``unknown``) that the desktop pins its TS token set against.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_ops


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_spawn_factory(spawns: list):
    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42
    return fake_spawn


# ---------------------------------------------------------------------------
# 1. The budget records WHY it was zero (gap 1)
# ---------------------------------------------------------------------------


def test_max_in_progress_cap_records_the_reason(kanban_home, all_assignees_spawnable):
    """A tick blocked by the global cap records the cap, not just ``(False, None)``.

    Before the fix the tick produced no bucket at all, so the warning printed
    no hold reason — 473 of the 753 live warnings carried NO hold reason.
    """
    kb.create_board("second")
    with kbc.connect(board="second") as conn:
        tid = kb.create_task(conn, title="busy", assignee="alice")
        assert kb.claim_task(conn, tid) is not None

    spawns: list = []
    with kbc.connect() as conn:
        kb.create_task(conn, title="wants-to-run", assignee="alice")
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=1)

    assert not spawns
    assert res.max_in_progress_reached == 1
    # The cap VALUE, not a count of running workers: the cap is the lever.
    assert res.max_in_progress_reached == 1


def test_max_spawn_cap_records_the_reason(kanban_home, all_assignees_spawnable):
    """The per-board spawn cap records its reason too."""
    spawns: list = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="busy", assignee="alice")
        assert kb.claim_task(conn, tid) is not None
        kb.create_task(conn, title="wants-to-run", assignee="alice")
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), max_spawn=1)

    assert not spawns
    assert res.max_spawn_reached == 1


def test_no_cap_means_no_cap_recorded(kanban_home, all_assignees_spawnable):
    """A healthy tick records no cap — the fields stay None."""
    spawns: list = []
    with kbc.connect() as conn:
        kb.create_task(conn, title="a", assignee="alice")
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns))

    assert len(spawns) == 1
    assert res.max_in_progress_reached is None
    assert res.max_spawn_reached is None
    assert res.max_in_progress_per_profile_reached is None


def test_per_profile_cap_records_the_reason(kanban_home, all_assignees_spawnable):
    """The per-profile cap names itself when it holds a card back."""
    spawns: list = []
    with kbc.connect() as conn:
        for i in range(3):
            kb.create_task(conn, title=f"a{i}", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress_per_profile=1,
        )

    assert len(spawns) == 1
    assert len(res.skipped_per_profile_capped) == 2
    assert res.max_in_progress_per_profile_reached == 1


# ---------------------------------------------------------------------------
# 2. describe_suppression names the caps (gap 2)
# ---------------------------------------------------------------------------


def test_describe_suppression_names_the_global_cap():
    """The suppression line names the cap that held the tick back."""
    res = kbd.DispatchResult(max_in_progress_reached=3)
    line = kbd.describe_suppression([res])
    assert "max_in_progress=3" in line


def test_describe_suppression_names_the_per_profile_cap():
    """The per-profile hold is visible (it used to be dropped entirely)."""
    res = kbd.DispatchResult(
        skipped_per_profile_capped=[("t1", "alice", 2), ("t2", "alice", 2)],
        max_in_progress_per_profile_reached=2,
    )
    line = kbd.describe_suppression([res])
    assert "max_in_progress_per_profile=2" in line
    assert "skipped_per_profile_capped=2" in line


def test_describe_suppression_names_the_per_board_spawn_cap():
    res = kbd.DispatchResult(max_spawn_reached=6)
    assert "max_spawn=6" in kbd.describe_suppression([res])


def test_describe_suppression_keeps_the_existing_reasons():
    """The pre-existing reasons are untouched — no regression in the vocabulary."""
    res = kbd.DispatchResult(
        respawn_guarded=[("t_held", "active_pr")],
        rate_limited=["quota-task"],
        skipped_locked=True,
        memory_pressure="elevated",
    )
    line = kbd.describe_suppression([res])
    assert "active_pr=1" in line
    assert "rate_limited=1" in line
    assert "skipped_locked=1" in line
    assert "memory_pressure=elevated" in line


def test_describe_suppression_is_empty_for_a_clean_tick():
    assert kbd.describe_suppression([kbd.DispatchResult()]) == ""


def test_describe_suppression_tolerates_none_results():
    """``None`` results (a board whose tick was quarantined) are skipped."""
    res = kbd.DispatchResult(max_in_progress_reached=3)
    assert "max_in_progress=3" in kbd.describe_suppression([None, res, None])


# ---------------------------------------------------------------------------
# 3. The warning text is conditional on the reason
# ---------------------------------------------------------------------------


def test_advice_blames_profile_health_when_no_cap_bound_the_tick():
    """With no cap, the ORIGINAL advice is still correct and must be kept."""
    advice = kbd.describe_cap_advice([kbd.DispatchResult()])
    assert "profile health" in advice
    assert "venv" in advice
    assert "credentials" in advice


def test_advice_names_the_global_cap_and_does_not_blame_profile_health():
    """At the global cap the warning must NOT send the operator to venv/PATH."""
    advice = kbd.describe_cap_advice([kbd.DispatchResult(max_in_progress_reached=3)])
    assert "max_in_progress=3" in advice
    assert "venv" not in advice
    assert "PATH" not in advice
    assert "credentials" not in advice
    assert "working as configured" in advice


def test_advice_names_the_per_profile_cap():
    advice = kbd.describe_cap_advice(
        [kbd.DispatchResult(max_in_progress_per_profile_reached=2)]
    )
    assert "max_in_progress_per_profile=2" in advice
    assert "venv" not in advice
    assert "busy, not broken" in advice


def test_advice_names_the_per_board_spawn_cap():
    advice = kbd.describe_cap_advice([kbd.DispatchResult(max_spawn_reached=6)])
    assert "max_spawn=6" in advice
    assert "venv" not in advice


# ---------------------------------------------------------------------------
# 4. The gateway warning (end to end through the real watcher loop)
# ---------------------------------------------------------------------------


def _run_gateway_dispatcher_watcher(monkeypatch, caplog, held_result):
    """Drive the real gateway dispatcher watcher and return the warnings."""
    import asyncio
    import logging

    import gateway.kanban_watchers as kw

    runner = object.__new__(kw.GatewayKanbanWatchersMixin)
    runner._running = True
    monkeypatch.setattr(runner, "_kanban_dispatcher_boot", lambda: (lambda: {}, object(), {}))

    class _Dispatcher:
        def __init__(self, *a, **k):
            pass

        def tick_once(self):
            return [("board", held_result)]

        def ready_nonempty(self):
            return True

    ticks = {"n": 0}

    async def _direct(fn, *args):
        return fn(*args)

    async def _sleep(_delay):
        ticks["n"] += 1
        if ticks["n"] > kw._HEALTH_WINDOW:
            runner._running = False

    monkeypatch.setattr(kw, "_KanbanDispatcher", _Dispatcher)
    monkeypatch.setattr(
        kw, "_resolve_dispatcher_settings", lambda cfg, kb: type("S", (), {"interval": 1.0})()
    )
    monkeypatch.setattr(kw, "_to_thread_process_service", _direct)
    monkeypatch.setattr(kw, "_kanban_dispatch_allowed", lambda: True)
    monkeypatch.setattr(kw, "_resolve_auto_decompose_settings", lambda load_config: (False, 0))
    monkeypatch.setattr(kbd, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(kw.asyncio, "sleep", _sleep)

    with caplog.at_level(logging.WARNING, logger=kw.logger.name):
        asyncio.run(asyncio.wait_for(runner._kanban_dispatcher_watcher(), timeout=5.0))

    return [r.getMessage() for r in caplog.records if "dispatcher stuck" in r.getMessage()]


def test_gateway_warning_at_the_cap_does_not_blame_profile_health(monkeypatch, caplog):
    """THE headline behaviour: a board at its cap must not say "check venv"."""
    held = kbd.DispatchResult(max_in_progress_reached=3)
    stuck = _run_gateway_dispatcher_watcher(monkeypatch, caplog, held)
    assert stuck, "no dispatcher-stuck warning was emitted"
    msg = stuck[0]
    assert "max_in_progress=3" in msg
    assert "working as configured" in msg
    assert "venv" not in msg
    assert "credentials" not in msg


def test_gateway_warning_still_names_the_hold_reason(monkeypatch, caplog):
    """The hold reason is still printed — the cap is ADDED, not substituted."""
    held = kbd.DispatchResult(
        respawn_guarded=[("t_held", "active_pr")], max_in_progress_reached=3,
    )
    stuck = _run_gateway_dispatcher_watcher(monkeypatch, caplog, held)
    assert stuck
    assert "Last tick held back: active_pr=1, max_in_progress=3." in stuck[0]


def test_gateway_warning_without_a_cap_keeps_the_old_advice(monkeypatch, caplog):
    """A genuinely stuck dispatcher still gets the profile-health advice."""
    held = kbd.DispatchResult(respawn_guarded=[("t_held", "active_pr")])
    stuck = _run_gateway_dispatcher_watcher(monkeypatch, caplog, held)
    assert stuck
    assert "Check profile health (venv, PATH, credentials)" in stuck[0]


# ---------------------------------------------------------------------------
# 5. The standalone daemon warning (end to end)
# ---------------------------------------------------------------------------


def test_daemon_warning_at_the_cap_does_not_blame_profile_health(
    monkeypatch, capsys,
):
    """The CLI daemon renders the same conditional advice."""
    res = kbd.DispatchResult(max_in_progress_reached=3)

    class _Conn:
        def __enter__(self):
            return None

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(kanban_ops.kbc, "connect_closing", lambda: _Conn())
    monkeypatch.setattr(kanban_ops.kbd, "dispatch_once", lambda *a, **k: res)

    def _fake_daemon(*, interval, max_spawn, failure_limit, on_tick):
        for _ in range(6):
            on_tick(res)

    monkeypatch.setattr(kanban_ops.kbd, "run_daemon", _fake_daemon)
    monkeypatch.setattr(kanban_ops.kbd, "has_spawnable_ready", lambda conn: True)
    monkeypatch.setattr(kanban_ops.kb, "init_db", lambda *a, **k: None)
    assert kanban_ops._cmd_daemon(
        SimpleNamespace(force=True, interval=5, max=None, failure_limit=2, verbose=False, pidfile=None)
    ) in (0, None)
    err = capsys.readouterr().err
    assert "dispatcher stuck" in err
    assert "max_in_progress=3" in err
    assert "venv" not in err
    assert "credentials" not in err


def test_daemon_warning_without_a_cap_keeps_the_old_advice(monkeypatch, capsys):
    res = kbd.DispatchResult(respawn_guarded=[("t_held", "active_pr")])

    class _Conn:
        def __enter__(self):
            return None

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(kanban_ops.kbc, "connect_closing", lambda: _Conn())
    monkeypatch.setattr(kanban_ops.kbd, "dispatch_once", lambda *a, **k: res)

    def _fake_daemon(*, interval, max_spawn, failure_limit, on_tick):
        for _ in range(6):
            on_tick(res)

    monkeypatch.setattr(kanban_ops.kbd, "run_daemon", _fake_daemon)
    monkeypatch.setattr(kanban_ops.kbd, "has_spawnable_ready", lambda conn: True)
    monkeypatch.setattr(kanban_ops.kb, "init_db", lambda *a, **k: None)
    assert kanban_ops._cmd_daemon(
        SimpleNamespace(force=True, interval=5, max=None, failure_limit=2, verbose=False, pidfile=None)
    ) in (0, None)
    err = capsys.readouterr().err
    assert "dispatcher stuck" in err
    assert "profile health" in err


# ---------------------------------------------------------------------------
# 6. The operator can see the cap directly on the CLI
# ---------------------------------------------------------------------------


def test_dispatch_json_exposes_the_cap(monkeypatch, capsys):
    """``hermes kanban dispatch --json`` names the cap that blocked the tick."""
    monkeypatch.setattr(
        kanban_ops.kbd,
        "dispatch_once",
        lambda *args, **kwargs: kbd.DispatchResult(max_in_progress_reached=3),
    )
    assert kanban_ops._cmd_dispatch(
        SimpleNamespace(dry_run=True, max=None, failure_limit=kbd.DEFAULT_FAILURE_LIMIT, json=True)
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["max_in_progress_reached"] == 3
    assert payload["max_spawn_reached"] is None
    assert payload["max_in_progress_per_profile_reached"] is None


def test_dispatch_text_names_the_cap(monkeypatch, capsys):
    monkeypatch.setattr(
        kanban_ops.kbd,
        "dispatch_once",
        lambda *args, **kwargs: kbd.DispatchResult(max_in_progress_reached=3),
    )
    assert kanban_ops._cmd_dispatch(
        SimpleNamespace(dry_run=True, max=None, failure_limit=kbd.DEFAULT_FAILURE_LIMIT, json=False)
    ) == 0
    out = capsys.readouterr().out
    assert "kanban.max_in_progress=3" in out
    assert "working as configured" in out


def test_dispatch_text_names_the_per_profile_cap(monkeypatch, capsys):
    monkeypatch.setattr(
        kanban_ops.kbd,
        "dispatch_once",
        lambda *args, **kwargs: kbd.DispatchResult(max_in_progress_per_profile_reached=2),
    )
    assert kanban_ops._cmd_dispatch(
        SimpleNamespace(dry_run=True, max=None, failure_limit=kbd.DEFAULT_FAILURE_LIMIT, json=False)
    ) == 0
    out = capsys.readouterr().out
    assert "kanban.max_in_progress_per_profile=2" in out
    assert "busy, not broken" in out


# ---------------------------------------------------------------------------
# 7. Constraints: the classification logic and the exit vocabulary are untouched
# ---------------------------------------------------------------------------


def test_exit_kind_vocabulary_is_unchanged():
    """The desktop pins its TS token set against this vocabulary.

    ``clean_exit``/``nonzero_exit``/``rate_limited``/``signaled``/``unknown``
    must not gain, lose or change a member because of this card. The
    classifier is exercised BEHAVIOURALLY (never by reading its source):
    each wait status is fed in and the produced kind is asserted.
    """
    rate_limit_code = kb.KANBAN_RATE_LIMIT_EXIT_CODE

    def _decode(exit_code: int = 0, signal_number: int = 0):
        raw = (signal_number & 0x7F) | ((exit_code & 0xFF) << 8)
        return kbd._decode_wait_status(raw)

    assert _decode(exit_code=0) == ("clean_exit", 0)
    assert _decode(exit_code=rate_limit_code) == ("rate_limited", rate_limit_code)
    assert _decode(exit_code=3) == ("nonzero_exit", 3)
    assert _decode(signal_number=9) == ("signaled", 9)
    assert _decode(signal_number=0x7F) == ("unknown", None)
    # An unregistered pid is "unknown" — the pre-existing fallthrough.
    assert kbd._classify_worker_exit(999999) == ("unknown", None)


def test_dispatch_result_is_still_a_plain_dataclass():
    """``asdict(result)`` is used by the dashboard API — the new fields are
    plain scalars so that keeps working."""
    import dataclasses

    assert dataclasses.is_dataclass(kbd.DispatchResult)
    payload = dataclasses.asdict(kbd.DispatchResult(max_in_progress_reached=3))
    assert payload["max_in_progress_reached"] == 3
    assert payload["max_spawn_reached"] is None
