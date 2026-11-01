/**
 * END-TO-END: a REAL event row from the live board, rendered by the desktop
 * notification path.
 *
 * The unit tests use synthetic payloads. This one takes the ACTUAL
 * `task_events` rows from the live `gamedev` board (the payload shape the
 * producer really writes) and runs them through the real module, to prove
 * the desktop renders what the board actually carries — not what the test
 * author imagined.
 *
 * The live rows are INLINED here (measured from the live board at
 * 2026-11-01), so the test does not depend on a board being present:
 *
 *   gamedev crashed  (3387 rows): {pid, claimer, worker_output?, retry_status}
 *                              — NO exit_kind/exit_code on ANY row.
 *   gamedev reclaimed (387 rows): {reason, manual, prev_pid, prev_lock, ...}
 *   gamedev protocol_violation (0 rows): the producer would carry
 *                              {exit_kind: clean_exit, exit_code: 0,
 *                               protocol_violation: true}.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

const { hostMock } = vi.hoisted(() => ({
  hostMock: { notify: vi.fn(), navigate: vi.fn() }
}))

vi.mock('@hermes/plugin-sdk', () => ({
  host: hostMock,
  usePluginI18n: () => (key: string) => key
}))

type Rest = <T>(path: string) => Promise<T>

interface Mod {
  bindCompletionNotify(r: Rest, t?: (key: string, ...args: unknown[]) => string): void
  onKanbanEventsFrame(slug: string, events?: unknown[]): Promise<boolean>
}

async function loadModule(): Promise<Mod> {
  vi.resetModules()

  return import('./completion-notify')
}

const makeRest = (latest: number) =>
  vi.fn(async (path: string) => {
    if (path.startsWith('/board')) {
      return { latest_event_id: latest }
    }

    throw new Error(`unexpected rest call: ${path}`)
  })

const lastToast = () => hostMock.notify.mock.calls[hostMock.notify.mock.calls.length - 1][0] as {
  title: string
  message: string
  detail?: string
}

/* -------------------------------------------------------------------------
 * REAL payloads, measured from the live boards.
 * ---------------------------------------------------------------------- */

// A real `crashed` row from gamedev (verbatim shape, worker_output truncated
// to the first 40 chars of the real thing — the real value is 400+ chars of
// ANSI). It carries NO cause key — because 0 of 3387 real rows do.
const REAL_CRASHED = {
  pid: 572,
  claimer: 'DESKTOP-7G7SGH3:11940',
  worker_output:
    ' out after \u001b[0m\u001b[38;2;255;248;220m \u001b[0m\u001b[38;2;255;215;0m \u001b[0m \u001b[38;2;255;215;0m\u001b[0m',
  retry_status: 'ready'
}

// A real `reclaimed` row from gamedev — it DOES carry the cause.
const REAL_RECLAIMED = {
  manual: true,
  reason: 'watchdog: hard reclaim of stuck task (running 2.1h with no completion)',
  prev_lock: 'DESKTOP-7G7SGH3:10832',
  retry_status: 'ready',
  prev_pid: 4856,
  host_local: true,
  termination_attempted: true,
  terminated: false,
  sigkill: false
}

// The `protocol_violation` payload the producer WOULD write
// (`_classify_dead_worker_exit` → clean_exit branch).
const PRODUCER_PROTOCOL_VIOLATION = {
  pid: 4242,
  claimer: 'DESKTOP-7G7SGH3:14236',
  exit_code: 0,
  exit_kind: 'clean_exit',
  protocol_violation: true
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('real live-board event rows', () => {
  it('a REAL gamedev crashed row names the cause as unknown — it carries none', async () => {
    const m = await loadModule()
    m.bindCompletionNotify(makeRest(100) as never)

    const fired = await m.onKanbanEventsFrame('gamedev', [
      { id: 101, task_id: 't_real', kind: 'crashed', payload: REAL_CRASHED }
    ])

    expect(fired).toBe(true)
    expect(lastToast().message).toBe('Cause unknown')
    // The pid/claimer/worker output never surface.
    expect(lastToast().message).not.toContain('572')
    expect(lastToast().detail ?? '').not.toContain('DESKTOP-7G7SGH3')
  })

  it('a REAL gamedev reclaimed row names the reclaim AND its reason', async () => {
    const m = await loadModule()
    m.bindCompletionNotify(makeRest(100) as never)

    const fired = await m.onKanbanEventsFrame('gamedev', [
      { id: 101, task_id: 't_real', kind: 'reclaimed', payload: REAL_RECLAIMED }
    ])

    expect(fired).toBe(true)
    expect(lastToast().message).toBe(
      'Reclaimed: watchdog: hard reclaim of stuck task (running 2.1h with no completion)'
    )
    expect(lastToast().detail ?? '').not.toContain('4856')
    expect(lastToast().detail ?? '').not.toContain('DESKTOP-7G7SGH3')
  })

  it('a producer-written protocol_violation row names the real cause', async () => {
    const m = await loadModule()
    m.bindCompletionNotify(makeRest(100) as never)

    const fired = await m.onKanbanEventsFrame('gamedev', [
      { id: 101, task_id: 't_real', kind: 'protocol_violation', payload: PRODUCER_PROTOCOL_VIOLATION }
    ])

    expect(fired).toBe(true)
    expect(lastToast().message).toBe('Exited without reporting a result')
    expect(lastToast().detail ?? '').not.toContain('4242')
  })

  it('the three kinds the card named all fire — before the fix, two of them fired NOTHING', async () => {
    const m = await loadModule()
    m.bindCompletionNotify(makeRest(100) as never)

    await m.onKanbanEventsFrame('gamedev', [
      { id: 101, task_id: 't1', kind: 'crashed', payload: REAL_CRASHED },
      { id: 102, task_id: 't2', kind: 'protocol_violation', payload: PRODUCER_PROTOCOL_VIOLATION },
      { id: 103, task_id: 't3', kind: 'reclaimed', payload: REAL_RECLAIMED }
    ])

    expect(hostMock.notify).toHaveBeenCalledTimes(3)
  })
})
