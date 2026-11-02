"""Tests for ``hermes kanban watchdog`` (the repo-side board hygiene sweep).

The watchdog replaces three UNTRACKED local scripts
(``kanban-completion-watchdog.py``, ``kanban_token_window_resume.py``,
``kanban_token_refresh_watchdog.py``) whose behaviour had no test at all.

The two properties under test are the ones that make the watchdog SAFE to
ship, both of which the old scripts got wrong in opposite directions:

1. **Dispatch is OFF by default.**  A second dispatcher on a board the
   gateway already dispatches spawns orphans (card ``t_616ce70a`` measured
   1389 of 3388 board crashes as CLI-spawned).  The watchdog must never
   spawn a worker unless the operator opts in, and even then it must go
   through the machine-global singleton dispatcher lock.

2. **The provider health gate blocks every mutation.**  Acting inside a dead
   provider window crashes a fresh batch of workers and books failures
   against work that was never attempted.

Plus the four behaviours the scripts implemented: promote, reclaim, recover,
detect.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_watchdog as kw


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
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


# --- helpers ---------------------------------------------------------------


def _healthy_probe():
    """A provider route that answers."""
    return (True, "HTTP 200 (OK)")


def _dead_probe():
    """A provider route that is out of credits."""
    return (False, "HTTP 402 Add credits")


def _settings(**overrides):
    """Settings with the health gate satisfied by default (tests supply probes)."""
    base = dict(require_provider_health=True, detect_only=False, allow_dispatch=False)
    base.update(overrides)
    return kw.WatchdogSettings(**base)


def _sweep(conn, **overrides):
    """Run one sweep with a single healthy provider route."""
    probes = overrides.pop("probes", [("test-route", _healthy_probe)])
    settings = overrides.pop("settings", _settings())
    return kw.run_watchdog(
        board="default", settings=settings, conn=conn, probes=probes, **overrides
    )


def _make_todo_with_done_parent(conn, title="child"):
    """A ``todo`` card whose parent is done — the promote scenario."""
    parent = kb.create_task(conn, title="parent", assignee="setup")
    child = kb.create_task(conn, title=title, assignee="setup", parents=[parent])
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
    return child, parent


def _make_running_dead_worker(conn, heartbeat_age=600, title="stuck"):
    """A ``running`` card with a dead worker PID and an old heartbeat."""
    task_id = kb.create_task(conn, title=title, assignee="setup")
    now = int(time.time())
    conn.execute(
        "UPDATE tasks SET status='running', worker_pid=999999, "
        "       claim_lock=?, claim_expires=?, last_heartbeat_at=?, started_at=? "
        "WHERE id=?",
        (f"{kb._host_prefix()}fake", now + 900, now - heartbeat_age, now - heartbeat_age, task_id),
    )
    return task_id


# ---------------------------------------------------------------------------
# 1. Dispatch is OFF by default
# ---------------------------------------------------------------------------


def test_watchdog_never_dispatches_without_opt_in(conn, monkeypatch):
    """The default sweep must not spawn a worker, and must say why."""
    calls: list = []

    def _boom(*a, **kw):
        calls.append((a, kw))
        raise AssertionError("dispatch_once must not be called without --allow-dispatch")

    monkeypatch.setattr(kw.kbd, "dispatch_once", _boom)

    kb.create_task(conn, title="ready work", assignee="setup")
    report = _sweep(conn)

    assert calls == []
    assert report.dispatched == []
    assert "off by default" in report.dispatch_skipped_reason


def test_watchdog_config_allow_dispatch_defaults_false():
    """``allow_dispatch`` defaults to False in the shipped settings."""
    settings = kw.WatchdogSettings.from_config({})
    assert settings.allow_dispatch is False
    assert settings.require_provider_health is True


def test_watchdog_dispatches_only_when_explicitly_allowed(conn, monkeypatch):
    """With the opt-in, the watchdog DOES dispatch."""
    seen: dict = {}

    class _FakeResult:
        spawned = [("t_fake", "setup", "/tmp/ws")]
        skipped_locked = False

    def _fake_dispatch_once(conn, **kw):
        seen.update(kw)
        return _FakeResult()

    monkeypatch.setattr(kw.kbd, "dispatch_once", _fake_dispatch_once)
    # The singleton lock must be acquirable in the test env.
    monkeypatch.setattr(
        kw, "_singleton_dispatcher_lock", lambda: _held_lock()
    )

    report = _sweep(conn, settings=_settings(allow_dispatch=True))
    assert report.dispatched == ["t_fake"]
    assert "dispatch_once" in str(seen) or seen  # the call happened


def test_watchdog_skips_dispatch_when_singleton_lock_is_contended(conn, monkeypatch):
    """Another dispatcher holds the lock -> the watchdog must not spawn."""
    called: list = []

    def _fake_dispatch_once(conn, **kw):
        called.append(1)
        raise AssertionError("must not dispatch while the lock is contended")

    monkeypatch.setattr(kw.kbd, "dispatch_once", _fake_dispatch_once)
    monkeypatch.setattr(
        kw, "_singleton_dispatcher_lock", lambda: _contended_lock()
    )

    report = _sweep(conn, settings=_settings(allow_dispatch=True))
    assert called == []
    assert report.dispatched == []
    assert "singleton lock" in report.dispatch_skipped_reason


def test_watchdog_refuses_dispatch_when_lock_is_unavailable(conn, monkeypatch):
    """An unsynchronised dispatcher is the orphan factory -> refuse."""
    called: list = []

    def _fake_dispatch_once(conn, **kw):
        called.append(1)

    monkeypatch.setattr(kw.kbd, "dispatch_once", _fake_dispatch_once)
    monkeypatch.setattr(
        kw, "_singleton_dispatcher_lock", lambda: _unavailable_lock()
    )

    report = _sweep(conn, settings=_settings(allow_dispatch=True))
    assert called == []
    assert report.dispatched == []
    assert any("unavailable" in e for e in report.errors)


# ---------------------------------------------------------------------------
# 2. The provider health gate blocks mutations
# ---------------------------------------------------------------------------


def test_health_gate_blocks_every_mutation_when_providers_are_down(conn):
    """No provider answers -> the board is left completely untouched."""
    child, parent = _make_todo_with_done_parent(conn)
    dead = _make_running_dead_worker(conn)

    report = _sweep(conn, probes=[("down", _dead_probe)])

    assert report.probe is not None
    assert report.probe.healthy is False
    assert report.promoted == []
    assert report.reclaimed == []
    assert report.recovered == []
    # Nothing was mutated.
    assert kb.get_task(conn, child).status == "todo"
    assert kb.get_task(conn, dead).status == "running"
    assert any("provider chain is down" in e for e in report.errors)


def test_health_gate_is_satisfied_when_one_route_answers(conn):
    """One healthy route is enough — the sweep proceeds."""
    child, _parent = _make_todo_with_done_parent(conn)

    report = _sweep(conn, probes=[("down", _dead_probe), ("up", _healthy_probe)])

    assert report.probe.healthy is True
    assert report.probe.healthy_routes == ["up"]
    assert child in report.promoted


def test_probe_never_raises_on_a_broken_route(conn):
    """A route whose probe raises counts as DOWN, not as a sweep failure."""

    def _explode():
        raise RuntimeError("boom")

    result = kw.probe_provider_chain([("bad", _explode), ("good", _healthy_probe)])
    assert result.healthy is True
    bad = [r for r in result.routes if r["name"] == "bad"][0]
    assert bad["healthy"] is False
    assert "boom" in bad["detail"]


def test_probe_with_no_routes_is_healthy(conn):
    """Nothing configured to probe -> nothing to gate on."""
    result = kw.probe_provider_chain([])
    assert result.healthy is True
    assert result.routes == []


# ---------------------------------------------------------------------------
# 3. promote
# ---------------------------------------------------------------------------


def test_promote_moves_a_todo_card_whose_parents_are_done(conn):
    child, _parent = _make_todo_with_done_parent(conn)
    assert kb.get_task(conn, child).status == "todo"

    report = _sweep(conn)

    assert child in report.promoted
    assert kb.get_task(conn, child).status == "ready"


def test_promote_leaves_a_todo_card_whose_parent_is_open(conn):
    parent = kb.create_task(conn, title="open parent", assignee="setup")
    child = kb.create_task(conn, title="child", assignee="setup", parents=[parent])

    report = _sweep(conn)

    assert child not in report.promoted
    assert kb.get_task(conn, child).status == "todo"


def test_promote_is_idempotent(conn):
    """A second sweep promotes nothing new."""
    _child, _parent = _make_todo_with_done_parent(conn)
    _sweep(conn)
    report = _sweep(conn)
    assert report.promoted == []


# ---------------------------------------------------------------------------
# 4. reclaim
# ---------------------------------------------------------------------------


def test_reclaim_releases_a_card_whose_worker_is_dead(conn):
    task_id = _make_running_dead_worker(conn)

    report = _sweep(conn)

    assert task_id in report.reclaimed
    assert kb.get_task(conn, task_id).status != "running"


def test_reclaim_leaves_a_card_whose_worker_is_alive(conn, monkeypatch):
    task_id = _make_running_dead_worker(conn)
    monkeypatch.setattr(kw, "_worker_alive", lambda pid, started=None: True)

    report = _sweep(conn)

    assert task_id not in report.reclaimed
    assert kb.get_task(conn, task_id).status == "running"


def test_reclaim_leaves_a_freshly_dead_worker(conn, monkeypatch):
    """Dead PID but a FRESH heartbeat -> the dispatcher has not reaped it yet."""
    task_id = _make_running_dead_worker(conn, heartbeat_age=1)
    monkeypatch.setattr(kw, "_worker_alive", lambda pid, started=None: False)

    report = _sweep(conn)

    assert task_id not in report.reclaimed
    assert kb.get_task(conn, task_id).status == "running"


def test_reclaim_ignores_another_hosts_claim(conn):
    """Another host's PID number is meaningless here — do not release its claim."""
    task_id = _make_running_dead_worker(conn)
    conn.execute(
        "UPDATE tasks SET claim_lock=? WHERE id=?", ("otherhost:1234", task_id)
    )

    report = _sweep(conn)

    assert task_id not in report.reclaimed
    assert kb.get_task(conn, task_id).status == "running"


# ---------------------------------------------------------------------------
# 5. recover
# ---------------------------------------------------------------------------


def test_recover_unblocks_a_crashed_card(conn):
    """A crashed card parked in ``blocked`` is released back into the queue."""
    task_id = kb.create_task(conn, title="crashed impl", assignee="setup")
    conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (task_id,))
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
        "VALUES (?, 'setup', 'crashed', 'crashed', ?, ?)",
        (task_id, int(time.time()) - 60, int(time.time())),
    )

    report = _sweep(conn)

    assert task_id in report.recovered
    assert kb.get_task(conn, task_id).status in ("ready", "todo")


def test_recover_leaves_a_healthy_blocked_card_alone(conn):
    """A card blocked for a HUMAN reason must not be auto-released."""
    task_id = kb.create_task(conn, title="needs a human", assignee="setup")
    conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (task_id,))
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
        "VALUES (?, 'setup', 'done', 'completed', ?, ?)",
        (task_id, int(time.time()) - 60, int(time.time())),
    )

    report = _sweep(conn)

    assert task_id not in report.recovered
    assert kb.get_task(conn, task_id).status == "blocked"


def test_recover_does_not_defeat_the_circuit_breaker(conn):
    """A crashed card AT its failure limit stays blocked for a human.

    ``unblock_task`` resets ``consecutive_failures``.  If the watchdog
    unblocked a card the breaker had just tripped, it would hand the card
    straight back to a worker, the worker would crash again, and the card
    would loop forever — the watchdog would be the reason the breaker could
    never latch.
    """
    task_id = kb.create_task(conn, title="breaker tripped", assignee="setup")
    conn.execute(
        "UPDATE tasks SET status='blocked', consecutive_failures=? WHERE id=?",
        (kw.kbd.DEFAULT_FAILURE_LIMIT, task_id),
    )
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
        "VALUES (?, 'setup', 'crashed', 'crashed', ?, ?)",
        (task_id, int(time.time()) - 60, int(time.time())),
    )

    report = _sweep(conn)

    assert task_id not in report.recovered
    assert kb.get_task(conn, task_id).status == "blocked"
    stuck = [s for s in report.stuck if s["task_id"] == task_id]
    assert stuck and stuck[0]["reason"] == "breaker_tripped"


def test_recover_leaves_an_explicitly_blocked_card_alone(conn):
    """A card a human PARKED is never auto-released by the watchdog.

    An explicit ``kanban block`` emits a ``blocked`` event; a breaker trip
    emits ``gave_up``.  The board's own ``recompute_ready`` uses exactly
    that distinction (``_has_sticky_block``) to decide whether a blocked
    card may auto-recover.  The watchdog MUST use the same discriminator:
    otherwise it unblocks work a human deliberately stopped, because some
    EARLIER run happened to crash — and the run that crashed may be the one
    that led to the human blocking it in the first place.

    The block is applied WITHOUT a reason so the crash stays the newest
    run: ``block_task`` synthesizes a ``blocked`` run when given a reason,
    which would mask the crash and make this test pass for the wrong
    reason.
    """
    task_id = kb.create_task(conn, title="parked by a human", assignee="setup")
    conn.execute("UPDATE tasks SET status='running', consecutive_failures=1 WHERE id=?", (task_id,))
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
        "VALUES (?, 'setup', 'crashed', 'crashed', ?, ?)",
        (task_id, int(time.time()) - 60, int(time.time())),
    )
    # The human parks it. This is what makes the block STICKY.
    kb.block_task(conn, task_id, reason=None)
    assert kb.get_task(conn, task_id).status == "blocked"
    # The crash is still the newest run — this is the dangerous shape.
    last = conn.execute(
        "SELECT status, outcome FROM task_runs WHERE task_id=? "
        "ORDER BY started_at DESC, id DESC LIMIT 1", (task_id,)
    ).fetchone()
    assert (last["status"], last["outcome"]) == ("crashed", "crashed")

    report = _sweep(conn)

    assert task_id not in report.recovered
    assert kb.get_task(conn, task_id).status == "blocked"
    stuck = [s for s in report.stuck if s["task_id"] == task_id]
    assert stuck and stuck[0]["reason"] == "sticky_block"


def test_recover_still_recovers_a_breaker_tripped_card(conn):
    """The sticky-block guard must not block the legitimate recovery path.

    A breaker trip emits ``gave_up`` (not ``blocked``), so the card is NOT
    sticky and the watchdog may release it — that is the whole point of the
    ``recover`` action.
    """
    task_id = kb.create_task(conn, title="breaker tripped, not sticky", assignee="setup")
    conn.execute("UPDATE tasks SET status='blocked', consecutive_failures=1 WHERE id=?", (task_id,))
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
        "VALUES (?, 'setup', 'crashed', 'crashed', ?, ?)",
        (task_id, int(time.time()) - 60, int(time.time())),
    )
    # A breaker trip appends a ``gave_up`` event, not a ``blocked`` one.
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) "
        "VALUES (?, 'gave_up', ?, ?)",
        (task_id, json.dumps({"failures": 2}), int(time.time())),
    )

    report = _sweep(conn)

    assert task_id in report.recovered
    assert kb.get_task(conn, task_id).status in ("ready", "todo")


def test_probe_timeout_is_applied_to_the_route_request(monkeypatch):
    """``probe_timeout_s`` must reach the request, not just the settings.

    The knob was documented and configurable but the probe hard-coded the
    module default, so an operator who lowered the timeout (to stop a
    hung provider from stalling a cron sweep) had no effect at all.
    """
    seen: dict = {}

    class _FakeCompletions:
        def create(self, **kwargs):
            seen.update(kwargs)
            raise RuntimeError("stop here")

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    def _fake_resolve(provider, model=None):
        return _FakeClient(), "fake-model"

    import agent.auxiliary_client as aux
    monkeypatch.setattr(aux, "resolve_provider_client", _fake_resolve)

    probe = kw._make_route_probe("some-provider", "some-model", timeout_s=7)
    healthy, detail = probe()

    assert seen["timeout"] == 7
    assert healthy is False
    assert "stop here" in detail


def test_default_probes_carry_the_configured_timeout(monkeypatch):
    """The configured timeout reaches every route built from the chain."""
    import agent.auxiliary_client as aux
    import hermes_cli.kanban_watchdog as watchdog_mod

    seen: dict = {}

    class _SpyCompletions:
        def create(self, **kwargs):
            seen.update(kwargs)
            return kwargs

    class _SpyChat:
        completions = _SpyCompletions()

    class _SpyClient:
        chat = _SpyChat()

    monkeypatch.setattr(aux, "resolve_provider_client", lambda p, m=None: (_SpyClient(), "m"))
    # The chain comes from config; pin one so the test does not depend on
    # the machine's real provider configuration.
    monkeypatch.setattr(
        watchdog_mod, "default_provider_probes",
        lambda timeout_s: [
            ("fake-provider", watchdog_mod._make_route_probe("p", "m", timeout_s=timeout_s))
        ],
    )

    result = kw.probe_provider_chain(timeout_s=11)
    assert result.routes, "the chain must produce at least one route"

    # The route's request must carry the configured timeout.
    for _name, probe in watchdog_mod.default_provider_probes(timeout_s=11):
        probe()
    assert seen.get("timeout") == 11


# ---------------------------------------------------------------------------
# 6. detect
# ---------------------------------------------------------------------------


def test_detect_reports_a_blocked_card_that_needs_a_human(conn):
    task_id = kb.create_task(conn, title="parked", assignee="setup")
    conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (task_id,))
    old = int(time.time()) - 4000
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'blocked', ?, ?)",
        (task_id, json.dumps({"reason": "waiting on a human"}), old),
    )

    report = _sweep(conn)

    stuck = [s for s in report.stuck if s["task_id"] == task_id]
    assert stuck, "a long-blocked card must be reported"
    assert stuck[0]["reason"] == "blocked_too_long"


def test_detect_reports_a_qa_reject_loop(conn):
    task_id = kb.create_task(conn, title="qa card", assignee="gameqa")
    now = int(time.time())
    for i in range(kw.DEFAULT_QA_REJECT_LOOP_RUNS):
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
            "VALUES (?, 'gameqa', 'blocked', 'blocked', ?, ?)",
            (task_id, now - i * 10, now - i * 10 + 1),
        )

    report = _sweep(conn)

    assert task_id in report.qa_reject_loops


def test_detect_reports_reclaim_thrash(conn):
    task_id = kb.create_task(conn, title="thrashing", assignee="setup")
    now = int(time.time())
    for i in range(kw.DEFAULT_RECLAIM_THRASH_RUNS):
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'reclaimed', ?, ?)",
            (task_id, json.dumps({"reason": "watchdog"}), now - i * 10),
        )

    report = _sweep(conn)

    assert task_id in report.reclaim_thrash


def test_detect_only_changes_nothing(conn):
    """``--detect-only`` is read-only."""
    child, _parent = _make_todo_with_done_parent(conn)
    dead = _make_running_dead_worker(conn)

    report = _sweep(conn, settings=_settings(detect_only=True))

    assert report.promoted == []
    assert report.reclaimed == []
    assert kb.get_task(conn, child).status == "todo"
    assert kb.get_task(conn, dead).status == "running"


# ---------------------------------------------------------------------------
# 7. The CLI surface
# ---------------------------------------------------------------------------


def test_cli_watchdog_runs_and_prints_json(kanban_home, capsys, monkeypatch):
    """``hermes kanban watchdog --json`` emits a machine-readable report."""
    monkeypatch.setattr(
        kw, "probe_provider_chain",
        lambda *a, **k: kw.ProbeResult(healthy=True, routes=[]),
    )
    rc = kc.run_slash("watchdog --json")
    payload = json.loads(rc)
    assert "promoted" in payload
    assert "dispatch_skipped_reason" in payload


def test_cli_watchdog_verb_is_registered():
    """The verb exists in the handler table and the parser tree."""
    assert "watchdog" in kc._HANDLERS
    assert "watchdog" in kc._DELEGATED_CHILD_DENIED_ACTIONS


def test_cli_watchdog_does_not_dispatch_by_default(kanban_home, capsys, monkeypatch):
    """The CLI path also refuses to dispatch without the opt-in flag."""
    calls: list = []
    monkeypatch.setattr(
        kw.kbd, "dispatch_once",
        lambda conn, **kw: calls.append(1),
    )
    monkeypatch.setattr(
        kw, "probe_provider_chain",
        lambda *a, **k: kw.ProbeResult(healthy=True, routes=[]),
    )
    rc = kc.run_slash("watchdog")
    # run_slash returns the printed output, not an exit code.
    assert "dispatch skipped" in rc
    assert "dispatch is off by default" in rc
    assert calls == []


# ---------------------------------------------------------------------------
# 8. Report shape
# ---------------------------------------------------------------------------


def test_report_json_is_serialisable(conn):
    report = _sweep(conn)
    blob = json.dumps(report.to_dict())
    assert "board" in blob
    assert "provider_gate_skipped" in blob


def test_format_report_mentions_every_action_class(conn):
    child, _parent = _make_todo_with_done_parent(conn)
    dead = _make_running_dead_worker(conn)

    report = _sweep(conn)
    text = kw.format_report(report)

    assert f"promoted {child}" in text
    assert f"reclaimed {dead}" in text
    assert "=== watchdog sweep end" in text


# ---------------------------------------------------------------------------
# 9. The configuration surface
# ---------------------------------------------------------------------------


def test_config_surface_has_a_reader_for_every_key():
    """Every ``kanban.watchdog.*`` key in DEFAULT_CONFIG is actually read.

    The repo invariant is that a registered key nothing reads is a knob that
    does nothing.  This test pins the reader for each key so a rename in
    config_defaults.py cannot silently orphan it.
    """
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    cfg = DEFAULT_CONFIG["kanban"]["watchdog"]
    assert cfg, "kanban.watchdog must ship defaults"
    for key in cfg:
        assert key in {
            "require_provider_health", "detect_only", "allow_dispatch", "max_dispatch",
            "dead_worker_heartbeat_s", "stuck_heartbeat_s", "stuck_running_s",
            "stuck_blocked_s", "stuck_todo_s", "qa_reject_loop_runs",
            "reclaim_thrash_runs", "probe_timeout_s",
        }, f"unexpected watchdog config key: {key}"

    # from_config() must map every key onto a settings field.
    settings = kw.WatchdogSettings.from_config(cfg)
    for key in cfg:
        assert hasattr(settings, key), f"watchdog config key {key} has no reader"


def test_config_surface_overrides_are_honoured():
    """A config value actually changes the resolved settings."""
    settings = kw.WatchdogSettings.from_config({
        "allow_dispatch": True,
        "require_provider_health": False,
        "stuck_blocked_s": 5,
        "qa_reject_loop_runs": 3,
    })
    assert settings.allow_dispatch is True
    assert settings.require_provider_health is False
    assert settings.stuck_blocked_s == 5
    assert settings.qa_reject_loop_runs == 3


def test_config_surface_ignores_a_bogus_value():
    """A non-numeric threshold falls back to the default, not to zero."""
    settings = kw.WatchdogSettings.from_config({"stuck_blocked_s": "not-a-number"})
    assert settings.stuck_blocked_s == kw.DEFAULT_STUCK_BLOCKED_S


# ---------------------------------------------------------------------------
# lock helpers
# ---------------------------------------------------------------------------


import contextlib  # noqa: E402  (kept next to the helpers that use it)


@contextlib.contextmanager
def _held_lock():
    yield (None, "held")


@contextlib.contextmanager
def _contended_lock():
    yield (None, "contended")


@contextlib.contextmanager
def _unavailable_lock():
    yield (None, "unavailable")
