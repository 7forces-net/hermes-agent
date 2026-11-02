"""The producer must CARRY the actionable provider hint on a quota wall.

A provider quota wall has exactly ONE remedy — add credits to the provider
that is out of credits, or switch providers — and the provider states it
verbatim in the worker's log::

    Add credits with Custom:Minimax Oauth Fallback.
    Or switch providers with /model <model> --provider <provider>.

The renderers (gateway, TUI, desktop plugin) read that text from the event
PAYLOAD, and they CANNOT read the worker's log. So if the producer does not
carry the hint, no renderer can ever show it.

This was the defect: the producer SKIPPED ``worker_output`` for rate-limited
runs on purpose::

    if task_id and not dead.rate_limited:
        ...worker_output = <the worker's last words>...

Measured on the live ``gamedev`` board (2026-11-02): 1682 ``rate_limited``
events, and **0 of 1682** carried ``worker_output``. Every one of them
carried exactly ``{pid, claimer, exit_code, retry_status}``. The renderer
side was complete and correct — it rendered a quota wall with no remedy,
because the remedy was never in the payload.

What these tests lock in:

* a rate-limited event payload carries ``provider_hint`` with the remedy;
* the payload stays BOUNDED — the extracted hint only, never the raw
  multi-KB panel;
* the classification is untouched: a quota wall is still ``rate_limited``,
  never a crash, and the failure budget is untouched;
* the crashed / gave_up payloads are unchanged;
* the hint names the provider that is actually OUT OF CREDITS (the last one),
  not the primary that merely rate-limited and was switched away from.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_notice as notice


# The provider's panel, as the worker prints it: ANSI-coloured, box-framed,
# and followed by the CLI's exit summary.
HINT_PANEL = (
    "\x1b[1;38;2;205;127;50m \x1b[0m    Add credits with Custom:Minimax Oauth Fallback."
    "                           \x1b[38;2;205;127;50m \x1b[0m"
    "    Or switch providers with \x1b[1m/model <model> --provider <provider>\x1b[0m."
)

# The PRIMARY provider's 429 — it rate-limited and was switched away from.
PRIMARY_QUOTA = (
    "Attempt 1/3 failed: HTTP 429: You've reached your weekly (7-day) usage limit."
    " To continue now, purchase extra usage or upgrade your plan:"
    " https://www.kimi.com/membership/subscription?tab=quota"
)

CLI_SUMMARY = (
    "\nResume this session with:\n  hermes --resume 20261102_005927_4fd032 -p dispatcher\n"
    "\nSession:        20261102_005927_4fd032\nDuration:       12s\nMessages:       1 (1 user, 0 tool calls)\n"
)


@pytest.fixture()
def board(tmp_path, monkeypatch):
    """A real board DB plus the worker-log dir the producer reads.

    The log dir is resolved through the PRODUCT's own resolver
    (``kb.worker_logs_dir``) rather than hard-coded: the ``default`` board
    keeps its logs at ``<root>/kanban/logs`` while a named board keeps them
    at ``<root>/kanban/boards/<slug>/logs``, and a hard-coded path would
    silently point the test at a directory the producer never reads.
    """
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban_home"))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    # The reclaim path skips a worker that died inside the launch-window
    # grace; a test worker is "born" now, so the grace would hide it.
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb.init_db()
    conn = kbc.connect(db_path=tmp_path / "kanban.db")
    task_id = kb.create_task(conn, title="quota wall", assignee="worker")
    kb.claim_task(conn, task_id)
    logs_dir = kb.worker_logs_dir(board="default")
    logs_dir.mkdir(parents=True, exist_ok=True)
    yield conn, task_id, logs_dir
    conn.close()


def _write_log(logs_dir, task_id, text):
    (logs_dir / f"{task_id}.log").write_text(text, encoding="utf-8")


def _classify(conn, task_id, logs_dir, monkeypatch):
    """Classify a rate-limited death through the REAL producer path."""
    # The exit registry says "rate limited" (exit code 75).
    monkeypatch.setattr(
        kbd, "_recent_worker_exits",
        {4242: ((kb.KANBAN_RATE_LIMIT_EXIT_CODE & 0xFF) << 8, 0.0)},
    )
    return kbd._classify_dead_worker(
        4242, "DESKTOP-X:1", task_id=task_id, board="default", conn=conn,
    )


# ---------------------------------------------------------------------------
# 1. The payload carries the hint
# ---------------------------------------------------------------------------


def test_a_quota_wall_carries_the_actionable_provider_hint(board, monkeypatch):
    """The regression this card fixes: the payload must carry the remedy.

    Before the fix the producer skipped ``worker_output`` for rate-limited
    runs, so the payload was exactly ``{pid, claimer, exit_code}`` and the
    renderers could only say WHAT happened, never what to DO.
    """
    conn, task_id, logs_dir = board
    _write_log(logs_dir, task_id, HINT_PANEL + CLI_SUMMARY)

    dead = _classify(conn, task_id, logs_dir, monkeypatch)

    assert dead.rate_limited is True
    hint = dead.event_payload.get("provider_hint")
    assert hint, dead.event_payload
    assert "Add credits" in hint, hint
    assert "switch providers" in hint, hint
    assert "Custom:Minimax Oauth Fallback" in hint, hint


def test_the_hint_names_the_provider_that_is_out_of_credits(board, monkeypatch):
    """The LAST provider's remedy, not the primary's.

    A rate-limited attempt usually hits two providers in sequence: the
    PRIMARY returns 429 and is switched away from, then the FALLBACK is out
    of credits. The provider that needs credits is the LAST one. A
    first-match extraction names the primary — the provider that is working
    — and sends the operator to top up the wrong account.
    """
    conn, task_id, logs_dir = board
    _write_log(
        logs_dir, task_id,
        PRIMARY_QUOTA + "\n" + HINT_PANEL + CLI_SUMMARY,
    )

    dead = _classify(conn, task_id, logs_dir, monkeypatch)

    hint = dead.event_payload["provider_hint"]
    assert "Custom:Minimax Oauth Fallback" in hint, hint
    assert "kimi.com" not in hint, hint


def test_the_producer_reads_the_hint_through_the_shared_helper(board, monkeypatch):
    """The producer and the renderers use ONE extraction helper.

    Two copies of the extraction logic would drift — the same failure mode
    the shared ``rate_limited_cause``/``rate_limited_detail`` helpers exist
    to prevent.
    """
    conn, task_id, logs_dir = board
    _write_log(logs_dir, task_id, HINT_PANEL + CLI_SUMMARY)

    dead = _classify(conn, task_id, logs_dir, monkeypatch)

    raw = (logs_dir / f"{task_id}.log").read_text(encoding="utf-8")
    assert dead.event_payload["provider_hint"] == notice.extract_rate_limited_hint(raw)


# ---------------------------------------------------------------------------
# 2. The payload stays BOUNDED
# ---------------------------------------------------------------------------


def test_the_payload_does_not_carry_the_raw_multi_kb_panel(board, monkeypatch):
    """The payload is stored in the board DB forever — it must stay small.

    The raw worker log of a retried card grows without bound (measured on
    the live board: up to 1.2 MB for a card that retried 195 times). The
    payload carries the EXTRACTED hint, never the raw panel.
    """
    conn, task_id, logs_dir = board
    filler = ("\x1b[1m" + "x" * 500 + "\x1b[0m\n") * 400
    _write_log(logs_dir, task_id, filler + HINT_PANEL + CLI_SUMMARY)

    dead = _classify(conn, task_id, logs_dir, monkeypatch)

    hint = dead.event_payload["provider_hint"]
    assert len(hint) <= 200, len(hint)
    assert "x" * 500 not in hint
    # And the payload as a whole is small.
    assert len(json.dumps(dead.event_payload)) < 1024, dead.event_payload
    # The raw worker output is NOT on the payload at all.
    assert "worker_output" not in dead.event_payload


def test_the_hint_is_bounded_even_when_the_provider_rambles(board, monkeypatch):
    """A pathological provider line still produces a bounded hint."""
    conn, task_id, logs_dir = board
    _write_log(
        logs_dir, task_id,
        "Add credits with " + ("Y" * 9000) + ". Or switch providers with /model x.\n",
    )

    dead = _classify(conn, task_id, logs_dir, monkeypatch)

    hint = dead.event_payload["provider_hint"]
    assert len(hint) <= 200, len(hint)


def test_a_missing_log_yields_no_hint_and_no_crash(board, monkeypatch):
    """A quota wall with no readable log is still a quota wall."""
    conn, task_id, logs_dir = board  # no log written

    dead = _classify(conn, task_id, logs_dir, monkeypatch)

    assert dead.rate_limited is True
    assert "provider_hint" not in dead.event_payload
    assert dead.event_kind == "rate_limited"


def test_a_log_with_no_actionable_text_yields_no_hint(board, monkeypatch):
    """A log without the provider's remedy carries no hint — and says nothing."""
    conn, task_id, logs_dir = board
    _write_log(logs_dir, task_id, "the worker just stopped\n" + CLI_SUMMARY)

    dead = _classify(conn, task_id, logs_dir, monkeypatch)

    assert "provider_hint" not in dead.event_payload


# ---------------------------------------------------------------------------
# 3. NO REGRESSION: the classification and the accounting are untouched
# ---------------------------------------------------------------------------


def test_a_quota_wall_is_still_never_a_crash(board, monkeypatch):
    """The classification is NOT changed by carrying the hint.

    ``t_72abab80`` owns the classification and it is DONE: a quota wall must
    keep being ``rate_limited`` and must never be booked as a crash.
    """
    conn, task_id, logs_dir = board
    _write_log(logs_dir, task_id, HINT_PANEL + CLI_SUMMARY)

    dead = _classify(conn, task_id, logs_dir, monkeypatch)

    assert dead.kind == "rate_limited"
    assert dead.event_kind == "rate_limited"
    assert dead.run_outcome == "rate_limited"
    assert dead.rate_limited is True
    assert "crashed" not in dead.event_kind
    assert "crashed" not in dead.run_outcome


def test_the_exit_kind_vocabulary_is_unchanged(board, monkeypatch):
    """The producer's vocabulary is pinned across the language boundary.

    The desktop's ``notice-cause.test.ts`` compares its token set against the
    Python source, so these names must not drift.
    """
    conn, task_id, logs_dir = board
    _write_log(logs_dir, task_id, HINT_PANEL + CLI_SUMMARY)

    dead = _classify(conn, task_id, logs_dir, monkeypatch)

    assert dead.kind in ("clean_exit", "nonzero_exit", "rate_limited", "signaled", "unknown")
    # The rate-limited payload's exit keys are unchanged.
    assert dead.event_payload["exit_code"] == kb.KANBAN_RATE_LIMIT_EXIT_CODE
    assert dead.event_payload["pid"] == 4242
    assert dead.event_payload["claimer"] == "DESKTOP-X:1"


def test_a_crash_still_carries_worker_output_and_no_provider_hint(board, monkeypatch):
    """The crashed path is unchanged: it keeps ``worker_output`` and gains nothing."""
    conn, task_id, logs_dir = board
    _write_log(logs_dir, task_id, "the worker exploded\n" + CLI_SUMMARY)

    monkeypatch.setattr(kbd, "_recent_worker_exits", {4242: ((3 & 0xFF) << 8, 0.0)})
    dead = kbd._classify_dead_worker(
        4242, "DESKTOP-X:1", task_id=task_id, board="default", conn=conn,
    )

    assert dead.kind == "nonzero_exit"
    assert dead.event_kind == "crashed"
    assert "worker_output" in dead.event_payload
    assert "provider_hint" not in dead.event_payload


def test_the_reclaim_does_not_count_a_failure(board, monkeypatch):
    """Carrying the hint must not change the ACCOUNTING.

    A quota wall is released without counting a failure; that is what stops
    a long quota window from tripping the breaker. The hint rides along on
    the payload; it does not touch ``consecutive_failures``.
    """
    conn, task_id, logs_dir = board
    _write_log(logs_dir, task_id, HINT_PANEL + CLI_SUMMARY)
    conn.execute("UPDATE tasks SET worker_pid = 4242 WHERE id = ?", (task_id,))
    conn.commit()

    monkeypatch.setattr(
        kbd, "_recent_worker_exits",
        {4242: ((kb.KANBAN_RATE_LIMIT_EXIT_CODE & 0xFF) << 8, 0.0)},
    )
    sweep = kbd._reclaim_dead_workers(conn, board="default")

    assert task_id in sweep.rate_limited
    assert task_id not in sweep.crashed
    assert sweep.crash_details == []

    row = conn.execute(
        "SELECT consecutive_failures, status FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert row["consecutive_failures"] == 0, dict(row)
    assert row["status"] == "ready", dict(row)

    # And the EVENT carries the hint.
    ev = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? AND kind = 'rate_limited'",
        (task_id,),
    ).fetchone()
    assert ev is not None
    payload = json.loads(ev["payload"])
    assert "Add credits" in payload["provider_hint"], payload
    # The raw panel is NOT stored.
    assert "worker_output" not in payload, payload
    assert len(ev["payload"]) < 1024, len(ev["payload"])


def test_the_event_payload_stays_json_serialisable_and_small(board, monkeypatch):
    """The payload must round-trip through JSON (it is a DB column)."""
    conn, task_id, logs_dir = board
    _write_log(logs_dir, task_id, HINT_PANEL + CLI_SUMMARY)

    dead = _classify(conn, task_id, logs_dir, monkeypatch)

    round_tripped = json.loads(json.dumps(dead.event_payload))
    assert round_tripped["provider_hint"] == dead.event_payload["provider_hint"]
