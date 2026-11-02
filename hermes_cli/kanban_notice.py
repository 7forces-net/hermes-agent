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
    "rate_limited_cause",
    "rate_limited_detail",
    "rate_limited_hint",
    "RATE_LIMITED_CAUSE",
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


# ---------------------------------------------------------------------------
# The quota wall (``rate_limited``)
# ---------------------------------------------------------------------------
#
# ``rate_limited`` is a FIRST-CLASS event kind the board emits:
#
#   hermes_cli/kanban_db_dispatch.py::_classify_dead_worker_exit
#       if kind == "rate_limited":
#           # Quota wall — NOT a task failure. Release to the source phase and do
#           # NOT count a failure so a long quota window can't trip the breaker.
#           return _DeadWorker(kind, code, ..., "rate_limited", ..., rate_limited=True)
#
# and NEITHER renderer had a formatter for it — the gateway's
# ``_EVENT_FORMATTERS`` and the TUI's ``_KANBAN_EVENT_FORMATTERS`` both lacked
# the kind entirely, and it was in no subscriber's kind list either. So a
# provider quota wall was announced with CRASH-SHAPED copy by the one renderer
# that could name it (``crashed_cause``), and by NOTHING at all by the other.
#
# The irony that proves the gap: the gateway's own ``_fmt_crashed`` docstring
# documents the distinction ("``rate_limited`` -> a quota wall, which is NOT a
# failure at all") and ``crashed_cause`` can name it — but that path only
# fires for a ``crashed`` event, and a rate-limited worker produces a
# ``rate_limited`` event, NEVER a ``crashed`` one. The code knew the
# distinction and no renderer ever used it.
#
# WHY THE COPY MUST NAME IT A QUOTA WALL AND NOT A FAILURE:
#
# The board's ACCOUNTING is already correct — a rate-limited requeue does not
# touch ``consecutive_failures``, the breaker does not trip, and the cooldown
# holds. Measured on the live ``gamedev`` board during the 17.8 h dead-quota
# window (2026-11-01 07:13:42 -> 2026-11-02 00:59:24): 1682 ``rate_limited``
# events against only 2 ``crashed`` events — a 838:1 ratio. Any reporter that
# collapses the two kinds describes the outage backwards.
#
# So the message must say, truthfully:
#
#   * the cause is a QUOTA WALL / provider rate limit, not a crash and not a
#     worker failure;
#   * the card was REQUEUED and WILL BE RETRIED, and it did NOT count against
#     the failure budget (that is what actually happened, and it is what makes
#     the board's behaviour trustworthy);
#   * the ACTIONABLE provider hint, when the payload carries one.
#
# The wording lives here — in the NEUTRAL module neither renderer owns — so the
# gateway and the TUI cannot drift apart, the same pattern ``t_fd034683`` used
# for ``crashed_cause``/``reclaimed_cause``.

# The cause clause. Shared verbatim by both renderers so the copy is
# byte-identical for the same payload (asserted by a test).
RATE_LIMITED_CAUSE = "its worker hit a provider quota wall (rate limited)"

# The provider's own actionable hint, when the payload carries one. The worker
# prints e.g. "Add credits with Custom:Minimax Oauth Fallback. Or switch
# providers with /model <model> --provider <provider>." — the text that tells
# the operator what to actually DO. It is only useful if it survives clipping.
#
# NOTE (measured 2026-11-02): the live board's 1682 ``rate_limited`` payloads
# carry NO ``worker_output`` — the producer skips it for rate-limited runs on
# purpose (``if task_id and not dead.rate_limited``). So this helper is correct
# and the hint is simply absent on those rows. It is rendered whenever a
# payload DOES carry one, which is what the acceptance criteria ask for.
# The provider's ACTIONABLE sentences. These are the calls to action the
# operator can actually take ("add credits", "switch providers"), not the
# descriptive ones ("the usage limit has been reached") — the cause clause
# already names the quota wall, so repeating that is noise.
#
# Matched as a SUBSTRING, case-insensitively, so "Add credits with X." and
# "You can switch providers temporarily with /model ..." both match.
_RATE_LIMITED_HINT_MARKERS = (
    "add credit",
    "buy credit",
    "update billing",
    "switch provider",
    "upgrade your plan",
)

# The hint is one or two sentences. A hint longer than this is a dump.
_RATE_LIMITED_HINT_LIMIT = 200

# How much prose may precede the actionable marker inside ONE chunk. The
# provider's hint is usually its own sentence, but a worker log can glue it
# to a long lead-in with no punctuation ("...1500 chars of prose Add credits
# with Foo."). Trimming the front to the marker keeps the INSTRUCTION, which
# is the whole point of the hint; without it the clip would spend the whole
# budget on prose and cut the instruction off.
_RATE_LIMITED_HINT_LEADIN = 24

# A tail-truncated worker log can cut a CSI sequence in half, leaving an
# orphaned SGR fragment ("205;127;50m") with no ESC prefix. The complete
# sequence regex above cannot match it, so it would leak into the message as
# noise. Strip those too — but only a bare parameter run that ends in "m",
# never ordinary prose ("2067", "model").
_ORPHAN_SGR_RE = re.compile(r"(?:(?<=\s)|(?<=^))\d+(?:;\d+)*m\b")


def rate_limited_hint(payload: Any) -> str:
    """The actionable provider hint from the payload, clipped; "" when absent.

    The hint is the operator's call to action ("add credits", "switch
    provider"). It is extracted from ``worker_output`` — the worker's own last
    words — because that is where the provider puts it.

    Only the ACTIONABLE sentences are returned, not the tail of the raw
    output: the raw worker output is a rendered panel full of box-drawing
    chrome and unrelated prose, so returning "the last 160 characters of it"
    would put noise in the message instead of the instruction.

    Every matching sentence is kept (the provider's hint is usually a PAIR —
    "Add credits with X. Or switch providers with /model ..." — and dropping
    half of it would drop half the instruction).
    """
    if not isinstance(payload, dict):
        return ""
    raw = payload.get("worker_output")
    if not isinstance(raw, str) or not raw.strip():
        return ""
    clean = _ANSI_RE.sub("", raw)
    clean = _ORPHAN_SGR_RE.sub(" ", clean)
    # The provider's hint is a sentence of its own. Split on sentence
    # boundaries so the useful line is not glued to the surrounding ASCII art.
    flat = " ".join(clean.split())
    parts: list[str] = []
    for chunk in re.split(r"(?<=[.!])\s+", flat):
        chunk = chunk.strip()
        if not chunk:
            continue
        low = chunk.lower()
        matched = [m for m in _RATE_LIMITED_HINT_MARKERS if m in low]
        if not matched:
            continue
        # Keep the INSTRUCTION, not the prose in front of it: start the chunk
        # at the marker so a long lead-in cannot push the actionable words
        # past the clip budget.
        start = min(low.index(m) for m in matched)
        if start > _RATE_LIMITED_HINT_LEADIN:
            chunk = chunk[start:].lstrip()
        parts.append(chunk)
    if not parts:
        return ""
    hint = " ".join(parts)
    if len(hint) > _RATE_LIMITED_HINT_LIMIT:
        hint = hint[: _RATE_LIMITED_HINT_LIMIT - 1].rstrip() + "…"
    return hint


def rate_limited_cause(payload: Any) -> str:
    """The cause clause for a ``rate_limited`` notification.

    Names the quota wall and NOTHING else. It never says "crashed", never
    names a pid or a claimer, and never claims a failure was counted.
    """
    return RATE_LIMITED_CAUSE


def rate_limited_detail(payload: Any) -> str:
    """The trailing detail clause for a ``rate_limited`` notification.

    States what actually happened to the card — requeued, will be retried,
    and NOT counted against the failure budget — plus the actionable provider
    hint when the payload carries one.

    The requeue/failure-budget clause is NOT optional: it is the part that
    distinguishes a quota wall from a failure, and the part that makes the
    board's behaviour (do not trip the breaker on a quota window) legible to
    the operator.

    The returned clause NEVER ends in a period: each renderer owns the
    punctuation that follows it, so the gateway's trailing "Logs: ..." and the
    TUI's bare line cannot double a period, and the two copies stay
    byte-identical over the shared part.
    """
    detail = "it was requeued and will be retried automatically; it does not count as a failure"
    hint = rate_limited_hint(payload)
    if hint:
        # The hint is a sentence and may already end in a period; drop it so
        # the renderer's own punctuation is the only one.
        detail += ". " + hint.rstrip(". ")
    return detail
