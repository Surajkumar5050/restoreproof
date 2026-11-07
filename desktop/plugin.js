/**
 * RestoreProof for Hermes Desktop.
 *
 *  - Pane: the drill running now (stage by stage) or the latest verdict, findings and history.
 *  - Status bar chip: the last verdict at a glance.
 *  - Inline card: the model writes ::restoreproof{run="rp-..."} and the evidence renders in chat.
 *  - Report tab: the full case-file report, sandboxed, inside Hermes.
 *  - Pet line + OS notification when a drill you are watching finishes.
 *
 * Inside Desktop the card borrows the app's look: every color, font and radius comes from the active
 * theme (--color-card, --color-border, --color-foreground, --color-muted-foreground,
 * --color-destructive, --font-sans, --font-mono). Pass and partial use the app's status green and
 * yellow, falling back to Hermes's ui_ok and ui_warn.
 *
 * Read-only: every request goes to this plugin's own backend (dashboard/plugin_api.py), which only
 * reads run folders.
 */

import { atom, Button, GlyphSpinner, haptic, host, SandboxedFrame, Tip, TRANSCRIPT_DIRECTIVE_AREA, useValue } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'restoreproof'
const RUN_ID = /^rp-\d{8}-\d{6}-[0-9a-f]{4}$/

// -- theme tokens -----------------------------------------------------------------------------------

const FG = 'var(--color-foreground)'
const MUTED = 'var(--color-muted-foreground)'
const BORDER = 'var(--color-border)'
const SURFACE = 'var(--color-card)'
const FAIL = 'var(--color-destructive)'
const OK = 'var(--ui-green, #4caf50)'
const WARN = 'var(--ui-yellow, #ffa726)'
const SANS = 'var(--font-sans, system-ui, sans-serif)'
const MONO = 'var(--font-mono, ui-monospace, monospace)'
const RADIUS = 'var(--radius-md, 8px)'

const VERDICT_COLOR = { PASS: OK, PARTIAL: WARN, FAIL, BLOCKED: MUTED, INCONCLUSIVE: MUTED, RUNNING: MUTED }
const STAGE_ORDER = ['PREFLIGHT', 'PROVISIONING', 'RESTORING', 'STARTING', 'VALIDATING', 'CLEANING']
const STAGE_LABEL = {
  PREFLIGHT: 'preflight',
  PROVISIONING: 'isolate',
  RESTORING: 'restore',
  STARTING: 'boot',
  VALIDATING: 'verify',
  CLEANING: 'clean up'
}

// -- shared state -----------------------------------------------------------------------------------

const $live = atom(null) // state.json of the drill running now, or null
const $runs = atom([]) // recent runs (history rows)
const $cards = atom({}) // run_id -> card payload
const $selected = atom(null) // run shown in the pane; null = latest
const $error = atom(null) // backend unreachable message

let ctxRef = null

function api(path) {
  return ctxRef.rest(path)
}

async function loadCard(runId) {
  const cached = $cards.get()[runId]
  if (cached) return cached
  const card = await api(`/runs/${runId}`)
  $cards.set({ ...$cards.get(), [runId]: card })
  return card
}

function petLine(card) {
  if (card.verdict === 'PASS') return `${card.app} came back in ${card.metrics.recovery}. Every check passed.`
  const ref = card.findings.find(f => f.kind === 'reference')
  if (ref) return `${card.app}: ${card.verdict}. ${ref.missing} of ${ref.references} files would not come back.`
  const first = card.findings[0]
  return `${card.app}: ${card.verdict}.${first ? ' ' + sentence(first.name) + ' failed.' : ''}`
}

function announce(card) {
  try {
    ctxRef.pet.say(petLine(card).slice(0, 120), { id: 'verdict', tone: card.verdict === 'PASS' ? 'info' : 'error', ttlMs: 14000 })
  } catch {}
  try {
    ctxRef.os.notify({ title: `RestoreProof · ${card.app} · ${card.verdict}`, body: card.headline })
  } catch {}
}

async function refresh() {
  try {
    const [{ run }, { runs }] = await Promise.all([api('/live'), api('/runs?limit=8')])
    const was = $live.get()
    $live.set(run || null)
    $runs.set(runs || [])
    $error.set(null)
    if (was && !run) {
      // A drill we watched just finished: show it and say so.
      const card = await loadCard(was.run_id)
      $selected.set(null)
      announce(card)
    }
    const latest = (runs || []).find(r => r.verdict !== 'RUNNING')
    if (latest) await loadCard(latest.run_id)
  } catch {
    $error.set('RestoreProof’s backend is not reachable. Enable the plugin (hermes plugins enable restoreproof) and restart the gateway.')
  }
}

// -- helpers ----------------------------------------------------------------------------------------

function ago(iso) {
  if (!iso) return ''
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

function secs(n) {
  if (n == null) return ''
  if (n < 1) return '<1s'
  if (n < 60) return `${n.toFixed(1)}s`
  return `${Math.floor(n / 60)}m ${String(Math.round(n % 60)).padStart(2, '0')}s`
}

function sentence(text) {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : ''
}

function findingLine(f) {
  if (f.kind === 'reference' && f.references) {
    const folders = f.folders || []
    const where = folders.length === 1 ? `, all under ${folders[0][0]}/` : ''
    return { text: `${f.missing.toLocaleString()} of ${f.references.toLocaleString()} referenced files missing${where}`, fact: null }
  }
  if (f.kind === 'journey' && f.steps && f.steps.length) {
    const bad = f.steps.find(s => !s.ok) || f.steps[f.steps.length - 1]
    return { text: sentence(f.name), fact: `${bad.method} ${bad.path} → ${bad.status ?? 'no response'}` }
  }
  return { text: sentence(f.name), fact: f.summary }
}

function Mono({ children, color, size = 11, style }) {
  return jsx('span', { style: { font: `${size}px/1.5 ${MONO}`, color: color || MUTED, ...style }, children })
}

// -- report tab -------------------------------------------------------------------------------------

function openReport(card) {
  haptic('tap')
  const render = () => jsx(ReportFrame, { runId: card.run_id })
  if (typeof host.openWorkspace === 'function') {
    host.openWorkspace('restoreproof-report', { title: `${card.app} · ${card.verdict}`, render, minWidth: '480px' })
  } else if (card.report) {
    ctxRef.os.revealPath(card.report)
  }
}

async function showEvidence(card) {
  haptic('tap')
  const ok = card.report ? await ctxRef.os.revealPath(card.report) : false
  if (!ok) host.notify({ kind: 'info', message: `Evidence for ${card.run_id} is in the RestoreProof data folder on the Hermes host.` })
}

function toDataUrl(htmlText) {
  const bytes = new TextEncoder().encode(htmlText)
  let bin = ''
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000))
  return 'data:text/html;charset=utf-8;base64,' + btoa(bin)
}

function ReportFrame({ runId }) {
  const [src, setSrc] = useState(null)
  const [err, setErr] = useState(null)
  useEffect(() => {
    let alive = true
    api(`/runs/${runId}/report`)
      .then(r => alive && setSrc(toDataUrl(r.html)))
      .catch(e => alive && setErr(String((e && e.message) || e)))
    return () => {
      alive = false
    }
  }, [runId])
  if (err) return jsx('div', { style: { padding: 16, color: FAIL, font: `13px ${SANS}` }, children: `Could not load the report: ${err}` })
  if (!src) return jsx('div', { style: { padding: 16 }, children: jsx(GlyphSpinner, { ariaLabel: 'Loading report' }) })
  return jsx(SandboxedFrame, { src, title: `RestoreProof report ${runId}`, style: { width: '100%', height: '100%', border: 0 } })
}

// -- the evidence card (pane and transcript) --------------------------------------------------------

function VerdictLabel({ verdict }) {
  const color = VERDICT_COLOR[verdict] || MUTED
  return jsx('span', {
    'aria-label': `Verdict ${verdict}`,
    style: {
      font: `600 12px/1 ${MONO}`,
      letterSpacing: '0.06em',
      color,
      border: `1px solid ${color}`,
      borderRadius: 4,
      padding: '4px 8px 3px',
      whiteSpace: 'nowrap',
      flexShrink: 0
    },
    children: verdict
  })
}

function Timeline({ stages, total }) {
  const byLabel = {}
  for (const s of stages) byLabel[s.label.toLowerCase()] = s
  const parts = []
  for (const key of ['preflight', 'isolate', 'restore', 'boot', 'verify']) {
    const s = byLabel[key]
    if (!s) continue
    if (s.state === 'fail') parts.push(`${key} failed`)
    else if (['restore', 'boot', 'verify'].includes(key) && s.state === 'pass' && s.seconds != null) parts.push(`${key} ${secs(s.seconds)}`)
  }
  const failedAt = stages.find(s => s.state === 'fail')
  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', gap: 6 },
    children: [
      jsx('div', {
        role: 'img',
        'aria-label': `Stage durations to scale${failedAt ? `; ${failedAt.label.toLowerCase()} failed` : ''}`,
        style: { display: 'flex', height: 6, gap: 2 },
        children: stages.map((s, i) =>
          jsx(
            Tip,
            {
              label: `${s.label}${s.seconds != null ? ` · ${secs(s.seconds)}` : ''}${s.state === 'fail' ? ' · failed' : ''}`,
              children: jsx('div', {
                style: {
                  flex: Math.max(s.seconds || 0, 0.15),
                  borderRadius: 1,
                  background:
                    s.state === 'fail' ? FAIL : s.label === 'Clean up' || s.state === 'idle' || s.state === 'skip' ? BORDER : MUTED
                }
              })
            },
            i
          )
        )
      }),
      jsxs('div', {
        style: { display: 'flex', justifyContent: 'space-between', gap: 8 },
        children: [jsx(Mono, { children: parts.join(' · ') }), jsx(Mono, { children: total })]
      })
    ]
  })
}

function CheckLine({ ok, text, fact }) {
  return jsxs('li', {
    style: { display: 'flex', gap: 8, alignItems: 'baseline', minWidth: 0 },
    children: [
      jsx(Mono, { size: 13, color: ok ? OK : FAIL, children: ok ? '✓' : '✗' }),
      jsxs('span', {
        style: { minWidth: 0, color: ok ? MUTED : FG },
        children: [text, fact ? jsx(Mono, { style: { display: 'block', overflowWrap: 'anywhere' }, children: fact }) : null]
      })
    ]
  })
}

function EvidenceCard({ card, maxFindings = 2 }) {
  const m = card.metrics
  const shown = card.findings.slice(0, maxFindings)
  const extra = card.findings.length - shown.length
  const others = card.checks.passed
  const footer = []
  if (card.claimed) footer.push(`backup job said ${String(card.claimed.status).toLowerCase()}`)
  footer.push(`recovery ${m.recovery}${m.rto_target ? ` / ${m.rto_target}` : ''}`)
  if (m.rpo && m.rpo !== 'unknown') footer.push(`RPO ${m.rpo}${m.rpo_target ? ` / ${m.rpo_target}` : ''}`)
  return jsxs('article', {
    style: {
      background: SURFACE,
      border: `1px solid ${BORDER}`,
      borderRadius: RADIUS,
      padding: 16,
      display: 'flex',
      flexDirection: 'column',
      gap: 14,
      font: `14px/1.55 ${SANS}`,
      color: FG,
      maxWidth: 640
    },
    children: [
      jsxs('header', {
        style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 },
        children: [
          jsxs('div', {
            style: { display: 'flex', alignItems: 'baseline', gap: 10, minWidth: 0, flexWrap: 'wrap' },
            children: [
              jsx('span', { style: { font: `600 15px/1.25 ${SANS}`, color: FG }, children: card.app }),
              jsx(Mono, { children: `recovery drill · ${card.recovery_point || ''}` })
            ]
          }),
          jsx(VerdictLabel, { verdict: card.verdict })
        ]
      }),
      jsx('p', { style: { margin: 0, color: FG }, children: card.headline }),
      card.stages && card.stages.length ? jsx(Timeline, { stages: card.stages, total: m.total }) : null,
      jsxs('ul', {
        style: { margin: 0, padding: 0, listStyle: 'none', display: 'flex', flexDirection: 'column', gap: 6, fontSize: 13 },
        children: [
          ...shown.map((f, i) => {
            const line = findingLine(f)
            return jsx(CheckLine, { ok: false, text: line.text, fact: line.fact }, i)
          }),
          extra > 0 ? jsx(CheckLine, { ok: false, text: `${extra} more failed check${extra === 1 ? '' : 's'} in the report` }, 'more') : null,
          jsx(
            CheckLine,
            {
              ok: card.cleanup.ok,
              text: `${others} ${card.findings.length ? 'other ' : ''}check${others === 1 ? '' : 's'} passed · ${
                card.cleanup.ok ? 'nothing left running' : 'cleanup incomplete, run hermes restoreproof cleanup'
              }`
            },
            'ok'
          )
        ]
      }),
      jsxs('footer', {
        style: {
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          gap: 12,
          flexWrap: 'wrap',
          borderTop: `1px solid ${BORDER}`,
          paddingTop: 12
        },
        children: [
          jsx(Mono, { children: footer.join(' · ') }),
          jsxs('div', {
            style: { display: 'flex', gap: 8 },
            children: [
              jsx(Button, { size: 'sm', variant: 'outline', onClick: () => showEvidence(card), children: 'Evidence' }),
              jsx(Button, { size: 'sm', onClick: () => openReport(card), children: 'Open report' })
            ]
          })
        ]
      })
    ]
  })
}

// -- live drill -------------------------------------------------------------------------------------

function LiveDrill({ run }) {
  const current = STAGE_ORDER.indexOf(run.stage)
  const failed = s => run.checks.some(c => c.stage === s && (c.status === 'FAIL' || c.status === 'ERROR'))
  const recent = run.checks.slice(-5)
  return jsxs('article', {
    style: {
      background: SURFACE,
      border: `1px solid ${BORDER}`,
      borderRadius: RADIUS,
      padding: 16,
      display: 'flex',
      flexDirection: 'column',
      gap: 12,
      font: `14px/1.55 ${SANS}`,
      color: FG
    },
    children: [
      jsxs('header', {
        style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 },
        children: [
          jsxs('div', {
            style: { display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' },
            children: [jsx('span', { style: { font: `600 15px/1.25 ${SANS}` }, children: run.app }), jsx(Mono, { children: 'recovery drill · running' })]
          }),
          jsx(GlyphSpinner, { ariaLabel: 'Drill running' })
        ]
      }),
      jsx('div', {
        style: { display: 'flex', flexWrap: 'wrap', columnGap: 12, rowGap: 2 },
        children: STAGE_ORDER.map((s, i) => {
          const mark = failed(s) ? '✗' : i < current ? '✓' : i === current ? '●' : '·'
          const color = failed(s) ? FAIL : i < current ? OK : i === current ? FG : MUTED
          return jsx(Mono, { size: 12, color, children: `${mark} ${STAGE_LABEL[s]}` }, s)
        })
      }),
      jsx('ul', {
        style: { margin: 0, padding: 0, listStyle: 'none', display: 'flex', flexDirection: 'column', gap: 4, fontSize: 13 },
        children: recent.map(c => jsx(CheckLine, { ok: c.status === 'PASS', text: sentence(c.name) }, c.id))
      })
    ]
  })
}

// -- pane -------------------------------------------------------------------------------------------

function History({ runs, selected }) {
  if (!runs.length) return null
  return jsxs('section', {
    style: { display: 'flex', flexDirection: 'column', gap: 2 },
    children: [
      jsx(Mono, { style: { marginBottom: 4 }, children: 'recent drills' }),
      ...runs.map(r =>
        jsxs(
          'button',
          {
            type: 'button',
            onClick: () => {
              haptic('tap')
              $selected.set(r.verdict === 'RUNNING' ? null : r.run_id)
              if (r.verdict !== 'RUNNING') loadCard(r.run_id).catch(() => {})
            },
            style: {
              display: 'grid',
              gridTemplateColumns: '1fr auto',
              gap: 8,
              alignItems: 'baseline',
              textAlign: 'left',
              padding: '6px 8px',
              borderRadius: 4,
              border: 0,
              cursor: 'pointer',
              background: selected === r.run_id ? 'var(--ui-row-active-background)' : 'transparent',
              color: FG,
              font: `13px/1.4 ${SANS}`
            },
            children: [
              jsxs('span', {
                style: { minWidth: 0, display: 'flex', flexDirection: 'column' },
                children: [
                  jsx('span', { style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, children: r.recovery_point || r.run_id }),
                  jsx(Mono, { style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, children: r.app })
                ]
              }),
              jsxs('span', {
                style: { whiteSpace: 'nowrap' },
                children: [jsx(Mono, { color: VERDICT_COLOR[r.verdict] || MUTED, children: r.verdict }), jsx(Mono, { children: ` · ${ago(r.started_at)}` })]
              })
            ]
          },
          r.run_id
        )
      )
    ]
  })
}

function Empty() {
  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', gap: 8, padding: '8px 2px', font: `13px/1.55 ${SANS}`, color: MUTED },
    children: [
      jsx('span', { style: { font: `600 15px/1.3 ${SANS}`, color: FG }, children: 'No drills yet' }),
      jsx('span', { children: 'A backup job saying “success” is a claim. A drill is the proof.' }),
      jsx(Mono, { children: 'Ask Hermes to prove your app can come back from last night’s backup, or run /drill run restoreproof.yaml' })
    ]
  })
}

function Pane() {
  const live = useValue($live)
  const runs = useValue($runs)
  const cards = useValue($cards)
  const selected = useValue($selected)
  const error = useValue($error)
  useEffect(() => {
    refresh()
  }, [])
  const latest = runs.find(r => r.verdict !== 'RUNNING')
  const showId = selected || (latest && latest.run_id)
  const card = showId ? cards[showId] : null
  let body
  if (error) body = jsx('div', { style: { color: FAIL, font: `13px/1.5 ${SANS}` }, children: error })
  else if (live && !selected) body = jsx(LiveDrill, { run: live })
  else if (card) body = jsx(EvidenceCard, { card, maxFindings: 6 })
  else if (!runs.length) body = jsx(Empty, {})
  else body = jsx(GlyphSpinner, { ariaLabel: 'Loading' })
  return jsxs('div', {
    style: { height: '100%', overflowY: 'auto', padding: 12, display: 'flex', flexDirection: 'column', gap: 16, boxSizing: 'border-box' },
    children: [body, jsx(History, { runs, selected: showId })]
  })
}

function Chip() {
  const live = useValue($live)
  const runs = useValue($runs)
  const latest = runs.find(r => r.verdict !== 'RUNNING')
  if (!live && !latest) return null
  const label = live ? `drill · ${STAGE_LABEL[live.stage] || 'running'}` : `drill ${latest.verdict} · ${ago(latest.started_at)}`
  return jsx(Tip, {
    label: live ? `RestoreProof drill running for ${live.app}` : `${latest.app}: ${latest.reason || latest.verdict}`,
    children: jsxs('button', {
      type: 'button',
      onClick: () => {
        haptic('tap')
        if (latest && !live) loadCard(latest.run_id).then(openReport).catch(() => {})
      },
      style: {
        display: 'inline-flex',
        alignItems: 'center',
        gap: 5,
        height: '100%',
        padding: '0 6px',
        border: 0,
        background: 'transparent',
        cursor: 'pointer',
        color: MUTED,
        font: `11px ${MONO}`
      },
      children: [
        jsx('span', { style: { width: 7, height: 7, borderRadius: 99, background: live ? MUTED : VERDICT_COLOR[latest.verdict] || MUTED } }),
        label
      ]
    })
  })
}

function InlineCard({ attrs, streaming }) {
  const runId = attrs && attrs.run
  const valid = typeof runId === 'string' && RUN_ID.test(runId)
  const cards = useValue($cards)
  const [err, setErr] = useState(false)
  useEffect(() => {
    if (valid && !streaming) loadCard(runId).catch(() => setErr(true))
  }, [runId, valid, streaming])
  if (!valid) return null // untrusted model output: render nothing rather than guess
  const card = cards[runId]
  if (err) return jsx(Mono, { size: 12, children: `RestoreProof run ${runId} is not available on this Hermes host.` })
  if (!card) return jsx(GlyphSpinner, { ariaLabel: 'Loading drill evidence' })
  return jsx('div', { style: { margin: '4px 0' }, children: jsx(EvidenceCard, { card }) })
}

export default {
  id: ID,
  name: 'RestoreProof',
  register(ctx) {
    ctxRef = ctx
    ctx.register({ id: 'pane', area: 'panes', title: 'restoreproof', data: { placement: 'right', width: '340px' }, render: () => jsx(Pane, {}) })
    ctx.register({ id: 'chip', area: 'statusBar.right', order: 140, render: () => jsx(Chip, {}) })
    ctx.register({ id: 'card', area: TRANSCRIPT_DIRECTIVE_AREA, data: { name: 'restoreproof', render: props => jsx(InlineCard, props) } })
    // Poll fast while a drill runs, slowly otherwise.
    let tick = 0
    refresh()
    ctx.setInterval(() => {
      tick += 1
      if ($live.get() || tick % 6 === 0) refresh()
    }, 1500)
  }
}
