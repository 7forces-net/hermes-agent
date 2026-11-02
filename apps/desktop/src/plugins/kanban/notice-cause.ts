/**
 * Cause phrasing for the kanban terminal-event toasts.
 *
 * The gateway (`gateway/kanban_watchers_notifier.py`) and the TUI
 * (`tui_gateway/session_notifications.py`) name the REAL cause of a crash /
 * reclaim / protocol violation through the shared Python helper
 * `hermes_cli/kanban_notice.py`. This plugin is TypeScript in a SEPARATE build
 * and cannot import it — so it does NOT copy the gateway's sentences. Copying
 * them would be exactly the drift the shared helper exists to prevent: two
 * copies of every sentence, in every locale, forever.
 *
 * Instead this module derives the cause from the SAME STRUCTURED PAYLOAD KEYS
 * the Python side reads (`exit_kind`, `exit_code`, `reason`, `manual`,
 * `heartbeat_stale`, `protocol_violation`), and renders them with SHORT LOCAL
 * LABELS ("Exit code 1", "Signal 9", "Cause unknown"). The gateway phrases;
 * this labels. The vocabulary is pinned across the language boundary by
 * `completion-notify.test.ts`, so the two cannot drift apart silently.
 *
 * THE HOUSE RULE, unchanged: name a cause only when the payload carries one;
 * never invent one. A payload with no cause key yields an explicit
 * "unknown", never a plausible-looking exit code.
 */

/** The exit classification the reaper puts on the payload (`exit_kind`).
 *  Kept in lockstep with `hermes_cli/kanban_db_dispatch.py::_classify_worker_exit`.
 *  Exported so the cross-language drift pin can compare it with the Python
 *  source of truth. */
export type ExitKind = 'clean_exit' | 'nonzero_exit' | 'rate_limited' | 'signaled' | 'unknown'

export const EXIT_KINDS: readonly string[] = [
  'clean_exit',
  'nonzero_exit',
  'rate_limited',
  'signaled',
  'unknown'
]

/** Machine tokens that must never reach a human message. The Python side
 *  applies the same rule (`_EXIT_KIND_CAUSES_NO_CODE` never leaks a token). */
const MACHINE_TOKENS = /_/

/** `exit_code` as an int, or null when absent/malformed. Accepts 0 and
 *  negatives (a real code, not a missing one); rejects booleans. */
function exitInt(value: unknown): number | null {
  if (value === null || value === undefined || typeof value === 'boolean') {
    return null
  }

  const n = typeof value === 'number' ? value : Number(value)

  if (!Number.isFinite(n)) {
    return null
  }

  return Math.trunc(n)
}

/** `exit_kind` when it is one this module knows, else null. An UNRECOGNISED
 *  token is treated as no classification at all — never echoed into a message. */
function exitKind(value: unknown): ExitKind | null {
  if (typeof value !== 'string') {
    return null
  }

  const kind = value.trim()

  return EXIT_KINDS.includes(kind) ? (kind as ExitKind) : null
}

/** A short, ANSI-stripped, length-capped excerpt of raw text.
 *  Mirrors `hermes_cli/kanban_notice.py::clip_worker_output`. */
export function clipText(raw: unknown, limit = 160): string {
  if (typeof raw !== 'string' || !raw.trim()) {
    return ''
  }

  // eslint-disable-next-line no-control-regex
  const clean = raw.replace(/\u001b\[[0-9;]*[A-Za-z]/g, '')
  const flat = clean.split(/\s+/).join(' ').trim()

  if (flat.length > limit) {
    return `${flat.slice(0, limit - 1).trimEnd()}…`
  }

  return flat
}

/** The crash cause, as a short LABEL — never a sentence, never a token.
 *
 *  Returns null when the payload carries no cause at all; the caller renders
 *  the explicit "unknown" wording in that case. */
export function crashedCauseLabel(
  payload: Record<string, unknown> | null | undefined,
  t: (key: string, ...args: unknown[]) => string
): string | null {
  const kind = exitKind(payload?.exit_kind)
  const code = exitInt(payload?.exit_code)

  if (!kind) {
    return null
  }

  switch (kind) {
    case 'nonzero_exit':
      return code === null ? t('notify.causeExitedAbnormally') : t('notify.causeExitCode', code)

    case 'signaled':
      return code === null ? t('notify.causeKilledBySignal') : t('notify.causeSignal', code)

    case 'clean_exit':
      return t('notify.causeExitedWithoutResult')

    case 'rate_limited':
      return t('notify.causeQuotaWall')

    default:
      return t('notify.causeUnknown')
  }
}

/** The reclaim cause, as a short LABEL plus the payload's own operator text.
 *
 *  The reclaim `reason` is free-form watchdog text ("watchdog: hard reclaim of
 *  stuck task (running 2.1h with no completion)"). It is the operator-facing
 *  cause, so it is shown — but CLIPPED to one line, because it can be long and
 *  it can carry a lock. It is never re-worded. */
export function reclaimedCauseLabel(
  payload: Record<string, unknown> | null | undefined,
  t: (key: string, ...args: unknown[]) => string
): string {
  const reason = typeof payload?.reason === 'string' ? payload.reason.trim() : ''

  if (reason) {
    return t('notify.causeReclaimed', clipText(reason, 120))
  }

  if (payload?.heartbeat_stale) {
    return t('notify.causeReclaimedStale')
  }

  if (payload?.manual) {
    return t('notify.causeReclaimedManual')
  }

  return t('notify.causeReclaimedPlain')
}

/** The protocol-violation cause: the worker exited WITHOUT reporting a result.
 *  That IS the cause — the payload carries it as `exit_kind=clean_exit` /
 *  `protocol_violation: true`. The violation streak rides on the `gave_up`
 *  event that trips the budget, not on this one, so it is read when present. */
export function protocolViolationCauseLabel(
  payload: Record<string, unknown> | null | undefined,
  t: (key: string, ...args: unknown[]) => string
): string {
  const streak = exitInt(payload?.protocol_violations)

  if (streak !== null && streak > 1) {
    return t('notify.causeProtocolViolations', streak)
  }

  return t('notify.causeExitedWithoutResult')
}

/** The timeout cause.
 *
 *  An iteration-budget exhaustion is NOT a wall-clock timeout, so it must not
 *  be described as one — the `t_94a810ea` lesson. The enforced wall-clock
 *  limit is a real, non-zero runtime. When neither key is present the cause
 *  is simply unknown; saying "its time limit" would name a limit that may
 *  not exist.
 *
 *  The precedence mirrors `gateway/kanban_watchers_notifier.py::_fmt_timed_out`
 *  exactly: the budget key first, then the legacy error text (events written
 *  before the producer carried the keys), then the wall-clock limit, then
 *  unknown. */
const BUDGET_EXHAUSTED_RE = /iteration budget exhausted\s*\(\s*(\d+)\s*\/\s*(\d+)\s*\)/i

export function timedOutCauseLabel(
  payload: Record<string, unknown> | null | undefined,
  t: (key: string, ...args: unknown[]) => string
): string | null {
  const budgetMax = exitInt(payload?.budget_max)

  if (budgetMax !== null && budgetMax > 0) {
    const budgetUsed = exitInt(payload?.budget_used)
    const used = budgetUsed !== null && budgetUsed > 0 ? budgetUsed : '?'

    return t('notify.causeIterationBudget', `${used}/${budgetMax}`)
  }

  // Legacy events name the budget only in their error text.
  const legacy = BUDGET_EXHAUSTED_RE.exec(String(payload?.error ?? ''))

  if (legacy) {
    return t('notify.causeIterationBudget', `${legacy[1]}/${legacy[2]}`)
  }

  const limit = exitInt(payload?.limit_seconds)

  if (limit !== null && limit > 0) {
    const minutes = Math.max(1, Math.round(limit / 60))

    return t('notify.causeTimeLimit', minutes)
  }

  return null
}

/** The actionable provider hint the PRODUCER carries on a quota-wall event.
 *
 *  A provider quota wall has exactly one remedy — add credits to the provider
 *  that is out of credits, or switch providers — and the provider states it
 *  verbatim in the worker's log:
 *
 *      Add credits with Custom:Minimax Oauth Fallback.
 *      Or switch providers with /model <model> --provider <provider>.
 *
 *  The renderers cannot read the worker's log, so the producer extracts the
 *  remedy and puts it on the event payload as `provider_hint`
 *  (`hermes_cli/kanban_db_dispatch.py`). Before that, the payload carried
 *  NOTHING extractable — measured on the live board, 0 of 1682
 *  `rate_limited` payloads carried `worker_output` — so this plugin could
 *  never show the remedy, and the operator had to guess which provider was
 *  out of credits.
 *
 *  There is deliberately NO `worker_output` fallback here: for a quota wall
 *  the producer never carried raw output either (0 of 1682), so there is no
 *  older payload to recover a hint from. Mirroring the Python extraction
 *  rule across the language boundary would duplicate a sentence-splitting
 *  rule in two languages — the exact drift the shared Python helper exists
 *  to prevent — for no payload that exists.
 *
 *  The text is provider-authored, so it is shown verbatim (clipped and
 *  ANSI-stripped), never re-worded. */
export function providerHint(payload: Record<string, unknown> | null | undefined): string {
  const raw = payload?.provider_hint

  if (typeof raw !== 'string' || !raw.trim()) {
    return ''
  }

  return clipText(raw, 200)
}

/** Guard: a rendered cause must never leak a machine token (`nonzero_exit`)
 *  or a pid/claimer. Used by the tests and cheap enough to keep in the
 *  production path as a last-resort scrub. */
export function leaksMachineToken(text: string): boolean {
  return MACHINE_TOKENS.test(text)
}
