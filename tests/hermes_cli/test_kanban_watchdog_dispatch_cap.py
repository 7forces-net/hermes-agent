"""The kanban watchdog's opt-in dispatch pass must respect the HOST cap.

Same defect class as the dashboard's manual nudge (see
``tests/plugins/test_kanban_dashboard_dispatch_cap.py``):
``_do_dispatch`` called ``dispatch_once`` with ``max_spawn=settings.max_dispatch``
(default 12) and NO ``max_in_progress``.  ``_tick_spawn_budget`` only enforces
the host cap when ``max_in_progress`` is not None, so an operator who enabled
``kanban.watchdog.allow_dispatch`` would have the watchdog bypass
``kanban.max_in_progress`` entirely and overspawn the host.

The watchdog is off by default on this host (``allow_dispatch`` is not set),
so this is a latent defect rather than an active one -- but the opt-in must
be safe, since enabling it is exactly the moment the cap stops protecting
the machine.
"""

from __future__ import annotations

import argparse

import pytest

from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_watchdog as kw


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir()
    return tmp_path / ".hermes"


def test_watchdog_dispatch_passes_host_cap_to_dispatch_once(isolated_home, monkeypatch):
    """_do_dispatch must resolve and pass kanban.max_in_progress."""
    import hermes_cli.config as config_mod

    fake_config = {"kanban": {"max_in_progress": 3}}
    # configured_max_in_progress() reads via load_config_readonly.
    monkeypatch.setattr(config_mod, "load_config_readonly", lambda: fake_config)
    monkeypatch.setattr(config_mod, "load_config", lambda: fake_config)

    captured = {}

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kbd.DispatchResult()

    monkeypatch.setattr(kwd_module(), "dispatch_once", fake_dispatch_once)

    settings = kw.WatchdogSettings(allow_dispatch=True, max_dispatch=12)
    report = kw.WatchdogReport(board="techdad")
    conn = _sqlite_conn()
    try:
        kw._do_dispatch(conn, report, settings)
    finally:
        conn.close()

    assert captured.get("max_in_progress") == 3, (
        "the watchdog dispatch pass must pass the configured host cap "
        "max_in_progress; got %r" % (captured.get("max_in_progress"),)
    )


def kwd_module():
    from hermes_cli import kanban_db_dispatch

    return kanban_db_dispatch


def _sqlite_conn():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT, "
        "status TEXT, priority INTEGER, created_by TEXT, created_at INTEGER, started_at INTEGER, "
        "completed_at INTEGER, workspace_kind TEXT, workspace_path TEXT, branch_name TEXT, "
        "claim_lock TEXT, claim_expires INTEGER, tenant TEXT, result TEXT, idempotency_key TEXT, "
        "consecutive_failures INTEGER DEFAULT 0, worker_pid INTEGER, last_failure_error TEXT, "
        "max_runtime_seconds INTEGER, last_heartbeat_at INTEGER, current_run_id INTEGER, "
        "workflow_template_id TEXT, current_step_key TEXT, skills TEXT, model_override TEXT, "
        "max_retries INTEGER, goal_mode INTEGER, goal_max_turns INTEGER, session_id TEXT, "
        "project_id TEXT, block_kind TEXT, block_recurrences INTEGER, provider_override TEXT, "
        "reasoning_effort TEXT, completion_contract TEXT, worker_started_at INTEGER)"
    )
    conn.commit()
    return conn
