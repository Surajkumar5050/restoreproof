/**
 * RestoreProof for Hermes Desktop.
 *
 *  - Pane: the drill running now (stage by stage) or the latest verdict, findings and history.
 *  - Status bar chip: the last verdict at a glance.
 *  - Inline card: the model writes ::restoreproof{run="rp-..."} and the evidence renders in chat.
 *  - Report tab: the full case-file report, sandboxed, inside Hermes.
 *  - Drill view: a workspace tab that opens by itself when a drill starts and shows what it does
 *    behind the scenes (artifacts, the isolated network, containers, file counters, the reference
 *    grid, probe requests, cleanup, verdict, event log). The same renderer replays finished runs.
 *    Every state in it is derived from the drill's own events.jsonl; nothing is simulated.
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
import { useEffect, useMemo, useRef, useState } from 'react'
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
const PRIMARY = 'var(--color-primary)'
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
    if (run && run.run_id && !autoOpened.has(run.run_id)) {
      // A drill just started (or was already running when Desktop opened): show it, once per run.
      autoOpened.add(run.run_id)
      openDrillView(run.run_id, { app: run.app, live: true })
    }
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
            style: { display: 'flex', gap: 8, flexWrap: 'wrap' },
            children: [
              canOpenView()
                ? jsx(
                    Button,
                    {
                      size: 'sm',
                      variant: 'outline',
                      onClick: () => {
                        haptic('tap')
                        openDrillView(card.run_id, { app: card.app })
                      },
                      children: 'Replay'
                    },
                    'replay'
                  )
                : null,
              jsx(Button, { size: 'sm', variant: 'outline', onClick: () => showEvidence(card), children: 'Evidence' }, 'evidence'),
              jsx(Button, { size: 'sm', onClick: () => openReport(card), children: 'Open report' }, 'report')
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
          jsxs('div', {
            style: { display: 'flex', alignItems: 'center', gap: 8 },
            children: [
              canOpenView()
                ? jsx(Button, {
                    size: 'sm',
                    variant: 'outline',
                    onClick: () => {
                      haptic('tap')
                      openDrillView(run.run_id, { app: run.app, live: true })
                    },
                    children: 'Watch'
                  })
                : null,
              jsx(GlyphSpinner, { ariaLabel: 'Drill running' })
            ]
          })
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
        if (live) openDrillView(live.run_id, { app: live.app, live: true })
        else if (latest) loadCard(latest.run_id).then(openReport).catch(() => {})
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

// -- drill view: live and replay ------------------------------------------------------------------
//
// One renderer for both. It fetches the run's events.jsonl through the backend and derives every
// visual state from the events whose time is at or before a playhead:
//   live    the playhead follows the newest event in real rhythm, about a second behind;
//   replay  the playhead runs from 0 at the chosen speed and can be scrubbed.
// A container is lit only after its "started" event and dark after "removed"; a counter moves only
// on a "progress" event; the reference grid is the engine's own missing-run map. Nothing in between
// is invented.

const VIEW_ID = 'restoreproof-drill'
const autoOpened = new Set()

function prefersReducedMotion() {
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches
  } catch {
    return false
  }
}

function canOpenView() {
  return typeof host.openWorkspace === 'function'
}

function openDrillView(runId, { app, live = false } = {}) {
  if (!canOpenView() || !RUN_ID.test(runId)) return false
  host.openWorkspace(VIEW_ID, {
    title: `${live ? 'Live drill' : 'Replay'} · ${app || runId}`,
    minWidth: '560px',
    render: () => jsx(DrillView, { runId, live }, runId)
  })
  return true
}

function num(n) {
  return n == null ? '' : Number(n).toLocaleString()
}

function bytes(n) {
  if (n == null) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

function at(t) {
  return `${(t || 0).toFixed(1)}s`
}

function basename(p) {
  return p ? String(p).split(/[\\/]/).filter(Boolean).pop() : ''
}

// Everything the view shows at playhead t, computed from events with e.t <= t.
function reduceEvents(events, t) {
  const s = {
    app: '', label: '', started: null, stage: null, stageAt: {}, failedStages: new Set(),
    artifacts: [], net: null, nodes: {}, db: null, files: {}, scratchRemoved: false,
    checks: [], refs: null, journeys: [], cleanup: null, done: null, blocked: null,
    hasResources: false, upTo: 0, shown: 0
  }
  for (const e of events) {
    if (e.t > t) break
    s.shown += 1
    s.upTo = e.t
    switch (e.kind) {
      case 'start':
        s.app = e.app
        s.label = basename(e.recovery_dir)
        s.started = e.ts
        break
      case 'stage':
        s.stage = e.stage
        s.stageAt[e.stage] = e.t
        break
      case 'artifact':
        s.artifacts = [...s.artifacts.filter(a => a.name !== e.name), e]
        break
      case 'resource':
        s.hasResources = true
        if (e.type === 'network') s.net = { name: e.name, status: e.action }
        else if (e.type === 'scratch') s.scratchRemoved = e.action === 'removed'
        else s.nodes = { ...s.nodes, [e.role]: { ...(s.nodes[e.role] || {}), name: e.name, image: e.image || (s.nodes[e.role] || {}).image, status: e.action } }
        break
      case 'restored':
        if (e.what === 'database') s.db = { tables: e.tables, rows: e.rows }
        break
      case 'progress': {
        const f = s.files[e.what] || {}
        s.files = { ...s.files, [e.what]: { ...f, [e.unit]: { done: e.done, total: e.total } } }
        break
      }
      case 'check':
        s.checks = [...s.checks, e.check]
        if (e.check.mandatory && (e.check.status === 'FAIL' || e.check.status === 'ERROR')) s.failedStages.add(e.check.stage)
        break
      case 'references':
        s.refs = e
        break
      case 'journey': {
        let j = s.journeys.find(x => x.check === e.check)
        if (!j) {
          j = { check: e.check, total: e.total, steps: [] }
          s.journeys = [...s.journeys, j]
        }
        const step = j.steps[e.index] || {}
        j.steps[e.index] = e.phase === 'request' ? { ...step, request: e } : { ...step, response: e }
        break
      }
      case 'cleanup':
        s.cleanup = e
        break
      case 'blocked':
        s.blocked = e.reason
        break
      case 'done':
        s.done = e
        break
      default:
        break
    }
  }
  return s
}

// One line of the event log, or null for events that only move a counter.
function logLine(e) {
  const c = e.check
  switch (e.kind) {
    case 'start':
      return { text: `drill started · ${e.trigger || 'manual'} · ${basename(e.recovery_dir)}` }
    case 'stage':
      return { text: `stage → ${STAGE_LABEL[e.stage] || e.stage.toLowerCase()}`, tone: 'stage' }
    case 'artifact':
      if (!e.present) return { text: `${e.file} is missing from the backup set`, tone: 'bad' }
      return {
        text: `${e.file} · ${bytes(e.bytes)} · sha256 ${e.sha256} · ${
          e.digest === 'match' ? 'matches the backup job’s record' : e.digest === 'mismatch' ? 'does NOT match the recorded digest' : 'no recorded digest'
        }`,
        tone: e.digest === 'mismatch' ? 'bad' : null
      }
    case 'resource':
      return {
        text: `${e.type === 'scratch' ? 'restored files' : `${e.type} ${e.name}`} ${e.action}${e.image ? ` · ${e.image}` : ''}${
          e.type === 'network' && e.action === 'created' ? ' · --internal, no egress' : ''
        }`
      }
    case 'restored':
      return { text: `database restored · ${num(e.tables)} tables, ${num(e.rows)} rows` }
    case 'progress':
      if (e.done !== e.total) return null
      return { text: `${e.what}: ${num(e.done)} files ${e.unit === 'checksummed' ? 'checksummed' : 'written to scratch storage'}` }
    case 'check':
      return { text: `${c.name}: ${c.summary}`, tone: c.status === 'PASS' || c.status === 'SKIPPED' ? null : 'bad', mark: c.status }
    case 'references':
      return { text: `${num(e.total)} file references checked · ${num(e.missing)} missing`, tone: e.missing ? 'bad' : 'ok' }
    case 'journey':
      if (e.phase === 'request') return { text: `probe → app  ${e.method} ${e.path}` }
      return {
        text: `app → probe  ${e.status ?? e.error ?? 'no response'}${e.content_type ? ` ${e.content_type}` : ''}${
          e.elapsed != null ? ` · ${Math.round(e.elapsed * 1000)}ms` : ''
        }${e.failed && e.failed.length ? ` · expected ${e.failed.join('; ')}` : ''}`,
        tone: e.ok ? 'ok' : 'bad'
      }
    case 'cleanup':
      return { text: e.ok ? `cleanup confirmed by re-listing · ${e.removed} removed, nothing left` : 'cleanup incomplete', tone: e.ok ? 'ok' : 'bad' }
    case 'blocked':
      return { text: `blocked: ${e.reason}`, tone: 'bad' }
    case 'done':
      return { text: `verdict ${e.verdict}`, tone: e.verdict === 'PASS' ? 'ok' : 'bad' }
    default:
      return null
  }
}

function useRunEvents(runId) {
  const [st, setSt] = useState({ events: [], finished: false, error: null, loaded: false, arrivedAt: 0 })
  useEffect(() => {
    let alive = true
    let next = 0
    let events = []
    let arrivedAt = 0
    let busy = false
    let stop = null
    const pull = async () => {
      if (busy) return
      busy = true
      try {
        const r = await api(`/runs/${runId}/events?since=${next}`)
        if (!alive) return
        if (r.events.length) {
          events = events.concat(r.events)
          next = r.next
          arrivedAt = Date.now()
        }
        setSt({ events, finished: !!r.finished, error: null, loaded: true, arrivedAt })
        if (r.finished && stop) {
          stop()
          stop = null
        }
      } catch (e) {
        if (alive) setSt(s => ({ ...s, error: String((e && e.message) || e), loaded: true }))
      } finally {
        busy = false
      }
    }
    stop = ctxRef.setInterval(pull, 1000)
    pull()
    return () => {
      alive = false
      if (stop) stop()
    }
  }, [runId])
  return st
}

function usePlayer(events, finished, live) {
  const end = events.length ? events[events.length - 1].t : 0
  const [mode, setMode] = useState(live ? 'live' : 'replay')
  const [playing, setPlaying] = useState(true)
  const [speed, setSpeed] = useState(1)
  const [t, setT] = useState(live ? Infinity : 0)
  const endRef = useRef(end)
  endRef.current = end
  const finishedRef = useRef(finished)
  finishedRef.current = finished
  useEffect(() => {
    if (!playing) return undefined
    let last = Date.now()
    const stop = ctxRef.setInterval(() => {
      const now = Date.now()
      const dt = (now - last) / 1000
      last = now
      setT(prev => {
        const e = endRef.current
        if (mode === 'live') {
          // Follow the newest event in its real rhythm; if we fall far behind (a tab opened
          // mid-drill), jump close to it instead of replaying the past.
          const base = prev === Infinity ? Math.max(0, e - 1) : prev
          let next = Math.min(e, base + dt)
          if (e - next > 2.5) next = e - 1
          if (finishedRef.current && next >= e) setPlaying(false)
          return next
        }
        const next = Math.min(e, (prev === Infinity ? e : prev) + dt * speed)
        if (finishedRef.current && next >= e) setPlaying(false)
        return next
      })
    }, 100)
    return stop
  }, [playing, speed, mode])
  const replay = () => {
    haptic('tap')
    setMode('replay')
    setT(0)
    setPlaying(true)
  }
  const toggle = () => {
    haptic('tap')
    if (!playing && t >= end) setT(0)
    if (mode === 'live' && finished) setMode('replay')
    setPlaying(p => !p)
  }
  const seek = v => {
    setMode('replay')
    setT(Math.max(0, Math.min(end, v)))
  }
  return { t: t === Infinity ? end : Math.min(t, end), end, mode, playing, speed, setSpeed, replay, toggle, seek }
}

// -- drill view parts -------------------------------------------------------------------------------

function Panel({ title, aside, children, style }) {
  return jsxs('section', {
    style: {
      background: SURFACE,
      border: `1px solid ${BORDER}`,
      borderRadius: RADIUS,
      padding: 14,
      display: 'flex',
      flexDirection: 'column',
      gap: 12,
      minWidth: 0,
      ...style
    },
    children: [
      jsxs('header', {
        style: { display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 8 },
        children: [jsx('span', { style: { font: `600 13px/1.3 ${SANS}`, color: FG }, children: title }), aside ? jsx(Mono, { children: aside }) : null]
      }),
      children
    ]
  })
}

function Bar({ done, total, active }) {
  const pct = total ? Math.min(100, (done / total) * 100) : 0
  return jsx('div', {
    role: 'progressbar',
    'aria-valuemin': 0,
    'aria-valuemax': total || 0,
    'aria-valuenow': done || 0,
    style: { height: 4, borderRadius: 2, background: BORDER, overflow: 'hidden' },
    children: jsx('div', { style: { width: `${pct}%`, height: '100%', background: active ? PRIMARY : MUTED } })
  })
}

function StageStrip({ s, failedAtEnd }) {
  const now = s.upTo
  return jsx('ol', {
    style: { listStyle: 'none', margin: 0, padding: 0, display: 'grid', gridTemplateColumns: 'repeat(6, minmax(0, 1fr))', gap: 6 },
    children: STAGE_ORDER.map((st, i) => {
      const startedAt = s.stageAt[st]
      const nextStage = STAGE_ORDER.slice(i + 1).find(x => s.stageAt[x] != null)
      const ended = nextStage ? s.stageAt[nextStage] : s.done && startedAt != null ? s.done.t : null
      const current = s.stage === st && !s.done
      const failed = s.failedStages.has(st) || (st === 'CLEANING' && failedAtEnd)
      const reached = startedAt != null
      const mark = failed ? '✗' : current ? '●' : reached ? '✓' : '·'
      const color = failed ? FAIL : current ? PRIMARY : reached ? OK : MUTED
      const dur = reached ? (ended != null ? ended : now) - startedAt : null
      return jsxs(
        'li',
        {
          'aria-current': current ? 'step' : undefined,
          style: { borderTop: `2px solid ${current ? PRIMARY : failed ? FAIL : reached ? BORDER : BORDER}`, paddingTop: 6, minWidth: 0 },
          children: [
            jsx(Mono, { size: 12, color, children: `${mark} ${STAGE_LABEL[st]}` }),
            jsx(Mono, { style: { display: 'block' }, children: dur != null ? secs(Math.max(0, dur)) : '—' })
          ]
        },
        st
      )
    })
  })
}

function BackupPanel({ s }) {
  const rows = s.artifacts.map(a =>
    jsxs(
      'li',
      {
        style: { display: 'flex', justifyContent: 'space-between', gap: 10, alignItems: 'baseline', minWidth: 0 },
        children: [
          jsxs('span', {
            style: { minWidth: 0, display: 'flex', flexDirection: 'column' },
            children: [jsx(Mono, { size: 13, color: FG, children: a.file }), jsx(Mono, { children: a.present ? `${bytes(a.bytes)} · sha256 ${a.sha256}` : 'missing from the backup set' })]
          }),
          jsx(Mono, {
            size: 12,
            color: !a.present || a.digest === 'mismatch' ? FAIL : a.digest === 'match' ? OK : MUTED,
            style: { whiteSpace: 'nowrap' },
            children: !a.present ? '✗ missing' : a.digest === 'match' ? '✓ digest' : a.digest === 'mismatch' ? '✗ digest' : 'no digest'
          })
        ]
      },
      a.name
    )
  )
  const fileBlocks = Object.entries(s.files).map(([name, f]) => {
    const w = f.files
    const c = f.checksummed
    return jsxs(
      'div',
      {
        style: { display: 'flex', flexDirection: 'column', gap: 6, opacity: s.scratchRemoved ? 0.5 : 1 },
        children: [
          jsxs('div', {
            style: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'baseline' },
            children: [
              jsx('span', { style: { font: `13px/1.4 ${SANS}`, color: FG }, children: `${name} → scratch storage` }),
              jsx(Mono, { size: 13, color: FG, children: w ? `${num(w.done)} / ${num(w.total)}` : '' })
            ]
          }),
          w ? jsx(Bar, { done: w.done, total: w.total, active: w.done < w.total }) : null,
          c
            ? jsxs('div', {
                style: { display: 'flex', flexDirection: 'column', gap: 4 },
                children: [jsx(Mono, { children: `checksummed ${num(c.done)} / ${num(c.total)}` }), jsx(Bar, { done: c.done, total: c.total, active: c.done < c.total })]
              })
            : null,
          s.scratchRemoved ? jsx(Mono, { children: 'deleted at cleanup' }) : null
        ]
      },
      name
    )
  })
  return jsx(Panel, {
    title: 'Backup set',
    aside: s.label,
    children: jsxs('div', {
      style: { display: 'flex', flexDirection: 'column', gap: 14 },
      children: [
        rows.length
          ? jsx('ul', { style: { listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: 8 }, children: rows })
          : jsx(Mono, {
              children: s.blocked ? 'not read: the drill was blocked' : s.done ? 'artifact details were not recorded in this run' : 'reading the backup set…'
            }),
        ...fileBlocks
      ]
    })
  })
}

function Node({ role, title, node, detail, last, legacy }) {
  const status = node ? node.status : null
  const up = status === 'started'
  const gone = status === 'removed'
  return jsxs('li', {
    style: {
      display: 'grid',
      gridTemplateColumns: '12px minmax(0, 1fr)',
      columnGap: 10,
      alignItems: 'start',
      paddingBottom: last ? 0 : 10,
      borderBottom: last ? 0 : `1px solid ${BORDER}`,
      opacity: gone || !status ? 0.55 : 1,
      transition: prefersReducedMotion() ? 'none' : 'opacity 240ms ease'
    },
    children: [
      jsx('span', {
        'aria-hidden': true,
        style: {
          width: 10,
          height: 10,
          marginTop: 4,
          borderRadius: 99,
          boxSizing: 'border-box',
          border: `2px solid ${up ? OK : status === 'created' ? FG : MUTED}`,
          background: up ? OK : 'transparent'
        }
      }),
      jsxs('div', {
        style: { minWidth: 0, display: 'flex', flexDirection: 'column', gap: 2 },
        children: [
          jsxs('div', {
            style: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'baseline' },
            children: [
              jsx('span', { style: { font: `600 13px/1.4 ${SANS}`, color: FG }, children: title }),
              jsx(Mono, { children: !status ? (legacy ? 'not recorded' : 'not started') : gone ? 'removed' : status === 'created' ? 'created' : 'running' })
            ]
          }),
          node && node.image ? jsx(Mono, { style: { overflowWrap: 'anywhere' }, children: node.image }) : null,
          detail ? jsx('span', { style: { font: `13px/1.45 ${SANS}`, color: MUTED }, children: detail }) : null
        ]
      })
    ],
    'aria-label': `${role} ${status || 'not started'}`
  })
}

function RequestRow({ step, index, reduced }) {
  const req = step.request
  const res = step.response
  if (!req) return null
  const bad = res && !res.ok
  return jsxs(
    'li',
    {
      style: { display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0 },
      children: [
        jsxs('div', {
          style: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'baseline', minWidth: 0 },
          children: [
            jsx(Mono, { size: 12, color: FG, style: { overflowWrap: 'anywhere', minWidth: 0 }, children: `${index + 1}  ${req.method} ${req.path}` }),
            jsx(Mono, {
              size: 12,
              color: !res ? PRIMARY : bad ? FAIL : OK,
              style: { whiteSpace: 'nowrap' },
              children: !res ? 'in flight' : `${res.ok ? '✓' : '✗'} ${res.status ?? 'no reply'}${res.elapsed != null ? ` · ${Math.round(res.elapsed * 1000)}ms` : ''}`
            })
          ]
        }),
        jsx('div', {
          'aria-hidden': true,
          style: { position: 'relative', height: 8 },
          children: [
            jsx('div', { style: { position: 'absolute', left: 0, right: 0, top: 4, height: 1, background: BORDER } }, 'line'),
            jsx(
              'div',
              {
                style: {
                  position: 'absolute',
                  top: 1,
                  left: res ? 'calc(100% - 7px)' : 0,
                  width: 7,
                  height: 7,
                  borderRadius: 99,
                  background: !res ? PRIMARY : bad ? FAIL : OK,
                  transition: reduced ? 'none' : 'left 320ms ease'
                }
              },
              'dot'
            )
          ]
        }),
        bad && res.failed && res.failed.length ? jsx(Mono, { color: FAIL, children: `expected ${res.failed.join('; ')}` }) : null
      ]
    },
    index
  )
}

function NetworkPanel({ s, finished, reduced }) {
  const boot = s.checks.find(c => c.kind === 'service')
  const nodes = s.nodes
  const net = s.net
  const steps = s.journeys.flatMap(j => j.steps.filter(Boolean))
  const legacy = finished && !s.hasResources && !s.blocked
  return jsx(Panel, {
    title: 'Isolated network',
    aside: net ? net.name : null,
    children: jsxs('div', {
      style: {
        border: `1px dashed ${net && net.status !== 'removed' ? FG : BORDER}`,
        borderRadius: RADIUS,
        padding: 12,
        display: 'flex',
        flexDirection: 'column',
        gap: 12,
        opacity: net && net.status === 'removed' ? 0.6 : 1
      },
      children: [
        jsx(Mono, {
          children: legacy
            ? 'This run was recorded before RestoreProof logged its containers; stages and checks are shown.'
            : !net
              ? s.blocked
                ? 'never created: nothing was started'
                : 'not created yet'
              : net.status === 'removed'
                ? 'removed at cleanup, confirmed by re-listing'
                : 'docker network --internal · no route to the internet or production'
        }),
        jsxs('ul', {
          style: { listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: 10 },
          children: [
            jsx(Node, { role: 'database', title: 'Database', legacy, node: nodes.database, detail: s.db ? `${num(s.db.rows)} rows in ${num(s.db.tables)} tables restored` : nodes.database && nodes.database.status === 'started' ? 'loading the dump…' : null }, 'db'),
            jsx(Node, { role: 'app', title: 'App', legacy, node: nodes.app, detail: boot ? `${boot.status === 'PASS' ? '✓' : '✗'} ${boot.summary}` : null }, 'app'),
            jsx(Node, { role: 'probe', title: 'Journey probe', legacy, node: nodes.probe, detail: steps.length ? 'requests to the app, from inside the network' : null, last: true }, 'probe')
          ]
        }),
        steps.length
          ? jsx('ol', {
              'aria-label': 'Probe requests',
              style: { listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: 10 },
              children: steps.map((step, i) => jsx(RequestRow, { step, index: i, reduced }, i))
            })
          : null
      ]
    })
  })
}

function RefGrid({ refs }) {
  const missing = useMemo(() => {
    const m = new Set()
    for (const [start, len] of refs.missing_runs || []) for (let i = start; i < start + len; i += 1) m.add(i)
    return m
  }, [refs])
  const cells = useMemo(
    () =>
      Array.from({ length: refs.total }, (_, i) =>
        jsx('span', { style: { width: 6, height: 6, borderRadius: 1, background: missing.has(i) ? FAIL : MUTED, opacity: missing.has(i) ? 1 : 0.35 } }, i)
      ),
    [refs, missing]
  )
  return jsxs('figure', {
    style: { margin: 0, display: 'flex', flexDirection: 'column', gap: 6 },
    children: [
      jsx('div', {
        role: 'img',
        'aria-label': `${num(refs.total)} files the database points to, in path order; ${num(refs.missing)} missing`,
        style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, 6px)', gap: 2 },
        children: cells
      }),
      jsxs('figcaption', {
        style: { display: 'flex', justifyContent: 'space-between', gap: 8, flexWrap: 'wrap' },
        children: [
          jsx(Mono, { children: `one square per referenced file, in path order${refs.truncated ? ' (first 200 gaps drawn)' : ''}` }),
          jsx(Mono, { color: refs.missing ? FAIL : OK, children: refs.missing ? `${num(refs.missing)} of ${num(refs.total)} missing` : `all ${num(refs.total)} present` })
        ]
      })
    ]
  })
}

function ChecksPanel({ s, finished }) {
  const failed = s.checks.filter(c => c.mandatory && (c.status === 'FAIL' || c.status === 'ERROR'))
  const passed = s.checks.filter(c => c.status === 'PASS')
  return jsx(Panel, {
    title: 'Checks',
    aside: s.checks.length ? `${passed.length} passed · ${failed.length} failed` : null,
    children: jsxs('div', {
      style: { display: 'flex', flexDirection: 'column', gap: 14 },
      children: [
        s.refs
          ? jsx(RefGrid, { refs: s.refs })
          : jsx(Mono, {
              children: s.done
                ? s.checks.some(c => c.kind === 'reference') || !s.checks.some(c => c.kind)
                  ? 'the per-file map was not recorded in this run; any result is listed below'
                  : 'no file references were checked'
                : s.stage === 'VALIDATING'
                  ? 'checking…'
                  : 'file references are checked after the app boots'
            }),
        jsx('ul', {
          style: { listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: 6, fontSize: 13 },
          children: s.checks
            .slice(-8)
            .map(c => jsx(CheckLine, { ok: c.status === 'PASS', text: sentence(c.name), fact: c.status === 'PASS' ? null : c.summary }, c.id))
        }),
        s.done
          ? jsxs('div', {
              'aria-live': 'polite',
              style: { borderTop: `1px solid ${BORDER}`, paddingTop: 12, display: 'flex', flexDirection: 'column', gap: 10 },
              children: [
                jsxs('div', {
                  style: { display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' },
                  children: [jsx(VerdictLabel, { verdict: s.done.verdict }), jsx(Mono, { children: `at ${at(s.done.t)}` })]
                }),
                jsx('span', { style: { font: `13px/1.5 ${SANS}`, color: FG }, children: sentence(s.done.reason) }),
                finished
                  ? jsx('div', {
                      children: jsx(Button, {
                        size: 'sm',
                        variant: 'outline',
                        onClick: () => loadCard(s.done.run_id).then(openReport).catch(() => {}),
                        children: 'Open report'
                      })
                    })
                  : null
              ]
            })
          : null
      ]
    })
  })
}

function EventLog({ events, t, follow }) {
  const ref = useRef(null)
  const lines = []
  for (const e of events) {
    if (e.t > t) break
    const l = logLine(e)
    if (l) lines.push({ ...l, t: e.t, i: lines.length })
  }
  useEffect(() => {
    if (follow && ref.current) ref.current.scrollTop = ref.current.scrollHeight
  }, [lines.length, follow])
  const tone = { ok: OK, bad: FAIL, stage: PRIMARY }
  return jsx(Panel, {
    title: 'Event log',
    aside: `${lines.length} events`,
    children: jsx('div', {
      ref,
      role: 'log',
      'aria-label': 'Drill event log',
      style: { maxHeight: 220, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 1 },
      children: lines.map(l =>
        jsxs(
          'div',
          {
            style: { display: 'grid', gridTemplateColumns: '56px minmax(0, 1fr)', gap: 10, font: `12px/1.6 ${MONO}` },
            children: [
              jsx('span', { style: { color: MUTED, textAlign: 'right' }, children: at(l.t) }),
              jsxs('span', {
                style: { color: tone[l.tone] || FG, overflowWrap: 'anywhere' },
                children: [
                  l.mark
                    ? jsx('span', { style: { color: l.mark === 'PASS' ? OK : l.mark === 'SKIPPED' ? MUTED : FAIL }, children: `${l.mark === 'PASS' ? '✓' : l.mark === 'SKIPPED' ? '–' : '✗'} ` })
                    : null,
                  l.text
                ]
              })
            ]
          },
          l.i
        )
      )
    })
  })
}

function Controls({ p, finished }) {
  if (p.mode === 'live' && !finished) return null
  const speeds = [1, 2, 4]
  return jsxs('div', {
    style: { display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' },
    children: [
      jsx(Button, { size: 'sm', onClick: p.toggle, children: p.playing ? 'Pause' : p.t >= p.end ? 'Replay' : 'Play' }),
      jsx('div', {
        role: 'group',
        'aria-label': 'Replay speed',
        style: { display: 'flex', gap: 4 },
        children: speeds.map(v =>
          jsx(
            Button,
            {
              size: 'sm',
              variant: p.speed === v ? 'default' : 'outline',
              onClick: () => {
                haptic('tap')
                p.setSpeed(v)
              },
              children: `${v}×`
            },
            v
          )
        )
      }),
      jsx('input', {
        type: 'range',
        min: 0,
        max: Math.max(p.end, 0.1),
        step: 0.1,
        value: p.t,
        'aria-label': 'Drill timeline',
        onChange: e => p.seek(Number(e.target.value)),
        style: { flex: '1 1 160px', minWidth: 120, accentColor: PRIMARY }
      }),
      jsx(Mono, { size: 12, children: `${at(p.t)} / ${at(p.end)}` })
    ]
  })
}

function DrillView({ runId, live }) {
  const { events, finished, error, loaded, arrivedAt } = useRunEvents(runId)
  const p = usePlayer(events, finished, live)
  const s = useMemo(() => reduceEvents(events, p.t), [events, p.t])
  const reduced = prefersReducedMotion()
  const [, setNow] = useState(0)
  const running = !finished && p.mode === 'live'
  useEffect(() => {
    if (!running) return undefined
    return ctxRef.setInterval(() => setNow(n => n + 1), 500)
  }, [running])
  if (error && !events.length)
    return jsx('div', { style: { padding: 20, color: FAIL, font: `13px/1.5 ${SANS}` }, children: `Could not load the drill’s events: ${error}` })
  if (!loaded) return jsx('div', { style: { padding: 20 }, children: jsx(GlyphSpinner, { ariaLabel: 'Loading drill' }) })
  // While live: the newest event's own drill time plus the time since it arrived. Both come from
  // this machine's clock and the engine's stopwatch, so a remote gateway's clock skew cannot show.
  const elapsed = running && events.length ? p.end + Math.max(0, (Date.now() - arrivedAt) / 1000) : p.t
  const status = running ? 'live' : p.mode === 'live' ? 'finished' : 'replay'
  return jsxs('div', {
    style: {
      height: '100%',
      overflowY: 'auto',
      boxSizing: 'border-box',
      padding: 20,
      display: 'flex',
      flexDirection: 'column',
      gap: 16,
      font: `14px/1.55 ${SANS}`,
      color: FG
    },
    children: [
      jsxs('header', {
        style: { display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 16, flexWrap: 'wrap' },
        children: [
          jsxs('div', {
            style: { display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 },
            children: [
              jsx('span', { style: { font: `600 18px/1.3 ${SANS}` }, children: s.app || 'Recovery drill' }),
              jsx(Mono, { children: `recovery drill · ${s.label || ''} · ${runId}` })
            ]
          }),
          jsxs('div', {
            'aria-live': 'polite',
            style: { display: 'flex', alignItems: 'center', gap: 10 },
            children: [
              running ? jsx(GlyphSpinner, { ariaLabel: 'Drill running' }) : null,
              jsx(Mono, { size: 12, color: running ? PRIMARY : MUTED, children: `${status} · ${at(elapsed)}` }),
              s.done ? jsx(VerdictLabel, { verdict: s.done.verdict }) : null
            ]
          })
        ]
      }),
      jsx(StageStrip, { s, failedAtEnd: s.cleanup && !s.cleanup.ok }),
      jsx(Controls, { p, finished }),
      jsxs('div', {
        style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 12, alignItems: 'start' },
        children: [jsx(BackupPanel, { s }, 'b'), jsx(NetworkPanel, { s, finished, reduced }, 'n'), jsx(ChecksPanel, { s, finished }, 'c')]
      }),
      jsx(EventLog, { events, t: p.t, follow: p.playing || running })
    ]
  })
}

export default {
  id: ID,
  name: 'RestoreProof',
  register(ctx) {
    ctxRef = ctx
    ctx.register({ id: 'pane', area: 'panes', title: 'restoreproof', data: { placement: 'right', width: '340px' }, render: () => jsx(Pane, {}) })
    ctx.register({ id: 'chip', area: 'statusBar.right', order: 140, render: () => jsx(Chip, {}) })
    ctx.register({ id: 'card', area: TRANSCRIPT_DIRECTIVE_AREA, data: { name: 'restoreproof', render: props => jsx(InlineCard, props) } })
    // Poll fast while a drill runs, every 3 s otherwise (cheap: it lists a few run folders), so the
    // drill view opens within moments of a drill starting.
    let tick = 0
    refresh()
    ctx.setInterval(() => {
      tick += 1
      if ($live.get() || tick % 2 === 0) refresh()
    }, 1500)
  }
}
