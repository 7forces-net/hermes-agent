"""``hermes kanban watchdog`` — repo-side board hygiene: promote / reclaim / recover / detect.

WHY THIS EXISTS
---------------
Three UNTRACKED local scripts under ``<hermes-home>/scripts/`` carried the
board's promote/reclaim/recover hygiene:

* ``kanban-completion-watchdog.py`` (cron ``*/5 * * * *``)
* ``kanban_token_window_resume.py`` (cron ``5 1,6,11,16,21 * * *``)
* ``kanban_token_refresh_watchdog.py`` (cron ``*/5 * * * *`` — DISABLED)

None were in the repo, none had a test, and their behaviour was invisible to
anyone reading the codebase.  This module is the ported, tested, documented
version of what they did.

THE TWO HARD RULES
------------------
1. **Dispatch is OFF by default.**  Card ``t_616ce70a`` measured that a
   second dispatcher on a board the gateway already dispatches is harmful:
   ``hermes kanban dispatch`` exits immediately after spawning, so the
   process that spawned a worker is gone before that worker dies and nobody
   can reap it (1389 of the board's 3388 crashes were CLI-spawned).  The
   watchdog therefore NEVER dispatches unless the operator explicitly opts
   in with ``--allow-dispatch``, and even then it goes through the
   machine-global singleton dispatcher lock (``kanban/.dispatcher.lock``)
   that the gateway uses — the same lock ``t_716018cc`` adds to the CLI
   ``dispatch`` verb.

2. **Never act inside a dead provider window.**  A worker that starts while
   the provider is out of credits dies immediately, the dispatcher books it
   as a failure, and the breaker trips on work that was never attempted.
   The token-window script's health gate is the fix: probe the provider
   chain FIRST, and when no route answers, touch nothing.

WHAT THE WATCHDOG DOES
-----------------------
``promote``   ``todo`` cards whose parents are all done/archived -> ``ready``.
``reclaim``   ``running`` cards whose worker PID is dead (stale claim).
``recover``   crashed implementation cards: reclaim + re-promote so a fresh
              worker picks them up instead of the card sitting unclaimed.
``detect``    cards stuck with no heartbeat, and QA cards in a reject loop.
``probe``     the provider chain before acting (the health gate).

NEVER force-completes a card and never marks one done on its own — it only
makes the board runnable again.

See ``website/docs/user-guide/features/kanban-watchdog.md`` for the
operator-facing configuration surface.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


# ---------------------------------------------------------------------------
# Defaults (every one of these is overridable from config.yaml)
# ---------------------------------------------------------------------------

# A ``running`` card whose worker PID is dead AND whose heartbeat is older
# than this is a stale claim.  The completion watchdog used 60s; the token
# window script used 60s.  Kept at 60s because the heartbeat is written on
# every activity touch.
DEFAULT_DEAD_WORKER_HEARTBEAT_S = 60

# A ``running`` card with no heartbeat at all for this long is "stuck" and
# is reported (not reclaimed — a live worker with a slow model can go a long
# time without a heartbeat; ``release_stale_claims`` owns the TTL).
DEFAULT_STUCK_HEARTBEAT_S = 30 * 60

# A ``running`` card whose worker has been alive this long with no
# completion is reported as stuck (the reclaim path is PID-driven).
DEFAULT_STUCK_RUNNING_S = 60 * 60

# A card that has been ``blocked`` this long with a review-required reason is
# reported for human attention.  The old refresh watchdog UNBLOCKED such
# cards after 2 minutes; that is a destructive policy (it re-runs a card a
# human deliberately parked) so the shipped watchdog only DETECTS it.
DEFAULT_STUCK_BLOCKED_S = 30 * 60

# A card that has been ``todo`` this long without being promoted is
# reported — the graph says its parents are done but nothing moved it.
DEFAULT_STUCK_TODO_S = 60 * 60

# A QA card whose recent runs are ALL rejections this many times in a row is
# a reject loop — the artifact never satisfies the acceptance criterion.
DEFAULT_QA_REJECT_LOOP_RUNS = 8

# Consecutive reclaims of one card at or above this threshold is a
# reclaim-thrash loop: the worker keeps re-running without converging.
DEFAULT_RECLAIM_THRASH_RUNS = 5

# Provider probe timeout.
DEFAULT_PROBE_TIMEOUT_S = 45


# ---------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    """Outcome of one provider-chain health probe."""

    healthy: bool
    routes: list[dict] = field(default_factory=list)
    """One entry per probed route: ``{name, healthy, detail}``."""
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "healthy": self.healthy,
            "routes": list(self.routes),
            "error": self.error,
        }

    @property
    def healthy_routes(self) -> list[str]:
        return [r["name"] for r in self.routes if r.get("healthy")]


@dataclass
class WatchdogReport:
    """What one watchdog sweep did (or why it did nothing)."""

    board: str = ""
    promoted: list[str] = field(default_factory=list)
    reclaimed: list[str] = field(default_factory=list)
    recovered: list[str] = field(default_factory=list)
    stuck: list[dict] = field(default_factory=list)
    qa_reject_loops: list[str] = field(default_factory=list)
    reclaim_thrash: list[str] = field(default_factory=list)
    dispatched: list[str] = field(default_factory=list)
    dispatch_skipped_reason: str = ""
    probe: Optional[ProbeResult] = None
    provider_gate_skipped: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "board": self.board,
            "promoted": list(self.promoted),
            "reclaimed": list(self.reclaimed),
            "recovered": list(self.recovered),
            "stuck": list(self.stuck),
            "qa_reject_loops": list(self.qa_reject_loops),
            "reclaim_thrash": list(self.reclaim_thrash),
            "dispatched": list(self.dispatched),
            "dispatch_skipped_reason": self.dispatch_skipped_reason,
            "provider_gate_skipped": self.provider_gate_skipped,
            "probe": self.probe.to_dict() if self.probe else None,
            "errors": list(self.errors),
        }

    @property
    def did_anything(self) -> bool:
        return bool(
            self.promoted or self.reclaimed or self.recovered or self.stuck
            or self.qa_reject_loops or self.reclaim_thrash or self.dispatched
        )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _kanban_config() -> dict:
    """``config.yaml`` ``kanban:`` section, or ``{}`` when config can't be loaded."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        return (cfg.get("kanban", {}) if isinstance(cfg, dict) else {}) or {}
    except Exception:
        return {}


def _watchdog_config() -> dict:
    """``config.yaml`` ``kanban.watchdog:`` section, or ``{}``."""
    return _kanban_config().get("watchdog") or {}


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


@dataclass
class WatchdogSettings:
    """Resolved watchdog knobs. Every field has a default; config only overrides."""

    dead_worker_heartbeat_s: int = DEFAULT_DEAD_WORKER_HEARTBEAT_S
    stuck_heartbeat_s: int = DEFAULT_STUCK_HEARTBEAT_S
    stuck_running_s: int = DEFAULT_STUCK_RUNNING_S
    stuck_blocked_s: int = DEFAULT_STUCK_BLOCKED_S
    stuck_todo_s: int = DEFAULT_STUCK_TODO_S
    qa_reject_loop_runs: int = DEFAULT_QA_REJECT_LOOP_RUNS
    reclaim_thrash_runs: int = DEFAULT_RECLAIM_THRASH_RUNS
    probe_timeout_s: int = DEFAULT_PROBE_TIMEOUT_S
    require_provider_health: bool = True
    """When True (default) the sweep refuses to act if no provider route answers."""
    detect_only: bool = False
    """When True the sweep reports what it WOULD do and changes nothing."""
    allow_dispatch: bool = False
    """When False (default) the sweep never spawns a worker. See the module docstring."""
    max_dispatch: int = 12

    @classmethod
    def from_config(cls, cfg: Optional[dict] = None) -> "WatchdogSettings":
        cfg = cfg if cfg is not None else _watchdog_config()
        if not isinstance(cfg, dict):
            cfg = {}
        defaults = cls()
        return cls(
            dead_worker_heartbeat_s=_positive_int(
                cfg.get("dead_worker_heartbeat_s"), defaults.dead_worker_heartbeat_s),
            stuck_heartbeat_s=_positive_int(
                cfg.get("stuck_heartbeat_s"), defaults.stuck_heartbeat_s),
            stuck_running_s=_positive_int(
                cfg.get("stuck_running_s"), defaults.stuck_running_s),
            stuck_blocked_s=_positive_int(
                cfg.get("stuck_blocked_s"), defaults.stuck_blocked_s),
            stuck_todo_s=_positive_int(
                cfg.get("stuck_todo_s"), defaults.stuck_todo_s),
            qa_reject_loop_runs=_positive_int(
                cfg.get("qa_reject_loop_runs"), defaults.qa_reject_loop_runs),
            reclaim_thrash_runs=_positive_int(
                cfg.get("reclaim_thrash_runs"), defaults.reclaim_thrash_runs),
            probe_timeout_s=_positive_int(
                cfg.get("probe_timeout_s"), defaults.probe_timeout_s),
            require_provider_health=bool(
                cfg.get("require_provider_health", defaults.require_provider_health)),
            detect_only=bool(cfg.get("detect_only", defaults.detect_only)),
            allow_dispatch=bool(cfg.get("allow_dispatch", defaults.allow_dispatch)),
            max_dispatch=_positive_int(cfg.get("max_dispatch"), defaults.max_dispatch),
        )


# ---------------------------------------------------------------------------
# Provider health gate
# ---------------------------------------------------------------------------


def _probe_route(name: str, probe: Callable[[], tuple[bool, str]]) -> dict:
    """Run one route probe; never raises — a broken probe is a DOWN route."""
    try:
        healthy, detail = probe()
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "healthy": False, "detail": f"{type(exc).__name__}: {exc}"}
    return {"name": name, "healthy": bool(healthy), "detail": detail}


def probe_provider_chain(
    probes: Optional[list[tuple[str, Callable[[], tuple[bool, str]]]]] = None,
    *,
    timeout_s: int = DEFAULT_PROBE_TIMEOUT_S,
) -> ProbeResult:
    """Probe the provider chain; healthy when AT LEAST ONE route answers.

    ``probes`` is a list of ``(route_name, probe_callable)``.  Each callable
    returns ``(healthy, detail)``.  This mirrors the token-window script's
    ``probe_minimax`` / ``probe_kimi`` but is provider-agnostic and takes its
    routes from the caller, so the repo ships no hard-coded vendor URLs.

    A route that raises counts as DOWN, never as an error that aborts the
    sweep: one broken route must not hide a healthy sibling.
    """
    if probes is None:
        probes = default_provider_probes(timeout_s=timeout_s)
    if not probes:
        # No route configured — nothing to gate on. The sweep proceeds.
        return ProbeResult(healthy=True, routes=[])
    routes = [_probe_route(name, fn) for name, fn in probes]
    healthy = [r for r in routes if r["healthy"]]
    result = ProbeResult(healthy=bool(healthy), routes=routes)
    if not healthy:
        result.error = "no provider route answered"
    return result


def default_provider_probes(
    timeout_s: int = DEFAULT_PROBE_TIMEOUT_S,
) -> list[tuple[str, Callable[[], tuple[bool, str]]]]:
    """Build probes for the CONFIGURED provider chain (main + fallbacks).

    Uses the runtime provider resolver so the probe hits the same endpoint
    and credentials the worker will use — a probe against a different route
    proves nothing.  Returns ``[]`` when no route can be resolved, which the
    caller treats as "nothing to gate on".

    ``timeout_s`` is applied to every route's request, so a hung provider
    cannot stall the sweep beyond the configured budget.
    """
    from hermes_cli.fallback_config import get_fallback_chain

    try:
        from hermes_cli.config import load_config_readonly

        raw = load_config_readonly()
    except Exception:
        raw = {}

    routes: list[tuple[str, Callable[[], tuple[bool, str]]]] = []
    chain = get_fallback_chain(raw if isinstance(raw, dict) else {})

    main_provider = ""
    main_model = ""
    if isinstance(raw, dict):
        model_cfg = raw.get("model") or {}
        if isinstance(model_cfg, dict):
            main_provider = str(model_cfg.get("provider") or "").strip()
            main_model = str(model_cfg.get("default") or "").strip()

    entries = [{"provider": main_provider, "model": main_model}] + list(chain)
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider") or "").strip()
        if not provider:
            continue
        model = str(entry.get("model") or "").strip()
        if (provider, model) in seen:
            continue
        seen.add((provider, model))
        label = f"{provider}" + (f"/{model}" if model else "")
        routes.append((label, _make_route_probe(provider, model, timeout_s=timeout_s)))
    return routes


def _make_route_probe(provider: str, model: str, timeout_s: int = DEFAULT_PROBE_TIMEOUT_S) -> Callable[[], tuple[bool, str]]:
    """One tiny real request against ``provider``; returns ``(healthy, detail)``.

    The probe goes through the central provider router
    (``agent.auxiliary_client.resolve_provider_client``) and then issues the
    request against THAT client directly.  It deliberately does NOT go
    through ``call_llm``: that helper walks the fallback chain, so a probe of
    route A could be answered by route B and every route would look healthy.
    The health gate needs per-route truth.

    ``timeout_s`` is the CONFIGURED probe timeout.  It is applied to the
    request itself, so a hung route cannot stall the sweep for longer than
    the operator allowed.
    """

    def _probe() -> tuple[bool, str]:
        try:
            from agent.auxiliary_client import resolve_provider_client
        except Exception as exc:  # noqa: BLE001
            return False, f"provider router unavailable: {type(exc).__name__}: {exc}"

        try:
            client, resolved_model = resolve_provider_client(provider, model or None)
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        if client is None:
            return False, "no client could be resolved for this route"

        try:
            response = client.chat.completions.create(
                model=resolved_model or model or "",
                messages=[{"role": "user", "content": "Reply with exactly: OK"}],
                max_tokens=8,
                timeout=timeout_s,
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        text = _probe_response_text(response)
        if text is None:
            return False, "empty response"
        return True, f"HTTP 200 ({text[:32]!r})"

    return _probe


def _probe_response_text(response: Any) -> Optional[str]:
    """Best-effort text extraction from an OpenAI-shaped chat completion."""
    try:
        choices = getattr(response, "choices", None)
        if choices is None and isinstance(response, dict):
            choices = response.get("choices")
        if not choices:
            return None
        first = choices[0]
        message = getattr(first, "message", None)
        if message is None and isinstance(first, dict):
            message = first.get("message")
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        if content is None:
            return None
        return str(content).strip()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Board inspection
# ---------------------------------------------------------------------------


def _now() -> int:
    return int(time.time())


def _host_prefix() -> str:
    return kb._host_prefix()


def _row_int(row: Any, key: str) -> Optional[int]:
    value = kb._row_get(row, key, None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _worker_alive(pid: Optional[int], started_at: Any = None) -> bool:
    """True when the worker PID is live AND is still the process we spawned.

    Delegates to the dispatcher's own liveness predicate so the watchdog
    cannot disagree with the dispatcher about whether a worker is alive.
    """
    if not pid:
        return False
    try:
        return bool(kbd._worker_alive(pid, started_at))
    except Exception:  # noqa: BLE001
        return False


def _promotable_ids(conn) -> list[str]:
    """``todo`` cards whose parents are ALL done/archived.

    ``promote_task`` refuses a card whose parents are still open, so the
    filter is done in SQL first: a card that cannot be promoted would
    otherwise cost a write attempt every sweep.
    """
    rows = conn.execute(
        "SELECT t.id FROM tasks t "
        "WHERE t.status = 'todo' AND t.id LIKE 't_%' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM task_links l JOIN tasks p ON p.id = l.parent_id "
        "  WHERE l.child_id = t.id AND p.status NOT IN ('done','archived'))"
    ).fetchall()
    return [r[0] for r in rows]


def _running_rows(conn) -> list:
    return conn.execute(
        "SELECT id, title, assignee, worker_pid, worker_started_at, "
        "       last_heartbeat_at, started_at, status "
        "FROM tasks WHERE status = 'running'"
    ).fetchall()


def _blocked_rows(conn) -> list:
    return conn.execute(
        "SELECT id, title, assignee, block_kind, last_heartbeat_at, created_at "
        "FROM tasks WHERE status = 'blocked'"
    ).fetchall()


def _todo_rows(conn) -> list:
    return conn.execute(
        "SELECT id, title, assignee, created_at FROM tasks WHERE status = 'todo'"
    ).fetchall()


def _latest_block_event(conn, task_id: str) -> tuple[Optional[int], str]:
    """``(created_at, reason)`` of the newest ``blocked`` event for a card."""
    row = conn.execute(
        "SELECT created_at, payload FROM task_events "
        "WHERE task_id = ? AND kind = 'blocked' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None, ""
    reason = ""
    payload = kb._json_dict(kb._row_get(row, "payload"))
    if isinstance(payload, dict):
        reason = str(payload.get("reason") or "")
    return _row_int(row, "created_at"), reason


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _do_promote(conn, report: WatchdogReport, settings: WatchdogSettings) -> None:
    """Promote ``todo`` cards whose parents are all done."""
    for task_id in _promotable_ids(conn):
        ok, reason = kb.promote_task(
            conn, task_id, actor="kanban-watchdog", reason="watchdog: parents complete",
        )
        if ok:
            report.promoted.append(task_id)
        else:
            report.errors.append(f"promote {task_id}: {reason}")


def _do_reclaim(conn, report: WatchdogReport, settings: WatchdogSettings) -> None:
    """Reclaim ``running`` cards whose worker PID is dead (stale claim).

    Only HOST-LOCAL claims are touched: another host's PID number is
    meaningless here, so reclaiming it would release a live claim elsewhere.
    """
    host_prefix = _host_prefix()
    now = _now()
    for row in _running_rows(conn):
        task_id = row["id"]
        pid = _row_int(row, "worker_pid")
        if not pid:
            continue
        lock = conn.execute(
            "SELECT claim_lock FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        claim_lock = (lock["claim_lock"] or "") if lock is not None else ""
        if claim_lock and not claim_lock.startswith(host_prefix):
            continue  # not our host — the PID means nothing to us
        if _worker_alive(pid, kb._row_get(row, "worker_started_at")):
            continue
        heartbeat = _row_int(row, "last_heartbeat_at")
        age = (now - heartbeat) if heartbeat else settings.dead_worker_heartbeat_s
        if heartbeat is not None and age < settings.dead_worker_heartbeat_s:
            # Dead PID but the heartbeat is fresh — the worker just exited and
            # the dispatcher has not reaped it yet. Leave it to the dispatcher.
            continue
        if kb.reclaim_task(
            conn, task_id,
            reason=f"watchdog: worker_pid {pid} is dead (heartbeat {age}s old)",
        ):
            report.reclaimed.append(task_id)


def _has_sticky_block(conn, task_id: str) -> bool:
    """True when the card carries an EXPLICIT block (a human parked it).

    Delegates to the board's own discriminator so the watchdog cannot
    disagree with the dispatcher about whether a blocked card is a
    deliberate park or a breaker trip.  ``recompute_ready`` uses the same
    rule to decide whether a blocked card may auto-recover.
    """
    try:
        return bool(kb._has_sticky_block(conn, task_id))
    except Exception:  # noqa: BLE001
        # Fail SAFE: an unreadable card is treated as deliberately parked.
        return True


def _do_recover(conn, report: WatchdogReport, settings: WatchdogSettings) -> None:
    """Recover crashed implementation cards: reclaim + re-promote.

    A crashed card is a card whose LAST run ended ``crashed`` and whose card
    is now ``blocked`` (the breaker blocked it) or ``ready`` (the dispatcher
    released it but nothing picked it up).  The recovery is: release the
    claim, clear the breaker block, and put it back in the queue so a fresh
    worker attempts it.

    Two guards keep the watchdog from re-running work a human deliberately
    stopped:

    * **A sticky block is never released.**  An explicit ``kanban block``
      (a worker or operator parking the card) emits a ``blocked`` event; a
      breaker trip emits ``gave_up`` instead.  The board's own
      ``recompute_ready`` uses exactly this distinction (``_has_sticky_block``)
      to decide whether a blocked card may auto-recover.  The watchdog uses
      the SAME discriminator, so it can never unblock a card that is waiting
      on a human just because some earlier run happened to crash.

    * **The circuit breaker is not defeated.**  ``unblock_task`` resets
      ``consecutive_failures``, so a card that has already burned its failure
      budget is left alone.  Without that guard the watchdog would unblock a
      card the breaker just tripped, hand it straight back to a worker, and
      the card would crash forever — the watchdog would become the loop that
      keeps the breaker from ever latching.
    """
    rows = conn.execute(
        "SELECT id, title, assignee, status, consecutive_failures, max_retries "
        "FROM tasks WHERE status IN ('blocked','ready') AND id LIKE 't_%'"
    ).fetchall()
    failure_limit = _failure_limit()
    for row in rows:
        task_id = row["id"]
        run = conn.execute(
            "SELECT status, outcome FROM task_runs WHERE task_id = ? "
            "ORDER BY started_at DESC, id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if run is None:
            continue
        last_status = (run["status"] or "").strip()
        last_outcome = (run["outcome"] or "").strip()
        if last_status != "crashed" and last_outcome != "crashed":
            continue
        if row["status"] == "blocked":
            # A sticky block is an EXPLICIT "this card is parked" — a human
            # decision (or a worker's own block). The watchdog reports it and
            # leaves it alone; only an operator may release it.
            if _has_sticky_block(conn, task_id):
                report.stuck.append({
                    "task_id": task_id,
                    "reason": "sticky_block",
                    "detail": (
                        "blocked explicitly (waiting on a human or a deliberate "
                        "park) — not auto-released"
                    ),
                    "assignee": row["assignee"],
                    "title": row["title"],
                })
                continue
            # Respect the breaker: a card at its failure limit stays blocked
            # for a human. ``unblock_task`` would reset the counter, so the
            # check has to happen BEFORE the unblock.
            failures = int(row["consecutive_failures"] or 0)
            task_override = row["max_retries"]
            effective_limit = (
                int(task_override) if task_override is not None else failure_limit
            )
            if failures >= effective_limit:
                report.stuck.append({
                    "task_id": task_id,
                    "reason": "breaker_tripped",
                    "detail": (
                        f"crashed and at the failure limit "
                        f"({failures}/{effective_limit}) — needs a human"
                    ),
                    "assignee": row["assignee"],
                    "title": row["title"],
                })
                continue
            if not kb.unblock_task(conn, task_id):
                report.errors.append(f"recover {task_id}: unblock refused")
                continue
            report.recovered.append(task_id)
        elif row["status"] == "ready":
            # Already queued — it will be picked up on the next dispatch tick.
            report.recovered.append(task_id)


def _failure_limit() -> int:
    """The dispatcher's failure limit (``kanban.failure_limit``)."""
    try:
        return int(_kanban_config().get("failure_limit") or kbd.DEFAULT_FAILURE_LIMIT)
    except (TypeError, ValueError):
        return kbd.DEFAULT_FAILURE_LIMIT


def _do_detect(conn, report: WatchdogReport, settings: WatchdogSettings) -> None:
    """Report cards that are stuck or looping. Read-only — never mutates."""
    now = _now()

    # (a) ``running`` with no heartbeat for a long time.
    for row in _running_rows(conn):
        heartbeat = _row_int(row, "last_heartbeat_at")
        started = _row_int(row, "started_at")
        pid = _row_int(row, "worker_pid")
        hb_age = (now - heartbeat) if heartbeat else None
        running_age = (now - started) if started else None
        if hb_age is not None and hb_age > settings.stuck_heartbeat_s:
            report.stuck.append({
                "task_id": row["id"],
                "reason": "no_heartbeat",
                "detail": f"running {running_age}s ago, last heartbeat {hb_age}s ago",
                "assignee": row["assignee"],
                "title": row["title"],
            })
        elif running_age is not None and running_age > settings.stuck_running_s:
            report.stuck.append({
                "task_id": row["id"],
                "reason": "running_too_long",
                "detail": f"running for {running_age}s with no completion",
                "assignee": row["assignee"],
                "title": row["title"],
            })

    # (b) ``blocked`` too long for a human to have acted on it.
    for row in _blocked_rows(conn):
        blocked_at, reason = _latest_block_event(conn, row["id"])
        age = (now - blocked_at) if blocked_at else None
        if age is not None and age > settings.stuck_blocked_s:
            report.stuck.append({
                "task_id": row["id"],
                "reason": "blocked_too_long",
                "detail": f"blocked {age}s ago: {reason[:160]}",
                "assignee": row["assignee"],
                "title": row["title"],
            })

    # (c) ``todo`` cards whose parents ARE done but which never moved.
    promotable = set(_promotable_ids(conn))
    for row in _todo_rows(conn):
        if row["id"] in promotable:
            continue  # the promote action handles it
        created = _row_int(row, "created_at")
        age = (now - created) if created else None
        if age is not None and age > settings.stuck_todo_s:
            report.stuck.append({
                "task_id": row["id"],
                "reason": "stuck_in_todo",
                "detail": f"todo for {age}s (parents not all done)",
                "assignee": row["assignee"],
                "title": row["title"],
            })

    # (d) QA reject loop: recent runs are ALL rejections.
    report.qa_reject_loops.extend(_detect_qa_reject_loops(conn, settings))

    # (e) reclaim thrash: the same card reclaimed over and over.
    report.reclaim_thrash.extend(_detect_reclaim_thrash(conn, settings))


def _detect_qa_reject_loops(conn, settings: WatchdogSettings) -> list[str]:
    """Cards whose last N runs were all rejections — an artifact that never passes."""
    threshold = settings.qa_reject_loop_runs
    if threshold < 2:
        return []
    rows = conn.execute(
        "SELECT id, title, assignee FROM tasks "
        "WHERE status IN ('blocked','ready','todo','running') AND assignee LIKE '%qa%'"
    ).fetchall()
    loops: list[str] = []
    for row in rows:
        runs = conn.execute(
            "SELECT status, outcome FROM task_runs WHERE task_id = ? "
            "ORDER BY started_at DESC, id DESC LIMIT ?",
            (row["id"], threshold),
        ).fetchall()
        if len(runs) < threshold:
            continue
        rejected = 0
        for run in runs:
            status = (run["status"] or "").strip()
            outcome = (run["outcome"] or "").strip()
            if status in ("blocked", "changes_requested") or outcome in (
                "blocked", "changes_requested",
            ):
                rejected += 1
        if rejected == threshold:
            loops.append(row["id"])
    return loops


def _detect_reclaim_thrash(conn, settings: WatchdogSettings) -> list[str]:
    """Cards reclaimed at/above the threshold times — a worker that never converges."""
    threshold = settings.reclaim_thrash_runs
    if threshold < 2:
        return []
    rows = conn.execute(
        "SELECT id FROM tasks WHERE status IN ('ready','blocked','running','todo') AND id LIKE 't_%'"
    ).fetchall()
    thrash: list[str] = []
    for row in rows:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events "
            "WHERE task_id = ? AND kind = 'reclaimed'",
            (row["id"],),
        ).fetchone()
        if int(count["n"] or 0) >= threshold:
            thrash.append(row["id"])
    return thrash


# ---------------------------------------------------------------------------
# Dispatch (OFF by default)
# ---------------------------------------------------------------------------


def _singleton_dispatcher_lock():
    """The machine-global singleton dispatcher lock the gateway uses.

    Returns a context manager yielding ``(handle, state)`` where state is
    ``"held"``, ``"contended"`` or ``"unavailable"``.  Mirrors
    ``gateway.kanban_watchers_common._acquire_singleton_lock`` so the
    watchdog and the gateway contend on the SAME lock file.
    """
    try:
        from gateway.kanban_watchers_common import (
            _acquire_singleton_lock,
            _release_singleton_lock,
        )
    except Exception:  # pragma: no cover - gateway package always present in-tree
        return contextlib.nullcontext((None, "unavailable"))

    lock_path = kb.kanban_home() / "kanban" / ".dispatcher.lock"

    @contextlib.contextmanager
    def _locked():
        handle, state = _acquire_singleton_lock(lock_path)
        try:
            yield handle, state
        finally:
            if handle is not None:
                _release_singleton_lock(handle)

    return _locked()


def _do_dispatch(conn, report: WatchdogReport, settings: WatchdogSettings) -> None:
    """Optional dispatch pass — only when the operator opted in.

    Goes through the machine-global singleton lock so the watchdog can never
    be a second dispatcher alongside the gateway.
    """
    if not settings.allow_dispatch:
        report.dispatch_skipped_reason = (
            "dispatch is off by default (pass --allow-dispatch to opt in; "
            "a second dispatcher spawns orphans — see t_616ce70a)"
        )
        return

    if settings.detect_only:
        report.dispatch_skipped_reason = "detect-only mode — nothing was dispatched"
        return

    with _singleton_dispatcher_lock() as (handle, state):
        if state == "contended":
            report.dispatch_skipped_reason = (
                "another dispatcher holds the singleton lock — skipping (no spawn)"
            )
            return
        if state == "unavailable":
            report.errors.append(
                "singleton dispatcher lock unavailable — refusing to dispatch "
                "(an unsynchronised dispatcher is the orphan factory)"
            )
            return
        result = kbd.dispatch_once(
            conn,
            max_spawn=settings.max_dispatch,
            board=report.board or None,
        )
        report.dispatched.extend(tid for tid, _who, _ws in result.spawned)
        if result.skipped_locked:
            report.dispatch_skipped_reason = "board dispatch lock held by another writer"


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def run_watchdog(
    *,
    board: Optional[str] = None,
    settings: Optional[WatchdogSettings] = None,
    conn=None,
    probes: Optional[list[tuple[str, Callable[[], tuple[bool, str]]]]] = None,
    probe: bool = True,
) -> WatchdogReport:
    """Run one watchdog sweep; returns a :class:`WatchdogReport`.

    ``conn`` may be supplied by a caller that already holds a connection
    (the CLI passes one); otherwise a short-lived connection is opened.

    ``probe=False`` skips the provider health gate — only for tests and for
    operators who deliberately disabled the gate in config.
    """
    settings = settings or WatchdogSettings.from_config()
    report = WatchdogReport(board=board or kb.get_current_board())

    # --- 1. provider health gate -----------------------------------------
    if not probe:
        # The operator explicitly skipped the gate (--no-probe).
        report.probe = None
        report.provider_gate_skipped = True
    elif settings.require_provider_health:
        result = probe_provider_chain(probes, timeout_s=settings.probe_timeout_s)
        report.probe = result
        if not result.healthy:
            # Nothing can serve a worker right now. Do NOT touch the board:
            # reclaiming and re-promoting inside a dead window just crashes a
            # fresh batch of workers and books failures against work that was
            # never really attempted.
            report.errors.append(
                "provider chain is down — leaving the board untouched "
                f"(routes: {', '.join(r['name'] for r in result.routes) or 'none'})"
            )
            return report
    else:
        report.probe = ProbeResult(healthy=True, routes=[])
        report.provider_gate_skipped = True

    if conn is not None:
        _sweep(conn, report, settings)
        return report

    with kbc.connect_closing(board=board) as c:
        _sweep(c, report, settings)
    return report


def _sweep(conn, report: WatchdogReport, settings: WatchdogSettings) -> None:
    """The mutating part of a sweep, after the health gate passed."""
    if settings.detect_only:
        _do_detect(conn, report, settings)
        return
    _do_promote(conn, report, settings)
    _do_reclaim(conn, report, settings)
    _do_recover(conn, report, settings)
    _do_detect(conn, report, settings)
    _do_dispatch(conn, report, settings)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def format_report(report: WatchdogReport) -> str:
    """Human-readable summary — one line per fact, then a summary line."""
    lines: list[str] = []
    for task_id in report.promoted:
        lines.append(f"promoted {task_id}")
    for task_id in report.reclaimed:
        lines.append(f"reclaimed {task_id} (dead worker)")
    for task_id in report.recovered:
        lines.append(f"recovered {task_id} (crashed → requeued)")
    for item in report.stuck:
        lines.append(
            f"STUCK {item['task_id']} [{item['reason']}] {item.get('detail','')}"
        )
    for task_id in report.qa_reject_loops:
        lines.append(f"QA_REJECT_LOOP {task_id} — repeated rejections, needs a human")
    for task_id in report.reclaim_thrash:
        lines.append(f"RECLAIM_THRASH {task_id} — reclaimed repeatedly without converging")
    for task_id in report.dispatched:
        lines.append(f"dispatched {task_id}")
    if report.dispatch_skipped_reason:
        lines.append(f"dispatch skipped: {report.dispatch_skipped_reason}")
    for err in report.errors:
        lines.append(f"ERROR {err}")
    summary = (
        f"promoted={len(report.promoted)} reclaimed={len(report.reclaimed)} "
        f"recovered={len(report.recovered)} stuck={len(report.stuck)} "
        f"qa_loops={len(report.qa_reject_loops)} thrash={len(report.reclaim_thrash)} "
        f"dispatched={len(report.dispatched)}"
    )
    lines.append(f"=== watchdog sweep end: {summary} ===")
    return "\n".join(lines)


def _print_report(report: WatchdogReport, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=True))
    else:
        print(format_report(report))
