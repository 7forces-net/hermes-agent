/**
 * Locale parity for the kanban plugin's own bundles.
 *
 * The desktop's bundles live inline in `i18n.ts` (en/ja/zh/zh-hant) — NOT the
 * `locales/*.yaml` catalogs the Python side uses, so there is no cross-suite
 * parity test that would catch a missing key here. A missing key renders as
 * the RAW KEY at runtime ("notify.causeExitCode" appears in a user's toast),
 * so the key set is pinned here instead.
 *
 * The Python side learned this the hard way: adding a kind to the gateway's
 * `_WAKE_KINDS` needs a `gateway.kanban.wake.<kind>` key in ALL 17 catalogs or
 * `tests/agent/test_i18n.py` fails. This file is the desktop's equivalent
 * guard.
 */

import { describe, expect, it, vi } from 'vitest'

// `./i18n` imports the plugin SDK, whose barrel pulls in the whole app graph
// (react-query, icons, …). The bundles under test are plain data, so the SDK
// is stubbed out — the same mock the sibling completion-notify test uses.
vi.mock('@hermes/plugin-sdk', () => ({
  usePluginI18n: () => (key: string) => key
}))

import { KANBAN_LOCALES } from './i18n'

type Tree = Record<string, unknown>

/** Every leaf key of a message tree, as dot-paths. Function leaves count as
 *  leaves (they take args, so their presence is what matters). */
function leafKeys(tree: unknown, prefix = ''): string[] {
  if (typeof tree === 'string' || typeof tree === 'function') {
    return [prefix]
  }

  if (typeof tree !== 'object' || tree === null) {
    return []
  }

  return Object.entries(tree as Tree).flatMap(([key, value]) =>
    leafKeys(value, prefix ? `${prefix}.${key}` : key)
  )
}

const LOCALES = Object.keys(KANBAN_LOCALES)

describe('kanban plugin locale parity', () => {
  it('every locale the plugin carries is non-empty', () => {
    expect(LOCALES.length).toBeGreaterThan(1)
    expect(LOCALES).toContain('en')
  })

  it('every locale has EXACTLY the English key set — no missing, no extra', () => {
    const english = leafKeys(KANBAN_LOCALES.en).sort()

    expect(english.length).toBeGreaterThan(0)

    for (const [locale, bundle] of Object.entries(KANBAN_LOCALES)) {
      const keys = leafKeys(bundle).sort()

      const missing = english.filter(key => !keys.includes(key))
      const extra = keys.filter(key => !english.includes(key))

      expect(missing, `${locale} is missing: ${missing.join(', ')}`).toEqual([])
      expect(extra, `${locale} has keys English does not: ${extra.join(', ')}`).toEqual([])
    }
  })

  it('the cause keys exist in every locale — a missing one renders as the raw key', () => {
    const required = [
      'notify.protocolViolationTitle',
      'notify.reclaimedTitle',
      'notify.causeExitCode',
      'notify.causeExitedAbnormally',
      'notify.causeSignal',
      'notify.causeKilledBySignal',
      'notify.causeExitedWithoutResult',
      'notify.causeQuotaWall',
      'notify.causeUnknown',
      'notify.causeReclaimed',
      'notify.causeReclaimedStale',
      'notify.causeReclaimedManual',
      'notify.causeReclaimedPlain',
      'notify.causeProtocolViolations',
      'notify.causeIterationBudget',
      'notify.causeTimeLimit'
    ]

    for (const [locale, bundle] of Object.entries(KANBAN_LOCALES)) {
      const keys = leafKeys(bundle)

      for (const key of required) {
        expect(keys, `${locale} must carry ${key}`).toContain(key)
      }
    }
  })

  it('no locale leaves a cause label as the raw key', () => {
    // A function leaf renders with its args; a string leaf renders as-is.
    // Neither may BE a key path.
    for (const [locale, bundle] of Object.entries(KANBAN_LOCALES)) {
      const notify = (bundle as Tree).notify as Tree

      for (const [key, value] of Object.entries(notify)) {
        if (typeof value === 'string') {
          expect(value, `${locale}.notify.${key} is a raw key`).not.toMatch(/^notify\./)
        }
      }
    }
  })
})
