---
sidebar_position: 13
title: "Kanban watchdog (board hygiene)"
description: "Promote, reclaim, recover, and detect — the repo-side command that replaces three untracked local scripts"
---

# Kanban watchdog — board hygiene

`hermes kanban watchdog` runs one hygiene sweep over a board: it **promotes**
cards whose dependencies are satisfied, **reclaims** cards whose worker died,
**recovers** cards whose worker crashed, and **detects** cards that are stuck
or looping.

It is the repo-side replacement for three untracked local scripts that used to
carry this behaviour on some installations. Those scripts were invisible to
anyone reading the codebase and had no tests at all; this command is the same
behaviour, ported, tested, and documented.

```bash
hermes kanban watchdog                       # hygiene sweep on the current board
hermes kanban watchdog --detect-only         # report what it would do; change nothing
hermes kanban watchdog --json                # machine-readable report
hermes kanban --board other watchdog         # a specific board
```

## Two rules that make it safe to run unattended

### 1. It never dispatches unless you ask

**Dispatch is off by default.** A second dispatcher on a board the gateway
already dispatches is harmful: `hermes kanban dispatch` exits immediately
after spawning a worker, so the process that spawned it is gone before the
worker dies and nothing can ever reap that worker. On one measured board,
1389 of 3388 crashes were spawned that way.

The watchdog therefore never spawns a worker unless you pass
`--allow-dispatch` (or set `kanban.watchdog.allow_dispatch: true`). Even when
enabled, the spawn goes through the machine-global singleton dispatcher lock
(`<hermes-home>/kanban/.dispatcher.lock`) — the same lock the gateway's
embedded dispatcher takes — so the watchdog can never be a second dispatcher
alongside it.

On a board where the gateway is already dispatching, you do not need
`--allow-dispatch` at all: the watchdog promotes cards to `ready` and the
gateway's next tick picks them up.

### 2. It never acts inside a dead provider window

Before it touches anything, the watchdog **probes the provider chain** — the
main provider plus every configured fallback — with one tiny real request per
route. If no route answers, the sweep leaves the board completely untouched
and says so.

This is the failure mode that matters. A worker that starts while the
provider is out of credits dies immediately, the dispatcher books it as a
failure, and the board's circuit breaker trips on work that was never really
attempted. Probing first turns "the board quietly died for hours" into "the
board is fine, the provider is down, retry at the next window".

The probe hits each route **individually** — it does not walk the fallback
chain — so the report tells you which routes are actually alive rather than
which one happened to answer.

## What each action does

| Action | What it does | Mutates? |
|---|---|---|
| `promote` | `todo` cards whose parents are all `done`/`archived` → `ready` | yes |
| `reclaim` | `running` cards whose worker PID is dead and whose heartbeat is stale → released back to `ready` | yes |
| `recover` | cards whose last run **crashed** and which the breaker parked in `blocked` → released back into the queue | yes |
| `detect` | cards stuck with no heartbeat, cards blocked too long, QA cards in a reject loop, cards reclaimed over and over | **no** |

Two things the watchdog deliberately does **not** do:

- **It never force-completes a card.** The deliverable is the worker's to
  verify and complete; the watchdog only makes the board runnable again.
- **It never auto-unblocks a card a human parked.** A `blocked` card older
  than `stuck_blocked_s` is *reported*, not released. (An older local script
  unblocked every blocked card after two minutes, which re-ran work a human
  had deliberately stopped.)

`recover` respects that boundary too. A card is only released when its block
came from the **circuit breaker** (the dispatcher gave up), not from an
explicit `kanban block`. The board's own dispatcher uses the same
distinction, so the watchdog cannot disagree with it about whether a card is
a deliberate park or a failed attempt:

| How the card got blocked | Event | Watchdog |
|---|---|---|
| The dispatcher gave up (breaker tripped) | `gave_up` | released back into the queue |
| A worker or operator parked it | `blocked` | **reported, never released** |

A card parked by a human is reported as `STUCK … [sticky_block]`.

`reclaim` only touches host-local claims: another host's PID number means
nothing here, so releasing it would pull the card out from under a live
worker on that host.

## Configuration

All settings live under `kanban.watchdog` in `config.yaml`:

```yaml
kanban:
  watchdog:
    # Probe the provider chain before acting; leave the board untouched when
    # no route answers. Turning this off is how you get a board that trips its
    # own breaker inside a dead window.
    require_provider_health: true

    # Report-only: say what the sweep would do, change nothing.
    detect_only: false

    # Spawn workers. OFF by default — see "It never dispatches unless you ask".
    # Even when true, the spawn takes the singleton dispatcher lock.
    allow_dispatch: false

    # Cap on workers spawned per sweep when allow_dispatch is true.
    max_dispatch: 12

    # A `running` card whose worker PID is dead AND whose heartbeat is at
    # least this old is a stale claim -> reclaimed.
    dead_worker_heartbeat_s: 60

    # A `running` card with no heartbeat for this long is reported as stuck.
    stuck_heartbeat_s: 1800

    # A `running` card older than this with no completion is reported as stuck.
    stuck_running_s: 3600

    # A `blocked` card older than this is reported for human attention.
    stuck_blocked_s: 1800

    # A `todo` card older than this whose parents are not all done is reported.
    stuck_todo_s: 3600

    # A QA card whose last N runs were ALL rejections is a reject loop.
    qa_reject_loop_runs: 8

    # A card reclaimed at least N times is thrashing.
    reclaim_thrash_runs: 5

    # Timeout for one provider health probe.
    probe_timeout_s: 45
```

## Running it on a schedule

The watchdog is a one-shot command, so any scheduler can drive it. The
pattern that replaces the old local scripts is:

```bash
# Every 5 minutes: hygiene only (the gateway does the dispatching).
*/5 * * * *  hermes kanban watchdog

# At provider-token-window boundaries: same sweep, but the health gate
# decides whether anything happens at all.
5 1,6,11,16,21 * * *  hermes kanban watchdog
```

If you use Hermes' own cron, the job runs `hermes kanban watchdog` with the
board pinned via `--board <slug>` (or `HERMES_KANBAN_BOARD`).

Keep the two schedules distinct. A frequent hygiene sweep is fine — it is
idempotent and cheap. A sweep that runs *only* at token-window boundaries
gets the health gate's full value, because the moment it runs is the moment
the provider just refreshed.

## Reading the report

```text
promoted t_a1b2c3
reclaimed t_d4e5f6 (dead worker)
recovered t_g7h8i9 (crashed → requeued)
STUCK t_j1k2l3 [blocked_too_long] blocked 4200s ago: waiting on a human
QA_REJECT_LOOP t_m4n5o6 — repeated rejections, needs a human
RECLAIM_THRASH t_p7q8r9 — reclaimed repeatedly without converging
dispatch skipped: dispatch is off by default (pass --allow-dispatch to opt in; a second dispatcher spawns orphans — see t_616ce70a)
=== watchdog sweep end: promoted=1 reclaimed=1 recovered=1 stuck=1 qa_loops=1 thrash=1 dispatched=0 ===
```

Every line is one fact. The summary line at the end carries the counts, so a
scheduler can grep it.

With `--json` you get the same facts as a JSON object:

```json
{
  "board": "gamedev",
  "promoted": ["t_a1b2c3"],
  "reclaimed": ["t_d4e5f6"],
  "recovered": [],
  "stuck": [{"task_id": "t_j1k2l3", "reason": "blocked_too_long", "detail": "..."}],
  "qa_reject_loops": [],
  "reclaim_thrash": [],
  "dispatched": [],
  "dispatch_skipped_reason": "dispatch is off by default ...",
  "provider_gate_skipped": false,
  "probe": {"healthy": true, "routes": [{"name": "minimax-oauth", "healthy": true, "detail": "HTTP 200 ('OK')"}]},
  "errors": []
}
```

## See also

- [Kanban (Multi-Agent Board)](./kanban) — the board, the dispatcher, and the CLI
- [Kanban multi-gateway](./kanban-multi-gateway) — running one board across several gateways
