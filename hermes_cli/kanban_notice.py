"""Shared cause-naming for the Kanban breaker-trip (``gave_up``) notification.

The gateway (``gateway/kanban_watchers_notifier.py``) and the TUI
(``tui_gateway/session_notifications.py``) BOTH render the ``gave_up`` event,
and they must agree: a fix in only one of them is a silent half-fix — the same
half-fix shape ``t_f4c4ba10`` had to clean up for ``protocol_violation``.

Why a shared helper at all: the copy used to read the payload key ``failures``
— the UNIFIED ``consecutive_failures`` counter — and announce "it failed N
times in a row". That counter does NOT tick for a protocol-violation trip:
``hermes_cli/kanban_db_dispatch.py::_account_crashes`` deliberately skips
``_record_task_failure`` for below-budget violations ("must not consume the
unified budget"), and the trip itself is forced (``force_trip=True``), so the
counter still holds whatever it held before. A task that violated the protocol
three times in a row was therefore announced as::

    ✖ ... is now blocked: it failed 1 times in a row

That is a confident statement about a number that does not describe the
event — the same class of bug as ``t_94a810ea``'s "max_runtime=0s" — and it
is grammatically broken ("1 times"). Measured on the live ``gamedev`` board:
1499 of 3203 ``gave_up`` events carry ``trigger_outcome == "crashed"`` with
``failures == 1``, i.e. 47% of every auto-block announcement named a count
that did not describe its own cause.

The rule this module encodes, in priority order:

1. a CAUSE-SPECIFIC key names the cause (``protocol_violations``);
2. otherwise the TRIGGER OUTCOME names it (``spawn_failed`` / ``crashed`` /
   ``timed_out`` / ``reclaimed``);
3. a LEGACY payload carrying neither key predates both fields, so the
   unified counter IS what tripped the breaker and is quoted as a plain
   count — never as a cause-specific claim;
4. no usable key at all -> NO cause and NO count. Never invent a confident one.

Rule 3 is safe by construction, not by assumption: ``trigger_outcome`` has
been in the ``gave_up`` payload since v0.13.0 (2026-05-07) and the
protocol-violation trip (the only ``force_trip`` caller, and the only
producer of ``protocol_violations``) only exists since v0.19.0 (2026-07-20).
A payload with NEITHER key therefore cannot be a violation trip, and the
unified counter is exactly what the breaker counted.
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = ["gave_up_cause", "times_phrase"]


# The trigger outcome is in the payload, so naming it is always truthful.
# Each phrase describes WHAT ended the task, not a guess about the cause.
_TRIGGER_CAUSES = {
    "spawn_failed": "it failed to spawn",
    "crashed": "its worker crashed",
    "timed_out": "it timed out",
    "reclaimed": "its worker was reclaimed",
}


def _positive_int(value: Any) -> Optional[int]:
    """``value`` as a positive int, or ``None`` when absent/malformed/non-positive.

    Total by construction: a notification must never raise on a malformed
    payload, and a zero/negative count is not a count.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _plural(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


def times_phrase(count: Any) -> str:
    """``"1 time"`` / ``"3 times"`` — the count with correct grammar.

    A notification that says "it failed 1 times in a row" reads as machine
    output; the singular/plural distinction is the same fix as the count
    itself being wrong.
    """
    number = _positive_int(count)
    if number is None:
        return ""
    return f"{number} {_plural(number, 'time', 'times')}"


def _iteration_budget_cause(payload: dict) -> Optional[str]:
    """The iteration-budget cause, when the payload carries it.

    An iteration-budget exhaustion is NOT a wall-clock timeout, so naming it
    is more truthful than "it timed out" (the ``t_94a810ea`` lesson).
    """
    budget_max = _positive_int(payload.get("budget_max"))
    if budget_max is None:
        return None
    budget_used = _positive_int(payload.get("budget_used"))
    used_text = f"{budget_used}/{budget_max}" if budget_used is not None else f"?/{budget_max}"
    return f"it ran out of iteration budget ({used_text})"


def gave_up_cause(payload: Any) -> str:
    """The cause phrase for a ``gave_up`` (circuit-breaker trip) notification.

    Returns a phrase naming the REAL cause, or a cause-free phrase when the
    payload carries no cause key. Never returns a count that does not
    describe the trip.
    """
    if not isinstance(payload, dict):
        payload = {}

    # 1. The cause-specific key wins outright. A protocol-violation trip does
    #    not tick the unified counter, so the counter must not be shown here.
    streak = _positive_int(payload.get("protocol_violations"))
    if streak is not None:
        phrase = (
            f"it stopped after {streak} protocol "
            f"{_plural(streak, 'violation', 'violations')}"
        )
        limit = _positive_int(payload.get("protocol_violation_limit"))
        if limit is not None:
            phrase += f" (limit {limit})"
        return phrase

    # 2. The trigger outcome is the real cause of this trip. The unified
    #    counter is deliberately NOT quoted here: it is a consecutive-
    #    non-success counter, not a count of this cause.
    trigger = payload.get("trigger_outcome")
    if isinstance(trigger, str) and trigger.strip():
        trigger = trigger.strip()
        if trigger == "timed_out":
            # A budget exhaustion is not a wall-clock timeout — name it.
            budget = _iteration_budget_cause(payload)
            if budget is not None:
                return budget
        phrase = _TRIGGER_CAUSES.get(trigger)
        if phrase:
            return phrase
        return f"it stopped ({trigger})"

    # 3. Legacy payload: no trigger outcome AND no violation keys, so this
    #    event predates both fields and the unified counter is exactly what
    #    tripped the breaker. Quote it as a plain count.
    failures = _positive_int(payload.get("failures"))
    if failures is not None:
        return f"it failed {times_phrase(failures)} in a row"

    # 4. Nothing to go on: say so, and invent nothing.
    return "it kept failing"
