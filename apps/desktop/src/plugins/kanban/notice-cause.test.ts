/**
 * Cross-language drift pin: the desktop's exit vocabulary must track the
 * PYTHON source of truth.
 *
 * The gateway and the TUI name the real cause of a crash through the shared
 * Python helper `hermes_cli/kanban_notice.py` (`_EXIT_KIND_CAUSES`). The
 * desktop plugin is TypeScript in a separate build and cannot import it, so
 * it keeps its OWN copy of the exit-kind vocabulary — in `notice-cause.ts`.
 *
 * Two copies of a vocabulary drift. This test is the pin that stops it: it
 * reads the Python source and compares the token sets. If the producer grows
 * a new classification (`_classify_worker_exit` is the producer), the
 * desktop's copy must grow it too — and the test says so instead of letting
 * the desktop quietly fall back to "Cause unknown" for a cause the board
 * actually knows.
 *
 * The comparison is on the TOKENS, not the prose: the desktop renders short
 * local labels, the Python side renders sentences. The vocabulary — which
 * classifications exist, and which of them need a code — is what must match.
 */

import { readFileSync } from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import { EXIT_KINDS } from './notice-cause'

/** The repo root, from this file: apps/desktop/src/plugins/kanban/<here>.
 *  The desktop is a workspace of the monorepo, so the Python source of truth
 *  is always a sibling of the app — that is what makes this pin possible. */
const REPO_ROOT = path.resolve(import.meta.dirname, '../../../../..')

const PY_NOTICE = path.join(REPO_ROOT, 'hermes_cli', 'kanban_notice.py')
const PY_DISPATCH = path.join(REPO_ROOT, 'hermes_cli', 'kanban_db_dispatch.py')

function readPython(file: string): string {
  return readFileSync(file, 'utf8')
}

/** The body of a Python dict literal block, e.g. `_EXIT_KIND_CAUSES = { ... }`.
 *  Brace-matched, so a value containing `{code}` does not end the block early. */
function dictBlock(source: string, name: string): string {
  const start = source.indexOf(`${name} = {`)
  expect(start, `${name} should exist in the Python source`).toBeGreaterThan(-1)

  const open = source.indexOf('{', start)
  let depth = 0
  let close = open

  for (; close < source.length; close++) {
    if (source[close] === '{') {
      depth++
    } else if (source[close] === '}') {
      depth--

      if (depth === 0) {
        break
      }
    }
  }

  return source.slice(open + 1, close)
}

/** The keys of a Python dict literal block. */
function dictKeys(source: string, name: string): string[] {
  return [...dictBlock(source, name).matchAll(/"([a-z_]+)":/g)].map(m => m[1])
}

/** The keys whose value carries a `{code}` placeholder — i.e. the causes that
 *  are only complete when the payload carries a code. */
function keysNeedingCode(source: string, name: string): string[] {
  const block = dictBlock(source, name)

  return [...block.matchAll(/"([a-z_]+)":\s*"([^"]*\{code\}[^"]*)"/g)].map(m => m[1])
}

describe('exit-kind vocabulary (cross-language pin)', () => {
  it('the desktop knows exactly the exit kinds the Python helper names', () => {
    const pythonKinds = dictKeys(readPython(PY_NOTICE), '_EXIT_KIND_CAUSES')

    expect(pythonKinds.length).toBeGreaterThan(0)
    expect([...EXIT_KINDS].sort()).toEqual([...pythonKinds].sort())
  })

  it('the kinds that need a code are the same on both sides', () => {
    const source = readPython(PY_NOTICE)
    const withCode = keysNeedingCode(source, '_EXIT_KIND_CAUSES')

    // The desktop mirrors the same split: `nonzero_exit` and `signaled` need
    // a code; the rest do not. See `crashedCauseLabel` in notice-cause.ts.
    expect([...withCode].sort()).toEqual(['nonzero_exit', 'signaled'])
  })

  it('the producer classifies into the same vocabulary', () => {
    const source = readPython(PY_DISPATCH)

    // `_classify_worker_exit` returns these tokens; every one of them must
    // be a kind the renderers on BOTH sides can name.
    for (const kind of EXIT_KINDS) {
      expect(source, `the producer should be able to classify ${kind}`).toContain(`"${kind}"`)
    }
  })
})
