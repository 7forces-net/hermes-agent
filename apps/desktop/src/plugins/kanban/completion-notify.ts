/**
 * Native kanban terminal-event notification (completion, blocker, failure).
 *
 * No maintained exact-fit OSS exists and the SDK
 * has no kanban event door, so this module rides the kanban plugin's EXISTING
 * /events socket (api.ts onEventsFrame). No new WebSocket, no new process,
 * no DB, no auth, no persistence — cursor is an in-memory per-board high-water
 * mark. Notifies on the same terminal kinds the gateway watcher pings
 * (gateway/kanban_watchers.py): 'completed' (kanban_db.complete_task —
 * payload: summary + artifacts), 'blocked' (payload: reason), 'gave_up'
 * (payload: error), 'crashed', 'timed_out', and 'block_loop_detected'
 * (payload: reason — the routed-to-triage orchestration handoff).
 *
 * Two delivery doors, complementary by design:
 *  - `host.notify` — the in-app toast, covers the foreground case;
 *  - `ctx.os.notify` (when bound) — the native OS notification, which the
 *    desktop shell fires only while the user is AWAY from Hermes. This is the
 *    door that covers "walked away and the worker hit a blocker".
 *
 * Cursor contract: first observation of a board baselines
 * seen[board] = GET /board latest_event_id (MAX task_events.id for that
 * board). Events id <= seen are historical/replay — never notified, no
 * cursor change. id > seen advances cursor for EVERY kind; only terminal
 * kinds emit. Reconnect replays from 0; cursor filters. Board switch never
 * mixes cursors; returning reuses prior cursor (never reset to current MAX).
 * Fail-closed: while a board's baseline is unknown, no event can be
 * classified so none is notified. Empty slug ('') suppressed.
 */

import { host, type PluginOs, type PluginRestOptions, type PluginTranslate } from '@hermes/plugin-sdk'

import { en } from './i18n'
import {
  clipText,
  crashedCauseLabel,
  protocolViolationCauseLabel,
  reclaimedCauseLabel,
  timedOutCauseLabel
} from './notice-cause'

type Rest = <T>(path: string, opts?: PluginRestOptions) => Promise<T>

export interface CompletionEvent {
  id?: unknown
  task_id?: string
  kind?: string
  payload?: Record<string, unknown> | null
}

type ToastKind = 'error' | 'success' | 'warning'

/** Terminal kinds → toast severity + i18n title key. Mirrors the gateway
 *  watcher's ping set (gateway/kanban_watchers.py) minus the intentionally
 *  silent kinds (status/archived/unblocked, which only advance the cursor).
 *
 *  ``protocol_violation`` (the worker finished but never reported a result)
 *  and ``reclaimed`` (the dispatcher's watchdog took a stale task back) are
 *  BOTH here now: they were missing, so a task that hung and was reclaimed,
 *  or a worker that exited cleanly without a verdict, produced NO desktop
 *  toast at all — the same silent-stall shape the gateway/TUI renderers had.
 *  Adding a kind is safe against a notification storm: the first observation
 *  of a board baselines the cursor at MAX(task_events.id), so history is never
 *  replayed. */
const TERMINAL_NOTIFY = new Map<string, { titleKey: string; toast: ToastKind }>([
  ['blocked', { titleKey: 'notify.blockedTitle', toast: 'warning' }],
  ['block_loop_detected', { titleKey: 'notify.blockLoopTitle', toast: 'warning' }],
  ['completed', { titleKey: 'notify.completedTitle', toast: 'success' }],
  ['crashed', { titleKey: 'notify.crashedTitle', toast: 'error' }],
  ['gave_up', { titleKey: 'notify.gaveUpTitle', toast: 'error' }],
  ['protocol_violation', { titleKey: 'notify.protocolViolationTitle', toast: 'error' }],
  ['rate_limited', { titleKey: 'notify.rateLimitedTitle', toast: 'warning' }],
  ['reclaimed', { titleKey: 'notify.reclaimedTitle', toast: 'warning' }],
  ['timed_out', { titleKey: 'notify.timedOutTitle', toast: 'warning' }]
])

const seenEventIdByBoard = new Map<string, number>()
const baselinePending = new Set<string>()

/* The quota-wall episode filter (client side).
 *
 * `rate_limited` is not a one-shot transition: during a provider quota window
 * the dispatcher requeues the card, it re-spawns, it hits the SAME wall and
 * books another `rate_limited` event — every ~360 s. Measured on the live
 * board: 1682 `rate_limited` events against 2 `crashed` events. Toasting
 * every one of them would be 1682 toasts for ONE condition with ONE remedy
 * ("add credits").
 *
 * The Python side suppresses the continuation events at CLAIM time
 * (`kanban_db_notify._filter_rate_limited_episodes`), so the gateway and the
 * TUI never see them. This plugin polls `/events` directly and does NOT go
 * through that claim path, so it needs its own copy of the rule.
 *
 * The rule: a `rate_limited` event is an episode START unless the previous
 * quota-relevant event for the same task was itself a quota wall (or the
 * reclaim the cooldown produces). Measured on the live board this is 12
 * episode starts out of 1680 events. */
const QUOTA_EPISODE_KINDS = new Set(['rate_limited', 'reclaimed'])
const lastQuotaEventKindByTask = new Map<string, string>()

/** True when this `rate_limited` event begins a NEW quota-wall episode. */
function isRateLimitedEpisodeStart(taskId: string): boolean {
  const previous = lastQuotaEventKindByTask.get(taskId)
  const isStart = previous === undefined || !QUOTA_EPISODE_KINDS.has(previous)
  lastQuotaEventKindByTask.set(taskId, 'rate_limited')

  return isStart
}

/** Record a non-quota event's kind, so the next quota event sees it.
 *
 *  ONLY a TERMINAL outcome (a crash, a completion, a timeout) ends the
 *  episode. The `/events` WebSocket streams EVERY kind — including the
 *  bookkeeping ones (`spawned`, `claimed`, `heartbeat`, `respawn_guarded`),
 *  and the dispatcher emits a `spawned`/`claimed` pair for EVERY retry of a
 *  still-standing wall. So a rule that reset on ANY non-quota event would
 *  reset on every retry and toast the wall every time.
 *
 *  Measured on the live board over the 17.8 h dead-quota window (1682
 *  `rate_limited` events): resetting on ANY event yields 1682 toasts;
 *  resetting only on terminal outcomes yields 14 — the same 14 episode
 *  starts the Python side computes from the run history. */
function rememberQuotaContext(taskId: string, kind: string): void {
  if (QUOTA_EPISODE_KINDS.has(kind)) {
    lastQuotaEventKindByTask.set(taskId, kind)

    return
  }

  // A terminal outcome (a crash, a completion, a timeout) ENDS the episode.
  // Bookkeeping kinds (spawned/claimed/heartbeat/...) do NOT.
  if (TERMINAL_NOTIFY.has(kind)) {
    lastQuotaEventKindByTask.delete(taskId)
  }
}

let rest: Rest | null = null
let translate: PluginTranslate | null = null
let osDoor: PluginOs | null = null

/** Resolve a dot-path against the plugin's own English bundle — the same
 *  last-rung fallback the plugin i18n registry applies, usable before (or
 *  without) a bound translator. */
function fallbackT(key: string, ...args: unknown[]): string {
  let node: unknown = en

  for (const part of key.split('.')) {
    node = (node as Record<string, unknown> | undefined)?.[part]
  }

  if (typeof node === 'function') {
    return (node as (...a: unknown[]) => string)(...args)
  }

  return typeof node === 'string' ? node : key
}

function t(key: string, ...args: unknown[]): string {
  const translated = translate?.(key, ...args)

  // The registry returns the raw key when the bundle isn't registered yet.
  return translated && translated !== key ? translated : fallbackT(key, ...args)
}

export function bindCompletionNotify(r: Rest, pluginTranslate?: PluginTranslate, os?: PluginOs): void {
  rest = r
  translate = pluginTranslate ?? null
  osDoor = os ?? null
}

async function ensureBaseline(slug: string): Promise<void> {
  if (seenEventIdByBoard.has(slug) || baselinePending.has(slug)) {
    return
  }

  baselinePending.add(slug)

  try {
    const board = (await rest!<{ latest_event_id?: unknown }>(`/board?board=${encodeURIComponent(slug)}`)) as {
      latest_event_id?: unknown
    }

    seenEventIdByBoard.set(slug, typeof board.latest_event_id === 'number' ? board.latest_event_id : 0)
  } catch {
    // Fail-closed: unknown baseline → notifications stay suppressed.
  } finally {
    baselinePending.delete(slug)
  }
}

function trimmed(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

/** The human handoff carried in the event payload, per kind (mirrors the
 *  payload contract the gateway watcher reads). `gave_up` deliberately has no
 *  payload body: its `error` is raw worker text, which belongs in the toast
 *  `detail` (see rawErrorFor), and the body is the plain-words i18n hint.
 *
 *  The FAILURE kinds carry a CAUSE instead: the toast names what actually
 *  ended the task, read from the same structured payload keys the gateway
 *  reads (`exit_kind`/`exit_code`, the reclaim `reason`, the protocol-violation
 *  marker). A cause is named ONLY when the payload carries one — with no cause
 *  key the toast says the cause is UNKNOWN rather than inventing an exit code
 *  (the `t_94a810ea` lesson). */
function bodyFor(kind: string, ev: CompletionEvent): string {
  const payload = ev.payload

  if (kind === 'completed') {
    return trimmed(payload?.summary)
  }

  if (kind === 'blocked' || kind === 'block_loop_detected') {
    return trimmed(payload?.reason)
  }

  if (kind === 'gave_up') {
    return t('notify.gaveUpBody')
  }

  if (kind === 'crashed') {
    return crashedCauseLabel(payload, t) ?? t('notify.causeUnknown')
  }

  if (kind === 'protocol_violation') {
    return protocolViolationCauseLabel(payload, t)
  }

  if (kind === 'reclaimed') {
    return reclaimedCauseLabel(payload, t)
  }

  if (kind === 'rate_limited') {
    // A quota wall is NOT a failure and NOT a crash: the dispatcher requeues
    // the card WITHOUT counting a failure, so the breaker does not trip and
    // the task is retried. The body says exactly that, plus the operator's
    // call to action (add credits / switch provider).
    return t('notify.rateLimitedBody')
  }

  if (kind === 'timed_out') {
    return timedOutCauseLabel(payload, t) ?? t('notify.causeUnknown')
  }

  return ''
}

/** Raw machine text that must never be the toast body — surfaced muted in `detail`.
 *
 *  `worker_output` is the worker's own last words: RAW worker output with ANSI
 *  escape codes, thousands of characters long. It never appears verbatim — not
 *  in the body, not in the detail: only a short ANSI-stripped, clipped excerpt
 *  does (the precedent `clip_worker_output` sets on the Python side). `pid` and
 *  `claimer` stay hidden — a pid is noise to a human reader. */
function rawErrorFor(kind: string, ev: CompletionEvent): string {
  if (kind === 'gave_up') {
    return trimmed(ev.payload?.error)
  }

  if (kind === 'crashed') {
    return clipText(ev.payload?.worker_output, 120)
  }

  return ''
}

function notifyOne(kind: string, spec: { titleKey: string; toast: ToastKind }, ev: CompletionEvent): void {
  const taskId = (ev.task_id ?? '').trim()
  const body = bodyFor(kind, ev)

  const artifacts =
    kind === 'completed' && Array.isArray(ev.payload?.artifacts)
      ? (ev.payload!.artifacts as unknown[])
          .filter((a): a is string => typeof a === 'string' && a.trim().length > 0)
          .map(a => a.trim())
      : []

  const artifactText =
    artifacts.length === 1
      ? artifacts[0].split(/[\\/]/).pop() || artifacts[0]
      : artifacts.length > 1
        ? t('notify.artifacts', artifacts.length)
        : ''

  const detail = [taskId, artifactText, rawErrorFor(kind, ev)].filter(Boolean).join(' · ')
  const title = t(spec.titleKey)
  const message = body || taskId || title
  host.notify({
    kind: spec.toast,
    title,
    message,
    ...(detail ? { detail } : {}),
    action: { label: t('notify.openKanban'), onClick: () => host.navigate('/kanban') }
  })

  // Native OS notification — the desktop shell fires it only while the user
  // is away from Hermes (the toast above covers the foreground case). Isolated:
  // a missing/broken shell must not mark the toast as unfired.
  try {
    osDoor?.notify({ title, body: [message, detail].filter(Boolean).join('\n') })
  } catch {
    /* swallowed */
  }
}

/** Consume one /events frame for a board. Returns true when a terminal-event
 *  notification was fired. Never throws: notification failure cannot
 *  interfere with api.ts cache invalidation. */
export async function onKanbanEventsFrame(slug: string, events?: CompletionEvent[]): Promise<boolean> {
  if (!events?.length || slug === '' || !rest) {
    return false
  }

  await ensureBaseline(slug)
  const seen = seenEventIdByBoard.get(slug)

  if (seen === undefined) {
    return false
  } // fail-closed

  let fired = false
  let cursor = seen

  for (const ev of events) {
    if (typeof ev.id !== 'number' || ev.id <= cursor) {
      continue
    }

    cursor = ev.id
    seenEventIdByBoard.set(slug, cursor)
    const spec = TERMINAL_NOTIFY.get(ev.kind ?? '')
    const kind = ev.kind ?? ''
    const taskId = (ev.task_id ?? '').trim()

    // The quota-wall episode filter: only the FIRST event of an episode is
    // reported. The continuation events of a still-standing provider quota
    // wall are the same wall, retried — one wall, one toast. Without this the
    // desktop would toast 1682 times for the 17.8 h dead-quota window.
    if (kind === 'rate_limited' && !isRateLimitedEpisodeStart(taskId)) {
      continue
    }

    if (kind !== 'rate_limited') {
      rememberQuotaContext(taskId, kind)
    }

    if (spec) {
      try {
        notifyOne(ev.kind!, spec, ev)
        fired = true
      } catch {
        /* swallowed */
      }
    }
  }

  return fired
}
