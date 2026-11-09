/**
 * Plan Usage: status-bar chips for Claude / ChatGPT (Codex) plan windows per pooled account,
 * Nous Portal credits and Admin-key API spend.
 *
 * Where the numbers come from:
 *   - usage windows: the gateway's `model.options` RPC (host.request), i.e. Hermes core's own
 *     per-account usage cache. This plugin never sees a subscription token.
 *   - account names / order / default, Nous credits, API spend: this package's backend
 *     (ctx.rest -> /api/plugins/plan-usage/*).
 * Preferences live on the plugin's own page under Settings -> Plugins -> Plan Usage
 * (ctx.registerSettingsPage); the Admin-key form is its "Agent settings" sub-page.
 * No secret ever reaches the renderer.
 */
import {
  Button, Codicon, Input, ListRow, PALETTE_AREA, SETTINGS_PLUGINS_AREA, STATUSBAR_AREAS, StatusDot, Switch,
  Tip, ToggleRow, cn, haptic, host, pluginSettingsHref, queryClient, useQuery,
} from '@hermes/plugin-sdk'
import { Fragment, jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useState } from 'react'

const ID = 'plan-usage'
const STATUS_KEY = ['plan-usage-status']
const USAGE_KEY = ['plan-usage-model-options']
const CONFIG_KEY = ['plan-usage-config']
const CLICK_REFRESH_MS = 30_000
const FAMILY = { claude: 'Claude', codex: 'ChatGPT / Codex' }
const SLUG = { claude: 'anthropic', codex: 'openai-codex' }

let _rest = null
let _storage = null
let _lastClick = 0

// ── formatting ────────────────────────────────────────────────────────────────

function fmtReset(iso) {
  if (!iso) return ''
  const ms = Date.parse(iso) - Date.now()
  if (!Number.isFinite(ms)) return ''
  if (ms <= 0) return 'resetting'
  const m = Math.round(ms / 60000)
  if (m < 60) return `resets in ${m}m`
  const h = Math.floor(m / 60)
  if (h < 48) return `resets in ${h}h ${m % 60}m`
  return `resets in ${Math.round(h / 24)}d`
}

function fmtWhen(iso) {
  if (!iso) return ''
  const s = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000))
  if (s < 90) return 'just now'
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  return `${Math.round(s / 3600)}h ago`
}

function money(v) {
  return typeof v === 'number' && Number.isFinite(v) ? `$${v.toFixed(2)}` : '—'
}

function shortLabel(label) {
  const l = String(label || '').toLowerCase()
  if (l.includes('session') || l.includes('5h') || l.includes('5-hour') || l.includes('hour')) return '5h'
  if (l.includes('week')) return 'wk'
  if (l.includes('month')) return 'mo'
  if (l.includes('day')) return 'day'
  if (l.includes('large') || l.includes('top')) return 'top'
  return String(label || '').slice(0, 4)
}

// Hermes names Codex windows by length but only knows 5h and 7d; a ChatGPT Free account's 30-day
// window falls back to the positional "Session" label. A window whose reset is more than 5h away
// cannot be a 5-hour window, so it is renamed Monthly (Free plan, or a reset over a week away) or Weekly.
function fixWindow(plan, w) {
  const low = String(w.label || '').toLowerCase()
  const secs = w.resets_at ? (Date.parse(w.resets_at) - Date.now()) / 1000 : NaN
  if ((low.includes('session') || low.includes('5h') || low.includes('5-hour')) && secs > 5 * 3600 + 600) {
    const label = (String(plan || '').toLowerCase() === 'free' || secs > 7 * 86400 + 3600) ? 'Monthly' : 'Weekly'
    return { ...w, label, short: shortLabel(label) }
  }
  return w.short ? w : { ...w, short: shortLabel(w.label) }
}

function toneForUsed(maxUsed) {
  if (maxUsed >= 90) return 'bad'
  if (maxUsed >= 80) return 'warn'
  return 'good'
}

// ── joining pool rows (backend) with Hermes's usage cache (model.options) ────

function providerRow(options, slug) {
  return (options?.providers || []).find(p => p && p.slug === slug) || null
}

function isLimited(windows) {
  return windows.some(w => (w.scope || 'account') === 'account' && Number(w.used_percent) >= 100)
}

/**
 * Attach usage windows to the backend's pool rows. `model.options` lists a multi-entry pool as
 * `usage.accounts` in pool order (one row per account, several credentials of one Codex account
 * merged), each with the entry's label; a single-account provider carries `usage.windows`.
 * Rows are matched by label in order; a pool row whose label was already consumed by a merged
 * account is marked `duplicate` and hidden, so one account shows once.
 */
function joinUsage(section, options, slug) {
  const rows = (section?.accounts || []).map(r => ({ ...r, windows: [], state: r.dead ? 'unavailable' : 'unknown', resets_at: null }))
  const usage = providerRow(options, slug)?.usage
  if (!usage) return rows
  if (Array.isArray(usage.accounts) && usage.accounts.length) {
    const used = new Set()
    for (const row of rows) {
      const idx = usage.accounts.findIndex((a, i) => !used.has(i) && String(a.label || '') === row.pool_label)
      if (idx < 0) {
        if (usage.accounts.some((a, i) => used.has(i) && String(a.label || '') === row.pool_label)) row.duplicate = true
        continue
      }
      used.add(idx)
      const acc = usage.accounts[idx]
      row.windows = Array.isArray(acc.windows) ? acc.windows : []
      row.state = acc.state || row.state
      row.resets_at = acc.resets_at || null
    }
    return rows
  }
  const live = rows.filter(r => !r.dead)
  if (live.length === 1 && Array.isArray(usage.windows) && usage.windows.length) {
    live[0].windows = usage.windows
    live[0].state = isLimited(usage.windows) ? 'limited' : 'ready'
  }
  return rows
}

/** Default (first live), the one Hermes is on now (first not limited), and the next fallback. */
function poolOrder(rows, strategy) {
  const live = rows.filter(r => !r.dead && !r.duplicate)
  const def = live[0] || null
  const inUse = live.find(r => r.state !== 'limited') || null
  const next = live.slice(1).find(r => r.state !== 'limited') || null
  return {
    strategy: strategy || 'fill_first',
    default_name: def ? def.name : null,
    default_resets_at: def && def.state === 'limited' ? def.resets_at : null,
    in_use_key: inUse ? inUse.key : null,
    in_use_name: inUse ? inUse.name : null,
    next_name: next ? next.name : null,
    fallback_active: !!(def && inUse && inUse !== def),
    all_out: live.length > 0 && !inUse,
  }
}

function sectionView(data, options, key) {
  const sec = data?.[key]
  if (!sec) return null
  if (!sec.ok) return { ok: false, error: sec.error, accounts: [], order: null }
  const rows = joinUsage(sec, options, SLUG[key])
  const order = poolOrder(rows, sec.strategy)
  for (const r of rows) r.in_use = r.key === order.in_use_key
  return { ok: true, accounts: rows, order, hint: sec.hint }
}

// ── chips ─────────────────────────────────────────────────────────────────────

const CHIP = cn(
  'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem] tabular-nums transition-colors whitespace-nowrap',
  'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
)

// Click and palette share this throttle; the backend enforces the same 30 s floor on /refresh.
async function refreshNow() {
  const now = Date.now()
  if (!_rest || now - _lastClick < CLICK_REFRESH_MS) return
  _lastClick = now
  haptic('tap')
  try {
    const fresh = await _rest('/refresh', { method: 'POST', body: {} })
    if (fresh && typeof fresh === 'object') queryClient.setQueryData(STATUS_KEY, fresh)
    queryClient.invalidateQueries({ queryKey: USAGE_KEY })
    // Background fetches land a few seconds later.
    setTimeout(() => {
      queryClient.invalidateQueries({ queryKey: STATUS_KEY })
      queryClient.invalidateQueries({ queryKey: USAGE_KEY })
    }, 6000)
  } catch { /* backend off: chips show their own error state */ }
}

function Chip({ tone, text, tip }) {
  return jsx(Tip, {
    label: tip,
    children: jsxs('button', {
      type: 'button',
      className: CHIP,
      onClick: refreshNow,
      children: [jsx(StatusDot, { tone }), jsx('span', { children: text })]
    })
  })
}

function tipLines(lines) {
  return jsx('span', {
    className: 'grid gap-0.5 py-0.5 text-[0.6875rem]',
    children: lines.filter(Boolean).map((l, i) => jsx('span', { className: i === 0 ? 'font-medium' : '', children: l }, i))
  })
}

function accountChip(acc, family) {
  const ws = (acc.windows || []).map(w => fixWindow(null, w))
  if (!ws.length) {
    const why = {
      unknown: 'no usage data from Hermes yet',
      unavailable: 'signed out — sign in again with hermes auth add',
      limited: `limit reached${acc.resets_at ? ` · ${fmtReset(acc.resets_at)}` : ''}`
    }[acc.state] || acc.state
    return jsx(Chip, { key: acc.key, tone: acc.state === 'unavailable' || acc.state === 'limited' ? 'bad' : 'muted', text: `${acc.name} …`,
      tip: tipLines([`${acc.name} (${family})`, why, 'Click to refresh']) })
  }
  const maxUsed = Math.max(...ws.map(w => Number(w.used_percent) || 0))
  const text = `${acc.name} ${ws.map(w => `${w.short || shortLabel(w.label)} ${Math.round(w.used_percent)}%`).join(' · ')}`
  const lines = [
    `${acc.name} — ${family}`,
    ...ws.map(w => `${w.label}: ${Math.round(w.used_percent)}% used, ${Math.max(0, 100 - Math.round(w.used_percent))}% left${w.resets_at ? ` · ${fmtReset(w.resets_at)}` : ''}`),
    acc.state === 'limited' ? 'Limit reached' : null,
    acc.is_default && acc.in_use ? 'Default — Hermes is using this account' : acc.is_default ? 'Default — out for now, Hermes is using a fallback' : acc.in_use ? 'In use as fallback' : null,
    'Click to refresh'
  ]
  return jsx(Chip, { key: acc.key, tone: acc.state === 'limited' ? 'bad' : toneForUsed(maxUsed), text, tip: tipLines(lines) })
}

const GAUGE_WORDS = { day: 'Daily', week: 'Weekly', month: 'Monthly', top_tier: 'Large models' }

/** Nous usage-window plans: gauges from model.options' `plan_usage` (left_fraction -> % used). */
function nousWindows(options) {
  const plan = providerRow(options, 'nous')?.plan_usage
  const out = []
  for (const g of plan?.gauges || []) {
    const left = Number(g.left_fraction)
    if (!Number.isFinite(left)) continue
    const label = GAUGE_WORDS[g.label] || g.title || String(g.label || '')
    out.push({ label, short: shortLabel(label), used_percent: Math.max(0, Math.min(100, (1 - left) * 100)),
      resets_at: g.resets_at_ms ? new Date(g.resets_at_ms).toISOString() : null })
  }
  return { plan: plan?.plan || null, windows: out }
}

function nousChip(n, options) {
  if (!n || n.loading) return jsx(Chip, { key: 'nous', tone: 'muted', text: 'Nous …', tip: tipLines(['Nous Portal', 'loading…']) })
  if (!n.ok) return jsx(Chip, { key: 'nous', tone: 'bad', text: 'Nous !', tip: tipLines(['Nous Portal', n.error || 'unavailable']) })
  const { plan, windows: ws } = nousWindows(options)
  const maxUsed = ws.length ? Math.max(...ws.map(w => w.used_percent)) : 0
  const tone = n.depleted ? 'bad' : (typeof n.total_usd === 'number' && n.total_usd < 1) ? 'warn' : toneForUsed(maxUsed)
  const text = `Nous ${money(n.total_usd)}${ws.length ? ` · ${ws.map(w => `${w.short} ${Math.round(w.used_percent)}%`).join(' · ')}` : ''}`
  const lines = [
    `Nous Portal${plan ? ` (${plan})` : ''}`,
    ...ws.map(w => `${w.label}: ${Math.round(w.used_percent)}% used${w.resets_at ? ` · ${fmtReset(w.resets_at)}` : ''}`),
    ...(n.lines || []),
    n.depleted ? 'Paid access paused — top up in the Portal' : null,
    n.fetched_at ? `Updated ${fmtWhen(n.fetched_at)}` : null
  ]
  return jsx(Chip, { key: 'nous', tone, text, tip: tipLines(lines) })
}

function apiChip(k) {
  if (k.loading) return jsx(Chip, { key: k.env, tone: 'muted', text: `${k.name} …`, tip: tipLines([k.name, k.env, 'loading…']) })
  if (!k.ok) return jsx(Chip, { key: k.env, tone: 'bad', text: `${k.name} !`, tip: tipLines([k.name, k.env, k.error]) })
  return jsx(Chip, {
    key: k.env, tone: 'good', text: `${k.name} ${money(k.month_to_date_usd)}`,
    tip: tipLines([`${k.name} — month-to-date spend`, `${money(k.month_to_date_usd)} since ${k.period_start}`, k.note, `Key: ${k.env}`,
      k.fetched_at ? `Updated ${fmtWhen(k.fetched_at)}` : null])
  })
}

// ── default-account / fallback warnings ───────────────────────────────────────

const WARN_AT = 90
const WARNED_KEY = 'warned.v1'

/** Which limit window a warning belongs to: the reset time rounded to the hour (Hermes may
 *  recompute it a few seconds differently on each fetch), else the UTC day, so a warning shows
 *  once per window and at most once a day when Hermes reports no reset time. */
function windowBucket(iso) {
  const t = iso ? Date.parse(iso) : NaN
  if (Number.isFinite(t)) return new Date(Math.round(t / 3_600_000) * 3_600_000).toISOString().slice(0, 13)
  return `day-${new Date().toISOString().slice(0, 10)}`
}

function _warnedOnce(id) {
  let seen = []
  try { seen = _storage ? (_storage.get(WARNED_KEY, []) || []) : [] } catch { seen = [] }
  if (!Array.isArray(seen)) seen = []
  if (seen.includes(id)) return false
  try { _storage?.set(WARNED_KEY, [...seen, id].slice(-60)) } catch { /* storage unavailable */ }
  return true
}

/** Pure: which warnings this joined snapshot calls for. */
function fallbackWarnings(views) {
  const out = []
  for (const key of ['codex', 'claude']) {
    const sec = views[key]
    const family = FAMILY[key]
    const order = sec?.order
    if (!sec?.ok || !order || order.strategy !== 'fill_first') continue
    if (order.fallback_active) {
      out.push({
        id: `switched:${key}:${order.default_name}:${windowBucket(order.default_resets_at)}`,
        kind: 'warning', persistent: true, title: `${family}: switched account`,
        message: `${order.default_name} is out. Hermes moves to ${order.in_use_name}` +
          (order.default_resets_at ? ` and goes back to ${order.default_name} when its limit ${fmtReset(order.default_resets_at)}.` : '.')
      })
      continue
    }
    if (order.all_out) {
      out.push({ id: `allout:${key}:${windowBucket(order.default_resets_at)}`, kind: 'error', persistent: true,
        title: `${family}: every account is out`, message: `No ${family} account has usage left. Hermes can't use ${family} until a limit resets.` })
      continue
    }
    const def = (sec.accounts || []).find(a => a.is_default)
    if (!def) continue
    const hot = (def.windows || []).map(w => fixWindow(null, w))
      .filter(w => (w.scope || 'account') === 'account' && w.used_percent >= WARN_AT)
      .sort((a, b) => b.used_percent - a.used_percent)[0]
    if (!hot) continue
    out.push({
      id: `near:${key}:${def.name}:${hot.label}:${windowBucket(hot.resets_at)}`,
      kind: 'warning', persistent: true, title: `${family}: ${def.name} almost out`,
      message: `${def.name} is at ${Math.round(hot.used_percent)}% of its ${hot.label.toLowerCase()} limit. ` +
        (order.next_name
          ? `Hermes will move to ${order.next_name} when it runs out.`
          : `There's no other ${family} account, so Hermes will stop using ${family} until its limit ${hot.resets_at ? fmtReset(hot.resets_at) : 'resets'}.`)
    })
  }
  return out
}

function useFallbackWarnings(views, ready, data, options) {
  useEffect(() => {
    if (!ready) return
    for (const w of fallbackWarnings(views)) {
      if (_warnedOnce(w.id)) host.notify({ ...w, id: `plan-usage:${w.id}` })
    }
  }, [data, options, ready])  // views is derived from data + options
}

function pollMs(q) {
  return Math.max(60, Number(q?.state?.data?.poll_seconds) || 120) * 1000
}

function StatusGroup() {
  const { data, error } = useQuery({ queryKey: STATUS_KEY, queryFn: () => _rest('/status'), refetchInterval: pollMs, staleTime: 20_000 })
  const wantsUsage = !!(data && (data.claude || data.codex || data.nous))
  const pollSeconds = Math.max(60, Number(data?.poll_seconds) || 120)
  const { data: options, isFetched } = useQuery({
    queryKey: USAGE_KEY,
    queryFn: () => host.request('model.options', {}),
    enabled: wantsUsage,
    refetchInterval: pollSeconds * 1000,
    refetchOnWindowFocus: false,
    staleTime: 20_000
  })
  const views = { claude: sectionView(data, options, 'claude'), codex: sectionView(data, options, 'codex') }
  useFallbackWarnings(views, !!data && isFetched, data, options)
  if (error && !data) {
    return jsx(Chip, { tone: 'muted', text: 'Plan usage off', tip: tipLines(['Plan Usage backend not reachable',
      'Enable it with: hermes plugins enable plan-usage, then restart Hermes']) })
  }
  if (!data) return null
  const chips = []
  // API-key rows (e.g. env:ANTHROPIC_API_KEY) never have plan windows: no chip, but they still
  // count for the default / fallback order above.
  const shown = acc => acc.enabled && !acc.duplicate && !acc.api_key
  if (data.nous) chips.push(nousChip(data.nous, options))
  if (views.codex) {
    if (!views.codex.ok) chips.push(jsx(Chip, { key: 'codex-err', tone: 'bad', text: 'ChatGPT !', tip: tipLines(['ChatGPT / Codex', views.codex.error]) }))
    chips.push(...views.codex.accounts.filter(shown).map(a => accountChip(a, FAMILY.codex)))
  }
  if (views.claude) {
    if (!views.claude.ok) chips.push(jsx(Chip, { key: 'claude-err', tone: 'bad', text: 'Claude !', tip: tipLines(['Claude', views.claude.error]) }))
    chips.push(...views.claude.accounts.filter(shown).map(a => accountChip(a, FAMILY.claude)))
  }
  if (data.api) chips.push(...(data.api.keys || []).map(apiChip))
  return jsx(Fragment, { children: chips })
}

// ── settings page (Settings -> Plugins -> Plan Usage) ─────────────────────────

const SECTION_META = {
  claude: { title: 'Claude', sub: 'Plan windows for each Claude account in Hermes' },
  codex: { title: 'ChatGPT / Codex', sub: 'Plan windows for each ChatGPT account in Hermes' },
  nous: { title: 'Nous Portal', sub: 'Subscription and top-up credits' },
  api: { title: 'API spend', sub: 'Month-to-date (UTC) spend from Anthropic / OpenAI Admin keys' }
}

const SECTION_HELP = {
  codex: 'Add another ChatGPT account with: hermes auth add openai-codex --label "Name". Remove one with: hermes auth remove openai-codex <id>.',
  claude: 'Add another Claude account with: hermes auth add anthropic --label "Name". Remove one with: hermes auth remove anthropic <id>.',
  api: 'Only Admin keys can read spend: sk-ant-admin… (Anthropic organization) or sk-admin… (OpenAI organization owner). Paste them on the Agent settings sub-page; Hermes stores them. This page never shows the values.'
}

const CAPTION = 'text-[0.6875rem] text-(--ui-text-quaternary)'

/** One account/key line: switch, name field, and a quiet caption underneath. */
function ItemRow({ on, onToggle, value, placeholder, onName, caption, extra }) {
  return jsxs('div', {
    className: cn('flex items-start gap-3', !on && 'opacity-60'),
    children: [
      jsx('div', { className: 'pt-2', children: onToggle
        ? jsx(Switch, { size: 'xs', checked: on, onCheckedChange: v => onToggle(!!v), 'aria-label': `Show ${placeholder}` })
        : jsx('span', { className: 'inline-block w-7' }) }),
      jsxs('div', {
        className: 'grid min-w-0 flex-1 gap-1',
        children: [
          jsx(Input, { value, placeholder, maxLength: 40, onChange: e => onName(e.target.value), 'aria-label': `Display name for ${placeholder}` }),
          jsx('div', { className: cn('truncate', CAPTION), children: caption })
        ]
      }),
      extra ? jsx('div', { className: 'pt-1.5', children: extra }) : null
    ]
  })
}

function SettingsPage() {
  const [draft, setDraft] = useState(null)
  const [saving, setSaving] = useState(false)
  const [busyDefault, setBusyDefault] = useState(null)
  const { data, error } = useQuery({ queryKey: CONFIG_KEY, queryFn: () => _rest('/config'), staleTime: 0, refetchOnWindowFocus: false })

  // Seed the draft once, so a refetch (after a star click) never wipes unsaved edits.
  useEffect(() => { if (data?.config && !draft) setDraft(JSON.parse(JSON.stringify(data.config))) }, [data, draft])

  const set = fn => setDraft(prev => { const next = JSON.parse(JSON.stringify(prev)); fn(next); return next })
  const isOn = key => !(draft?.disabled || []).includes(key)
  const toggle = (key, on) => set(d => {
    d.disabled = (d.disabled || []).filter(k => k !== key)
    if (!on) d.disabled.push(key)
  })
  const setName = (map, key, v) => set(d => { if (v.trim()) d[map][key] = v; else delete d[map][key] })
  const dirty = !!(draft && data?.config && JSON.stringify(draft) !== JSON.stringify(data.config))

  async function save() {
    setSaving(true)
    try {
      const res = await _rest('/config', { method: 'POST', body: { config: draft } })
      if (res?.config) setDraft(JSON.parse(JSON.stringify(res.config)))
      await queryClient.invalidateQueries({ queryKey: CONFIG_KEY })
      await queryClient.invalidateQueries({ queryKey: STATUS_KEY })
      host.notify({ kind: 'success', message: 'Plan Usage settings saved' })
    } catch (e) {
      host.notify({ kind: 'error', message: `Could not save: ${e?.message || e}` })
    } finally { setSaving(false) }
  }

  async function makeDefault(acc) {
    setBusyDefault(acc.key)
    try {
      const res = await _rest('/default', { method: 'POST', body: { key: acc.key } })
      await queryClient.invalidateQueries({ queryKey: CONFIG_KEY })
      await queryClient.invalidateQueries({ queryKey: STATUS_KEY })
      if (!res || !res.ok) {
        // ok:false with a note = Hermes kept another account first (its own ordering rules).
        host.notify({ kind: res?.note ? 'warning' : 'error', message: res?.note || res?.error || 'Could not set default' })
        return
      }
      host.notify({ kind: res.note ? 'warning' : 'success', message: res.note || `${draft.names[acc.key] || acc.name} is now the default ${FAMILY[acc.section]} account` })
    } catch (e) {
      host.notify({ kind: 'error', message: `${e?.message || e}` })
    } finally {
      setBusyDefault(null)
    }
  }

  function accountRows(section) {
    const cli = SLUG[section]
    const rows = (data?.accounts || []).filter(a => a.section === section)
    if (!rows.length) {
      return jsx('div', { className: 'text-xs text-(--ui-text-tertiary)',
        children: `No account yet. Add one in a terminal: hermes auth add ${cli} --label "Name"` })
    }
    return rows.map(acc => jsx(ItemRow, {
      key: acc.key,
      on: acc.api_key ? true : isOn(acc.key), onToggle: acc.api_key ? null : v => toggle(acc.key, v),
      value: draft.names[acc.key] ?? '', placeholder: acc.name,
      onName: v => setName('names', acc.key, v),
      caption: [acc.pool_label ? `Hermes account: ${acc.pool_label}` : 'Hermes account',
        acc.api_key ? 'API key, no plan windows (no chip; still in the fallback order)' : null,
        acc.dead ? 'signed out' : null].filter(Boolean).join(' · '),
      extra: acc.is_default
        ? jsx(Tip, { label: 'Hermes uses this account first and falls back to the next one only when it runs out',
            children: jsxs('span', { className: 'inline-flex items-center gap-1 rounded-full border border-(--ui-stroke-secondary) px-2 py-0.5 text-[0.6875rem] text-(--ui-text-secondary)',
              children: [jsx(Codicon, { name: 'star-full' }), 'Default'] }) })
        : jsx(Tip, { label: 'Make this the account Hermes uses first (same as hermes auth priority)',
            children: jsx(Button, { size: 'sm', variant: 'ghost', className: 'text-(--ui-text-tertiary)', disabled: busyDefault === acc.key || acc.dead,
              'aria-label': 'Make default', onClick: () => makeDefault(acc), children: jsx(Codicon, { name: 'star-empty' }) }) })
    }))
  }

  function keyRows() {
    return (data?.api_keys || []).map(slot => jsx(ItemRow, {
      key: slot.env,
      on: isOn(slot.env), onToggle: v => toggle(slot.env, v),
      value: draft.key_names[slot.env] ?? '', placeholder: slot.name || 'Display name',
      onName: v => setName('key_names', slot.env, v),
      caption: [slot.env, slot.provider === 'openai' ? 'OpenAI' : 'Anthropic',
        slot.present ? 'set' : 'not set — add it on the Agent settings sub-page'].join(' · ')
    }))
  }

  function section(id, children) {
    const meta = SECTION_META[id]
    return jsx(ToggleRow, {
      key: id, wide: true, label: meta.title,
      description: SECTION_HELP[id] ? `${meta.sub}. ${SECTION_HELP[id]}` : meta.sub,
      checked: !!draft.sections[id], onChange: v => set(d => { d.sections[id] = !!v }),
      below: children && draft.sections[id] ? jsx('div', { className: 'mt-3 grid gap-3', children }) : null
    })
  }

  if (error && !data) {
    return jsx(ListRow, { title: 'Plan Usage backend not reachable',
      description: 'Enable it with: hermes plugins enable plan-usage, then restart Hermes.' })
  }
  if (!draft) return jsx('div', { className: 'py-6 text-sm text-(--ui-text-tertiary)', children: 'Loading…' })

  return jsxs('div', {
    className: 'grid gap-1',
    children: [
      jsx('div', { className: 'pb-2 text-xs text-(--ui-text-tertiary)',
        children: 'Choose what shows in the status bar. Settings are saved for the current Hermes profile; no secrets are stored here.' }),
      section('nous'),
      section('codex', accountRows('codex')),
      section('claude', accountRows('claude')),
      section('api', keyRows()),
      jsx(ListRow, {
        title: 'Refresh', description: 'How often the status bar asks for new numbers (60–1800 seconds).',
        action: jsx(Input, { className: 'w-24', type: 'number', min: 60, max: 1800, value: draft.poll_seconds, 'aria-label': 'Poll interval in seconds',
          onChange: e => { const v = parseInt(e.target.value, 10); set(d => { d.poll_seconds = Number.isFinite(v) ? v : 120 }) } })
      }),
      jsxs('div', {
        className: 'flex justify-end gap-2 border-t border-(--ui-stroke-secondary) pt-3',
        children: [
          jsx(Button, { variant: 'ghost', disabled: !dirty || saving, onClick: () => setDraft(JSON.parse(JSON.stringify(data.config))), children: 'Discard' }),
          jsx(Button, { disabled: saving || !dirty, onClick: save, children: saving ? 'Saving…' : 'Save' })
        ]
      })
    ]
  })
}

export default {
  id: ID,
  name: 'Plan Usage',
  description: 'Claude / ChatGPT plan windows per account, Nous credits and Admin-key API spend in the status bar.',
  register(ctx) {
    _rest = ctx.rest
    _storage = ctx.storage
    ctx.register({ id: 'chips', area: STATUSBAR_AREAS.right, order: 48, render: () => jsx(StatusGroup, {}) })
    // Preferences: one entry under Settings -> Plugins (the SDK's home for plugin settings).
    const page = { id: 'settings', title: 'Plan Usage', icon: 'pulse', render: () => jsx(SettingsPage, {}) }
    if (ctx.registerSettingsPage) ctx.registerSettingsPage(page)
    else ctx.register({ area: SETTINGS_PLUGINS_AREA, id: page.id, title: page.title, render: page.render, data: { icon: page.icon } })
    ctx.register({
      id: 'open-settings', area: PALETTE_AREA,
      data: { id: 'plan-usage.settings', label: 'Plan Usage: settings', keywords: ['quota', 'usage', 'claude', 'codex', 'nous', 'credits'],
        run: () => host.navigate(pluginSettingsHref(ID)) }
    })
    ctx.register({
      id: 'refresh', area: PALETTE_AREA,
      data: { id: 'plan-usage.refresh', label: 'Plan Usage: refresh now', keywords: ['quota', 'usage'], run: () => refreshNow() }
    })
    // An Admin key saved in Settings -> Plugins -> Plan Usage shows up without waiting for the poll.
    ctx.onEvent(`plugin.${ID}.settings.changed`, () => {
      queryClient.invalidateQueries({ queryKey: STATUS_KEY })
      queryClient.invalidateQueries({ queryKey: CONFIG_KEY })
    })
  }
}
