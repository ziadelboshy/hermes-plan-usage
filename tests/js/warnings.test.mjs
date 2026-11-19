// Offline tests for the pop-up warnings: node --import ./tests/js/register.mjs --test tests/js/warnings.test.mjs
import test from 'node:test'
import assert from 'node:assert/strict'

const { _test: { warnMark, alreadyWarned, fallbackWarnings } } = await import('../../desktop/plugin.js')

const HOUR = 3_600_000
const reset = Date.parse('2026-11-20T10:29:59Z')  // just under a :30 edge, where hour rounding flipped
const iso = ms => new Date(ms).toISOString()

function views(usedPercent, resetsAt, { next = null } = {}) {
  return {
    claude: {
      ok: true,
      order: { strategy: 'fill_first', fallback_active: false, all_out: false, next_name: next },
      accounts: [{ name: 'Main', is_default: true,
        windows: [{ label: 'Current week', scope: 'account', used_percent: usedPercent, resets_at: resetsAt }] }]
    }
  }
}

test('almost-out warnings are off unless the setting is on', () => {
  assert.deepEqual(fallbackWarnings(views(92, iso(reset))), [])
  assert.deepEqual(fallbackWarnings(views(92, iso(reset)), { nearLimit: false }), [])
  const [w] = fallbackWarnings(views(92, iso(reset)), { nearLimit: true })
  assert.match(w.message, /Main is at 92% of its current week limit/)
})

test('a drifting reset time stays one warning for the whole window', () => {
  const seen = []
  let shown = 0
  for (let i = 0; i < 200; i++) {            // 200 polls, reset time jitters a few seconds each way
    const drift = ((i * 7919) % 11 - 5) * 1000
    const [w] = fallbackWarnings(views(92 + (i % 5), iso(reset + drift)), { nearLimit: true })
    if (!alreadyWarned(seen, w.id)) { seen.push(w.id); shown++ }
  }
  assert.equal(shown, 1)
})

test('the next limit window warns again', () => {
  const first = warnMark('near:claude:Main:Current week', iso(reset))
  const nextWeek = warnMark('near:claude:Main:Current week', iso(reset + 7 * 24 * HOUR))
  assert.equal(alreadyWarned([first], nextWeek), false)
  assert.equal(alreadyWarned([first], warnMark('near:claude:Other:Current week', iso(reset))), false)
})

test('no reset time means at most once a day', () => {
  const a = warnMark('allout:claude', null)
  assert.match(a, /@day-\d{4}-\d{2}-\d{2}$/)
  assert.equal(alreadyWarned([a], warnMark('allout:claude', null)), true)
  assert.equal(alreadyWarned(['allout:claude@day-2000-01-01'], a), false)
})

test('account-switch and all-out warnings still show with almost-out off', () => {
  const v = views(100, iso(reset))
  v.claude.order = { strategy: 'fill_first', fallback_active: true, default_name: 'Main', in_use_name: 'Spare',
    default_resets_at: iso(reset) }
  assert.equal(fallbackWarnings(v)[0].title, 'Claude: switched account')
  v.claude.order = { strategy: 'fill_first', all_out: true, default_resets_at: iso(reset) }
  assert.equal(fallbackWarnings(v)[0].title, 'Claude: every account is out')
})

test('old-format and junk storage entries are ignored', () => {
  const m = warnMark('near:claude:Main:Current week', iso(reset))
  assert.equal(alreadyWarned(['near:claude:Main:Current week:2026-11-20T10', 42, null], m), false)
})
