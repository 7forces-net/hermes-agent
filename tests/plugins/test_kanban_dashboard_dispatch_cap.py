"""The dashboard's manual "Nudge dispatch" button must not bypass the HOST
concurrency cap.

Measured incident (2026-11-16, techdad board): the dashboard's
``POST /api/plugins/kanban/dispatch?max=8`` called ``dispatch_once`` with
``max_spawn=8`` and ``max_in_progress=None``.  With ``kanban.max_in_progress: 3``
configured, the HOST cap was never evaluated, so 3 running cards + a 5-card
burst = 8 workers on a host with <1 GB free RAM (96% used).

``_tick_spawn_budget`` only enforces the host cap when ``max_in_progress`` is
not None.  Every other dispatch entry point (gateway watcher, ``hermes kanban
dispatch``) resolves that value from config; the dashboard endpoint did not.

These tests pin the contract:
  1. the endpoint resolves ``max_in_progress`` from config and passes it through;
  2. a manual nudge while the host is at its cap spawns NOTHING;
  3. the endpoint still honours an explicit ``?max=`` ceiling.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


def _load_plugin():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_kanban_dispatch_cap_test", plugin_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return _load_plugin()


@pytest.fixture
def client(plugin):
    app = FastAPI()
    app.include_router(plugin.router, prefix="/api/plugins/kanban")
    return TestClient(app)


def _write_config(plugin, tmp_path, **kanban_cfg):
    """Write a root config.yaml carrying the given ``kanban:`` block."""
    import yaml

    home = Path(str(tmp_path / ".hermes"))
    (home / "config.yaml").write_text(
        yaml.safe_dump({"kanban": kanban_cfg}), encoding="utf-8"
    )


def test_dispatch_endpoint_resolves_max_in_progress_from_config(plugin, client, tmp_path, monkeypatch):
    """The endpoint must read kanban.max_in_progress and pass it to dispatch_once."""
    _write_config(plugin, tmp_path, max_in_progress=3)

    seen = {}

    def _fake_dispatch_once(conn, **kwargs):
        seen.update(kwargs)
        return type("R", (), {"__iter__": lambda self: iter(())})()

    monkeypatch.setattr(plugin.kbd, "dispatch_once", _fake_dispatch_once)

    r = client.post("/api/plugins/kanban/dispatch?max=8")
    assert r.status_code == 200, r.text
    assert seen.get("max_in_progress") == 3, (
        "the dashboard dispatch endpoint must pass the configured host cap "
        "max_in_progress to dispatch_once; got %r" % (seen.get("max_in_progress"),)
    )


def test_dispatch_endpoint_spawns_nothing_when_host_is_at_cap(plugin, client, tmp_path, monkeypatch):
    """With max_in_progress=3 and 3 workers already running, a manual nudge
    must not spawn a 4th worker."""
    _write_config(plugin, tmp_path, max_in_progress=3)

    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect()
    try:
        for i in range(3):
            kb.create_task(conn, title="busy %d" % i, assignee="dadresearch")
        # Mark 3 as running the way the dispatcher does.
        conn.execute("UPDATE tasks SET status = 'running'")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE status='running'").fetchone()[0] == 3
    finally:
        conn.close()

    spawned = []

    def _fake_dispatch_once(conn, **kwargs):
        # Mirror the real budget maths so the test exercises the REAL cap path.
        from hermes_cli import kanban_db_dispatch as kbd

        running = kbd.count_running_tasks(conn)
        cap = kwargs.get("max_in_progress")
        if cap is not None and running >= cap:
            return type("R", (), {"spawned": [], "skipped_locked": False})()
        spawned.append(running)
        return type("R", (), {"spawned": [], "skipped_locked": False})()

    monkeypatch.setattr(plugin.kbd, "dispatch_once", _fake_dispatch_once)

    r = client.post("/api/plugins/kanban/dispatch?max=8")
    assert r.status_code == 200, r.text
    assert spawned == [], (
        "a manual dispatch nudge must not spawn while the host is at "
        "max_in_progress=3; the endpoint dropped the host cap"
    )


def test_dispatch_endpoint_still_honours_explicit_max_query(plugin, client, tmp_path, monkeypatch):
    """An explicit ?max= must still bound the per-board spawn count."""
    _write_config(plugin, tmp_path, max_in_progress=3)

    seen = {}

    def _fake_dispatch_once(conn, **kwargs):
        seen.update(kwargs)
        return type("R", (), {"spawned": [], "skipped_locked": False})()

    monkeypatch.setattr(plugin.kbd, "dispatch_once", _fake_dispatch_once)

    r = client.post("/api/plugins/kanban/dispatch?max=4")
    assert r.status_code == 200, r.text
    assert seen.get("max_spawn") == 4, (
        "the explicit ?max= ceiling must still be passed through; got %r"
        % (seen.get("max_spawn"),)
    )
