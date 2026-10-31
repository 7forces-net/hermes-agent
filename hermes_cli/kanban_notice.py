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

import re
from typing import Any, Optional

__all__ = [
    "gave_up_cause",
    "times_phrase",
    "crashed_cause",
    "reclaimed_cause",
    "clip_worker_output",
]


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


# ---------------------------------------------------------------------------
# The ordinary crash path (``crashed``)
# ---------------------------------------------------------------------------
#
# ``hermes_cli/kanban_db_dispatch.py::_classify_dead_worker_exit`` classifies
# every dead worker and puts the classification on the event payload as
# ``exit_kind`` / ``exit_code``:
#
#   ``nonzero_exit``  -> the worker exited with a non-zero code;
#   ``signaled``      -> the worker was killed by a signal (``exit_code`` IS
#                        the signal number);
#   ``clean_exit``    -> rc=0 while the task was still ``running`` — that is
#                        a PROTOCOL VIOLATION and is booked under its own
#                        event kind, not ``crashed``;
#   ``rate_limited``  -> the quota wall; NOT a failure at all;
#   ``unknown``       -> the pid was absent from the exit registry, so the
#                        board genuinely does not know how it died.
#
# The renderers used to ignore all of it and say "its worker stopped
# unexpectedly" — the generic fallback you write when you know nothing, while
# the board knew several concrete things. Worse, the ``clean_exit`` case was
# called a random crash when it is the opposite: the worker MISBEHAVED (it
# finished but never reported a result).
#
# The rule here is the same rule as everywhere else in this module: name a
# cause only when the payload carries it, and never invent one.

# The exit classification -> the phrase a human reader should see.
# Each phrase describes HOW the worker ended, not a guess about the cause.
# ``{code}`` marks a phrase that needs a code to be complete.
_EXIT_KIND_CAUSES = {
    "nonzero_exit": "its worker exited with code {code}",
    "signaled": "its worker was killed by signal {code}",
    "clean_exit": "its worker exited cleanly without reporting a result",
    "rate_limited": "its worker hit a quota wall (rate limited), not a failure",
    "unknown": "its worker is gone; the cause is unknown",
}

# The same kinds, WITHOUT the code, for a payload whose code is missing. The
# cause is only half known then, so the phrase says the half that is true —
# and never leaks the machine token (``nonzero_exit``) into a human message.
_EXIT_KIND_CAUSES_NO_CODE = {
    "nonzero_exit": "its worker exited abnormally",
    "signaled": "its worker was killed by a signal",
}

# Legacy events (written before the producer carried ``exit_kind``) name the
# exit only in their error text: "pid 12345 exited with code 1", "pid 12345
# killed by signal 9". Parsing it lets those already-queued events render
# their real cause too — the same fallback the ``timed_out`` copy uses for
# legacy iteration-budget events.
_EXIT_CODE_RE = re.compile(r"exited with code\s+(-?\d+)", re.IGNORECASE)
_EXIT_SIGNAL_RE = re.compile(r"killed by signal\s+(\d+)", re.IGNORECASE)


def _exit_int(value: Any) -> Optional[int]:
    """``value`` as an int, or ``None`` when absent/malformed.

    Unlike ``_positive_int`` this accepts 0 and negatives: an exit code of 0
    or a negative return code is a real code, not a missing one. ``bool`` is
    rejected (``True`` is not the exit code 1).
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def crashed_cause(payload: Any) -> str:
    """The cause phrase for a ``crashed`` notification.

    Names the REAL cause when the payload carries one, and says the cause is
    unknown when it does not. Never invents an exit code and never calls a
    quota wall a crash.
    """
    if not isinstance(payload, dict):
        payload = {}

    kind = payload.get("exit_kind")
    code = _exit_int(payload.get("exit_code"))

    if isinstance(kind, str) and kind.strip():
        kind = kind.strip()
        template = _EXIT_KIND_CAUSES.get(kind)
        if template is not None:
            # A cause that names a code MUST have one. A missing code means
            # the cause is only half known, so say the half that is true
            # rather than printing "code None" — and never leak the machine
            # token into a human message.
            if "{code}" in template:
                if code is None:
                    return _EXIT_KIND_CAUSES_NO_CODE.get(
                        kind, "its worker exited abnormally")
                return template.format(code=code)
            return template

    # Legacy payload: no ``exit_kind``. The error text may still name the
    # exit, so read it before falling back to "unknown".
    error = str(payload.get("error") or "")
    match = _EXIT_CODE_RE.search(error)
    if match:
        return f"its worker exited with code {int(match.group(1))}"
    match = _EXIT_SIGNAL_RE.search(error)
    if match:
        return f"its worker was killed by signal {int(match.group(1))}"

    # No cause key at all: the board genuinely does not know how the worker
    # died. Say that — do not imply a crash we cannot substantiate.
    return "its worker is gone; the cause is unknown"


# ---------------------------------------------------------------------------
# The stale-claim reclaim path (``reclaimed``)
# ---------------------------------------------------------------------------
#
# ``hermes_cli/kanban_db.py::release_stale_claims`` books a reclaim whose
# payload carries the stale lock, the worker pid, the claim expiry and the
# last heartbeat. The WATCHDOG path additionally carries a human-readable
# ``reason``:
#
#   "watchdog: stale running task (heartbeat age 4079s)"
#   "watchdog: hard reclaim of stuck task (running 2.1h with no completion)"
#
# The reclaim event was in NEITHER ``TERMINAL_KINDS`` (gateway) nor
# ``_KANBAN_NOTIFY_KINDS`` (TUI), so a task that hung for 2.6 hours and was
# then reclaimed produced ZERO notification — the same silent-stall shape
# ``t_f4c4ba10`` fixed for ``protocol_violation``, one event kind over.

# The reclaim reason is the operator-facing text; it can be long and can
# contain the pid, so it is clipped and never dumped raw.
_RECLAIM_REASON_LIMIT = 160


def _clip_reason(text: str, limit: int = _RECLAIM_REASON_LIMIT) -> str:
    """One-line, length-capped reason text.

    The reclaim ``reason`` is free-form operator/watchdog text. It may contain
    a pid or a lock, and it may be thousands of characters, so it is clipped
    to one line and to ``limit`` characters before it reaches a message.
    """
    flat = " ".join(str(text).split())
    if len(flat) > limit:
        return flat[: limit - 1].rstrip() + "…"
    return flat


def reclaimed_cause(payload: Any) -> str:
    """The cause phrase for a ``reclaimed`` notification.

    A reclaim means the worker's claim went stale and the dispatcher took the
    task back. The phrase ALWAYS names the reclaim (so the reader can tell
    what happened even when the payload carries no reason text) and adds the
    reclaim reason when the payload supplies one.
    """
    if not isinstance(payload, dict):
        payload = {}

    reason = payload.get("reason")
    if isinstance(reason, str) and reason.strip():
        return f"its worker was reclaimed: {_clip_reason(reason)}"

    # No reason text: describe the reclaim from the facts the payload does
    # carry. A stale heartbeat is the most specific thing available.
    if payload.get("heartbeat_stale"):
        return "its worker's heartbeat went stale, so its claim was reclaimed"
    if payload.get("manual"):
        return "its worker was reclaimed by hand"
    return "its claim expired, so its worker was reclaimed"


# ---------------------------------------------------------------------------
# Worker output
# ---------------------------------------------------------------------------

# The worker's own last words ("Provider said: HTTP 429: The usage limit has
# been reached"). It is RAW worker output: thousands of characters, full of
# ANSI escape codes. It stays in the payload and the log; a message carries
# only a short, cleaned excerpt — the precedent the ``gave_up`` copy already
# sets by clipping ``error`` to 160 chars.
_WORKER_OUTPUT_LIMIT = 160

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def clip_worker_output(payload: Any, limit: int = _WORKER_OUTPUT_LIMIT) -> str:
    """A short, ANSI-stripped excerpt of the worker's last output.

    Returns ``""`` when there is none. The excerpt is never the raw string:
    raw worker output is unreadable in a chat message and can be thousands of
    characters long.
    """
    if not isinstance(payload, dict):
        return ""
    raw = payload.get("worker_output")
    if not isinstance(raw, str) or not raw.strip():
        return ""
    clean = _ANSI_RE.sub("", raw)
    flat = " ".join(clean.split())
    if len(flat) > limit:
        return flat[: limit - 1].rstrip() + "…"
    return flat
