"""Contract test for the kanban liveness watchdog's stale-reclaim policy.

THE BUG
-------
``scripts/kanban_hollow_table_liveness.py`` reclaimed a ``running`` card when
its heartbeat was older than 30 minutes -- even when the worker's PID was
ALIVE. On 2026-11-17 12:45 and 2026-11-18 00:03 it did exactly that to the
Phase-3 Home Lab card (``t_1e8f56fe``), whose worker was healthy but inside a
long Lighthouse tool call with no auto-heartbeat. Both times the whole
in-flight conversation was thrown away and the card restarted from scratch.

THE CONTRACT
------------
* A DEAD worker is always reclaimed -- there is nothing to lose.
* A LIVE worker is NEVER reclaimed by this watchdog. The dispatcher owns the
  staleness policy: it requires the run to exceed
  ``kanban.dispatch_stale_timeout_seconds`` AND defers the reclaim while a
  live worker survives termination.

These tests drive the REAL ``tick()`` against a REAL board database, so they
fail on the pre-fix script for the behavioural reason (a reclaim call was
made) and not merely because a new attribute is missing.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT = Path(
    r"C:/Users/admin/AppData/Local/hermes/scripts/kanban_hollow_table_liveness.py"
)

# Schema columns the watchdog reads. Kept minimal but real: the script's own
# SQL is exercised unchanged.
TASKS_DDL = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT,
    status TEXT,
    assignee TEXT,
    worker_pid INTEGER,
    worker_started_at TEXT,
    last_heartbeat_at INTEGER,
    started_at INTEGER,
    created_at INTEGER,
    goal_mode INTEGER DEFAULT 0,
    goal_max_turns INTEGER,
    block_kind TEXT,
    block_recurrences INTEGER DEFAULT 0,
    consecutive_failures INTEGER DEFAULT 0,
    last_failure_error TEXT
);
CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
    kind TEXT, payload TEXT, created_at INTEGER);
CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
    ended_at INTEGER);
"""


def _load_watchdog():
    """Import the watchdog script by path.

    The script is a HOST ops script (it is what the ``kanban-liveness-all-boards``
    cron job runs), not a repo file -- the cron job resolves
    ``script: kanban_hollow_table_liveness.py`` against ``HERMES_HOME/scripts/``.
    So the test targets it by absolute path and SKIPS when this host does not
    have it, rather than failing on a machine that legitimately lacks it.
    """
    if not SCRIPT.exists():
        pytest.skip(f"host watchdog script not present: {SCRIPT}")
    spec = importlib.util.spec_from_file_location("kanban_liveness_watchdog", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def wd():
    return _load_watchdog()


@pytest.fixture
def board(tmp_path, monkeypatch):
    """A board directory with a real (minimal) kanban.db holding one running card."""
    (tmp_path / "kanban.db").write_text("", encoding="utf-8")
    conn = sqlite3.connect(str(tmp_path / "kanban.db"))
    conn.executescript(TASKS_DDL)
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        "sqlite3.connect", sqlite3.connect, raising=False
    )
    return tmp_path


def _seed_running(board: Path, *, pid: int, hb_age: int, now: int) -> None:
    conn = sqlite3.connect(str(board / "kanban.db"))
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, worker_pid, "
        "last_heartbeat_at, started_at, created_at) "
        "VALUES ('t_probe', 'probe card', 'running', 'dadwriter', ?, ?, ?, ?)",
        (pid, now - hb_age, now - hb_age, now - hb_age),
    )
    conn.commit()
    conn.close()


def _run_tick(wd, board: Path, monkeypatch, *, pid_alive: bool):
    """Run the watchdog's ``tick()`` and capture every ``hermes`` CLI call."""
    calls: list[list[str]] = []
    monkeypatch.setattr(wd, "is_pid_alive", lambda pid: pid_alive)
    monkeypatch.setattr(wd, "hermes", lambda b, *args: calls.append(list(args)))
    monkeypatch.setattr(wd, "log", lambda *a, **k: None)
    monkeypatch.setattr(wd, "db_path", lambda b: board / "kanban.db")
    monkeypatch.setattr(wd, "now_ts", lambda: 1_000_000)
    wd.tick("probe")
    return calls


def test_live_worker_with_stale_heartbeat_is_not_reclaimed(wd, board, monkeypatch):
    """THE BUG: a live worker was reclaimed on a stale heartbeat alone."""
    _seed_running(board, pid=4242, hb_age=8 * 60 * 60, now=1_000_000)
    calls = _run_tick(wd, board, monkeypatch, pid_alive=True)

    assert calls == [], (
        "a LIVE worker must never be reclaimed by the watchdog; "
        f"got reclaim calls: {calls}"
    )


def test_live_worker_stale_beyond_any_plausible_window_is_not_reclaimed(
    wd, board, monkeypatch
):
    """Even a 3-day-old heartbeat must not reclaim a live worker here."""
    _seed_running(board, pid=4242, hb_age=3 * 24 * 60 * 60, now=1_000_000)
    calls = _run_tick(wd, board, monkeypatch, pid_alive=True)

    assert calls == [], (
        "the dispatcher owns the stale policy and defers for a live worker; "
        f"got reclaim calls: {calls}"
    )


def test_dead_worker_is_always_reclaimed(wd, board, monkeypatch):
    """A dead worker is always safe to reclaim, even with a fresh heartbeat."""
    _seed_running(board, pid=4242, hb_age=5, now=1_000_000)
    calls = _run_tick(wd, board, monkeypatch, pid_alive=False)

    assert len(calls) == 1, f"a dead worker must be reclaimed; got {calls}"
    assert "reclaim" in calls[0]
    assert "t_probe" in calls[0]


def test_fresh_heartbeat_on_live_worker_is_not_reclaimed(wd, board, monkeypatch):
    """The ordinary case: a live, heartbeating worker is left alone."""
    _seed_running(board, pid=4242, hb_age=30, now=1_000_000)
    calls = _run_tick(wd, board, monkeypatch, pid_alive=True)

    assert calls == []


def test_stale_floor_is_at_least_an_hour(wd):
    """The floor must not be lower than the dispatcher's own heartbeat gap."""
    assert wd.STALE_RECLAIM_MIN_SECONDS >= 3600


def test_dispatch_stale_timeout_matches_the_dispatcher_policy(wd):
    """The watchdog reads the SAME config key the dispatcher uses."""
    import yaml

    cfg_path = Path(r"C:/Users/admin/AppData/Local/hermes/config.yaml")
    if not cfg_path.exists():
        pytest.skip("config.yaml not present on this host")
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    expected = int(((cfg.get("kanban") or {}).get("dispatch_stale_timeout_seconds") or 0))
    assert expected > 0, "config must define the dispatcher's stale timeout"
    assert wd.dispatch_stale_timeout_seconds() == expected


def test_dispatcher_gap_is_not_smaller_than_the_watchdog_floor(wd):
    """The dispatcher's own heartbeat gap must not undercut the watchdog floor."""
    sys.path.insert(0, r"C:/Users/admin/AppData/Local/hermes/hermes-agent")
    try:
        from hermes_cli.kanban_db_dispatch import _STALE_HEARTBEAT_GAP_SECONDS
    except Exception as exc:  # pragma: no cover - import guard
        pytest.skip(f"hermes_cli not importable here: {exc}")
    assert _STALE_HEARTBEAT_GAP_SECONDS >= wd.STALE_RECLAIM_MIN_SECONDS
