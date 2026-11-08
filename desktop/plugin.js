/**
 * RestoreProof for Hermes Desktop.
 *
 *  - Pane: the drill running now (stage by stage) or the latest verdict, findings and history.
 *    With no drills yet it shows how to start: run the demo, or pick the app's folder and get a
 *    ready prompt in the composer. "New drill" brings the same choices back later.
 *  - Status bar chip: the last verdict at a glance.
 *  - Inline card: the model writes ::restoreproof{run="rp-..."} and the evidence renders in chat.
 *  - Report tab: the full case-file report, sandboxed, inside Hermes.
 *  - Drill view: a workspace tab that opens by itself when a drill starts. It answers first: six
 *    plain steps, one sentence on what is happening now, the sandbox as three boxes (database, your
 *    app, a test customer), then the verdict and the one fix. Backup files, checks, requests and the
 *    event log stay folded under "Technical details". The same renderer replays finished runs.
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

// -- getting started --------------------------------------------------------------------------------
//
// Two ways in, both ending in the chat so the user sees and sends every request themselves:
//   Run the demo   sends /drill demo (the user's click is the request; the drill view opens by itself)
//   Set up my app  asks for the app's folder with Desktop's own picker, then writes a plain prompt
//                  into the composer for the user to edit and send. The folder grant is given back
//                  at once: the plugin only needs the path, never access to the files.

const SETUP_PROMPT = folder =>
  `Set up a RestoreProof recovery drill for the app in ${folder}. Find its backups and its Docker image, ` +
  'ask me what a customer does in the app, write the recovery contract, show me the plan, then run the drill.'

async function startDemo() {
  haptic('tap')
  const sent = host.composer && host.composer.submit ? host.composer.submit(null, '/drill demo') : false
  if (!sent) host.notify({ kind: 'info', message: 'Open a chat and send /drill demo to run the demo drill.' })
}

async function startSetup() {
  haptic('tap')
  let folder = null
  try {
    if (ctxRef.files && ctxRef.files.requestFolder) {
      const r = await ctxRef.files.requestFolder()
      if (r && r.ok && r.folder) {
        folder = r.folder.path
        ctxRef.files.release(r.folder.id).catch(() => {})
      } else if (r && r.ok) {
        return // cancelled
      }
    }
  } catch {}
  const text = SETUP_PROMPT(folder || '<your app folder>')
  const put = host.composer && host.composer.setDraft ? await host.composer.setDraft(null, text) : false
  if (!put) host.notify({ kind: 'info', message: `Open a chat and ask: ${text}` })
}

function Need({ n, title, text }) {
  return jsxs('li', {
    style: { display: 'grid', gridTemplateColumns: '18px minmax(0, 1fr)', gap: 8, alignItems: 'baseline' },
    children: [
      jsx(Mono, { children: String(n) }),
      jsxs('span', {
        style: { minWidth: 0, font: `13px/1.5 ${SANS}`, color: MUTED },
        children: [jsx('span', { style: { color: FG, fontWeight: 600 }, children: title }), ` ${text}`]
      })
    ]
  })
}

function GetStarted({ compact }) {
  const art = useArt()
  return jsxs('section', {
    style: {
      background: SURFACE,
      border: `1px solid ${BORDER}`,
      borderRadius: RADIUS,
      overflow: 'hidden',
      display: 'flex',
      flexDirection: 'column',
      font: `14px/1.55 ${SANS}`,
      color: FG
    },
    children: [
      !compact && art ? jsx('img', { src: art, alt: '', style: { display: 'block', width: '100%', height: 120, objectFit: 'cover', objectPosition: 'center 58%' } }) : null,
      jsxs('div', {
        style: { padding: 16, display: 'flex', flexDirection: 'column', gap: 14 },
        children: [
          jsxs('div', {
            style: { display: 'flex', flexDirection: 'column', gap: 4 },
            children: [
              jsx('span', { style: { font: `600 16px/1.3 ${SANS}` }, children: compact ? 'Start a new drill' : 'Prove your app can come back' }),
              compact
                ? null
                : jsx('span', {
                    style: { font: `13px/1.5 ${SANS}`, color: MUTED },
                    children: 'A backup that says “success” is a claim. A drill restores it into a sealed sandbox, starts your app and tries it like a customer.'
                  })
            ]
          }),
          jsxs('div', {
            style: { display: 'flex', flexDirection: 'column', gap: 8 },
            children: [
              jsx(Button, { onClick: startDemo, children: 'Run the demo' }),
              jsx(Mono, { children: 'a billing app whose backup said “success” but lost 55 invoices · about a minute' }),
              jsx('div', { style: { height: 4 } }),
              jsx(Button, { variant: 'outline', onClick: startSetup, children: 'Set up my app…' }),
              jsx(Mono, { children: 'pick the app’s folder; Hermes finds the backups and asks the rest' })
            ]
          }),
          compact
            ? null
            : jsxs('div', {
                style: { display: 'flex', flexDirection: 'column', gap: 6, borderTop: `1px solid ${BORDER}`, paddingTop: 12 },
                children: [
                  jsx(Mono, { children: 'what you need' }),
                  jsxs('ol', {
                    style: { listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: 4 },
                    children: [
                      jsx(Need, { n: 1, title: 'Docker', text: 'running on this machine.' }, 1),
                      jsx(Need, { n: 2, title: 'A backup', text: 'folder: a database dump and file archives.' }, 2),
                      jsx(Need, { n: 3, title: 'Your app’s Docker image,', text: 'the one you deploy.' }, 3)
                    ]
                  })
                ]
              })
        ]
      })
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
  const [starting, setStarting] = useState(false)
  const latest = runs.find(r => r.verdict !== 'RUNNING')
  const showId = selected || (latest && latest.run_id)
  const card = showId ? cards[showId] : null
  let body
  if (error) body = jsx('div', { style: { color: FAIL, font: `13px/1.5 ${SANS}` }, children: error })
  else if (live && !selected) body = jsx(LiveDrill, { run: live })
  else if (card) body = jsx(EvidenceCard, { card, maxFindings: 6 })
  else if (!runs.length) body = jsx(GetStarted, {})
  else body = jsx(GlyphSpinner, { ariaLabel: 'Loading' })
  const hasRuns = runs.length > 0 && !error
  return jsxs('div', {
    style: { height: '100%', overflowY: 'auto', padding: 12, display: 'flex', flexDirection: 'column', gap: 16, boxSizing: 'border-box' },
    children: [
      body,
      hasRuns && starting ? jsx(GetStarted, { compact: true }) : null,
      hasRuns
        ? jsx('div', {
            children: jsx(Button, {
              size: 'sm',
              variant: 'outline',
              onClick: () => {
                haptic('tap')
                setStarting(v => !v)
              },
              children: starting ? 'Close' : 'New drill'
            })
          })
        : null,
      jsx(History, { runs, selected: showId })
    ]
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
        if (e.what === 'database') s.db = { tables: e.tables, rows: e.rows, units: e.units || ['tables', 'rows'] }
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
      return { text: `database restored · ${num(e.tables)} ${(e.units || ['tables'])[0]}, ${num(e.rows)} ${(e.units || [0, 'rows'])[1]}` }
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

const PLAIN_STAGE = {
  PREFLIGHT: 'Check the backup',
  PROVISIONING: 'Seal a sandbox',
  RESTORING: 'Restore',
  STARTING: 'Start the app',
  VALIDATING: 'Try it as a customer',
  CLEANING: 'Clean up'
}

const $art = atom(null) // the halftone emblem as a data URL; '' while loading or unavailable

function useArt() {
  const art = useValue($art)
  useEffect(() => {
    if ($art.get() !== null) return
    $art.set('')
    api('/art')
      .then(r => $art.set(`data:image/jpeg;base64,${r.jpeg}`))
      .catch(() => {})
  }, [])
  return art
}

// Six steps in plain words. The current one is marked; a failed one says so.
function Steps({ s, failedAtEnd }) {
  return jsx('ol', {
    'aria-label': 'Drill steps',
    style: { listStyle: 'none', margin: 0, padding: 0, display: 'grid', gridTemplateColumns: 'repeat(6, minmax(0, 1fr))', gap: 8 },
    children: STAGE_ORDER.map((st, i) => {
      const reached = s.stageAt[st] != null
      const current = s.stage === st && !s.done
      const failed = s.failedStages.has(st) || (st === 'CLEANING' && failedAtEnd)
      const color = failed ? FAIL : current ? PRIMARY : reached ? FG : MUTED
      return jsxs(
        'li',
        {
          'aria-current': current ? 'step' : undefined,
          style: {
            borderTop: `2px solid ${failed ? FAIL : current ? PRIMARY : reached ? MUTED : BORDER}`,
            paddingTop: 8,
            minWidth: 0,
            display: 'flex',
            flexDirection: 'column',
            gap: 2
          },
          children: [
            jsx(Mono, { children: failed ? `${i + 1} · failed` : reached && !current ? `${i + 1} · done` : `${i + 1}` }),
            jsx('span', { style: { font: `${current ? 600 : 400} 12px/1.35 ${SANS}`, color }, children: PLAIN_STAGE[st] })
          ]
        },
        st
      )
    })
  })
}

function activeFiles(s) {
  for (const [name, f] of Object.entries(s.files)) {
    const w = f.files
    if (w && w.done < w.total) return { name, ...w }
  }
  return null
}

// The one sentence that says what is happening right now, plus a progress bar while files restore.
function nowLine(s) {
  if (s.blocked) return { text: `The drill could not start: ${s.blocked}` }
  const steps = s.journeys.flatMap(j => j.steps.filter(Boolean))
  const last = steps[steps.length - 1]
  switch (s.stage) {
    case 'PROVISIONING':
      return { text: 'Building a sealed sandbox with no internet access.' }
    case 'RESTORING': {
      const f = activeFiles(s)
      if (f) return { text: `Restoring ${f.name}: ${num(f.done)} of ${num(f.total)} files.`, bar: f }
      if (s.db) return { text: `Database restored: ${num(s.db.rows)} ${s.db.units[1]} in ${num(s.db.tables)} ${s.db.units[0]}.` }
      return { text: 'Loading the database from the backup.' }
    }
    case 'STARTING':
      return { text: 'Starting your app on the restored data.' }
    case 'VALIDATING':
      if (last && last.request && !last.response) return { text: `A test customer asks for ${last.request.path}` }
      if (last && last.response) {
        const r = last.response
        return {
          text: r.ok
            ? `${last.request.path} answered ${r.status}, as a customer expects.`
            : `${last.request.path} answered ${r.status ?? 'nothing'}. A customer would see an error.`
        }
      }
      if (s.refs) {
        return {
          text: s.refs.missing
            ? `${num(s.refs.missing)} of the ${num(s.refs.total)} files the database points to did not come back.`
            : `All ${num(s.refs.total)} files the database points to came back.`
        }
      }
      return { text: 'Trying the app the way a customer would.' }
    case 'CLEANING':
      return {
        text: s.cleanup && s.cleanup.ok ? 'Everything the drill made is removed. Nothing is left running.' : 'Cleaning up: removing everything the drill made.'
      }
    default:
      return {
        text: s.artifacts.length
          ? `Checking the backup: ${s.artifacts.length} part${s.artifacts.length === 1 ? '' : 's'} found.`
          : 'Checking that the backup is all there.'
      }
  }
}

function Box({ title, state, fact, tone, gone }) {
  const color = tone === 'bad' ? FAIL : tone === 'ok' ? OK : tone === 'active' ? PRIMARY : MUTED
  return jsxs('div', {
    style: {
      border: `1px solid ${tone === 'bad' ? FAIL : BORDER}`,
      borderRadius: RADIUS,
      background: SURFACE,
      padding: '10px 12px',
      minWidth: 0,
      display: 'flex',
      flexDirection: 'column',
      gap: 4,
      opacity: gone ? 0.5 : 1,
      transition: prefersReducedMotion() ? 'none' : 'opacity 240ms ease'
    },
    children: [
      jsxs('div', {
        style: { display: 'flex', alignItems: 'center', gap: 6 },
        children: [
          jsx('span', { 'aria-hidden': true, style: { width: 7, height: 7, borderRadius: 99, flex: 'none', background: color } }),
          jsx('span', { style: { font: `600 13px/1.3 ${SANS}`, color: FG }, children: title })
        ]
      }),
      jsx(Mono, { color, children: state }),
      fact ? jsx('span', { style: { font: `12px/1.45 ${SANS}`, color: MUTED, overflowWrap: 'anywhere' }, children: fact }) : null
    ]
  })
}

// The sandbox as three boxes: the restored database, the app on top of it, and the test customer.
function Sandbox({ s, finished }) {
  const n = s.nodes
  const net = s.net
  const boot = s.checks.find(c => c.kind === 'service')
  const answered = s.journeys.flatMap(j => j.steps.filter(x => x && x.response))
  const okCount = answered.filter(x => x.response.ok).length
  const bad = answered.find(x => !x.response.ok)
  const legacy = finished && !s.hasResources && !s.blocked
  const word = node =>
    !node ? (legacy ? 'not recorded' : 'waiting') : node.status === 'removed' ? 'removed' : node.status === 'started' ? 'running' : 'starting'
  const live = node => node && node.status !== 'removed'
  const arrow = key => jsx('span', { 'aria-hidden': true, style: { alignSelf: 'center', textAlign: 'center', color: MUTED, font: `13px ${MONO}` }, children: '→' }, key)
  return jsxs('section', {
    'aria-label': 'Sandbox',
    style: {
      border: `1px dashed ${net && net.status !== 'removed' ? MUTED : BORDER}`,
      borderRadius: RADIUS,
      padding: 12,
      display: 'flex',
      flexDirection: 'column',
      gap: 10
    },
    children: [
      jsxs('div', {
        style: { display: 'flex', justifyContent: 'space-between', gap: 8, flexWrap: 'wrap' },
        children: [
          jsx(Mono, { children: 'sealed sandbox · no internet, no production' }),
          jsx(Mono, {
            children: !net ? (s.blocked ? 'never built' : legacy ? '' : 'not built yet') : net.status === 'removed' ? 'removed and re-checked' : 'open'
          })
        ]
      }),
      jsxs('div', {
        style: { display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 16px minmax(0, 1fr) 16px minmax(0, 1fr)', gap: 6, alignItems: 'stretch' },
        children: [
          jsx(
            Box,
            {
              title: 'Database',
              state: word(n.database),
              fact: s.db ? `${num(s.db.rows)} ${s.db.units[1]} restored` : n.database && n.database.status === 'started' ? 'loading the backup…' : null,
              tone: s.db ? 'ok' : live(n.database) ? 'active' : null,
              gone: n.database && n.database.status === 'removed'
            },
            'db'
          ),
          arrow('a1'),
          jsx(
            Box,
            {
              title: 'Your app',
              state: boot ? (boot.status !== 'PASS' ? 'did not start' : live(n.app) || !n.app ? 'up and answering' : 'removed') : word(n.app),
              fact: boot && boot.status !== 'PASS' ? boot.summary : null,
              tone: boot ? (boot.status === 'PASS' ? 'ok' : 'bad') : live(n.app) ? 'active' : null,
              gone: n.app && n.app.status === 'removed'
            },
            'app'
          ),
          arrow('a2'),
          jsx(
            Box,
            {
              title: 'Test customer',
              state: answered.length ? `${okCount} of ${answered.length} request${answered.length === 1 ? '' : 's'} worked` : word(n.probe),
              fact: bad ? `${bad.request.path} → ${bad.response.status ?? 'no reply'}` : null,
              tone: bad ? 'bad' : answered.length ? 'ok' : live(n.probe) ? 'active' : null,
              gone: n.probe && n.probe.status === 'removed'
            },
            'probe'
          )
        ]
      })
    ]
  })
}

// The answer, once the drill is over: the verdict in words, the plain headline, the one fix.
function Verdict({ s, finished }) {
  const cards = useValue($cards)
  const doneId = s.done && s.done.run_id
  useEffect(() => {
    if (finished && doneId) loadCard(doneId).catch(() => {})
  }, [finished, doneId])
  const card = doneId ? cards[doneId] : null
  const v = s.done.verdict
  const color = VERDICT_COLOR[v] || MUTED
  const headline = (card && card.headline) || sentence(s.done.reason)
  return jsxs('section', {
    'aria-live': 'polite',
    style: { display: 'flex', flexDirection: 'column', gap: 12 },
    children: [
      jsxs('div', {
        style: { display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' },
        children: [
          jsx('span', { 'aria-hidden': true, style: { width: 9, height: 9, borderRadius: 99, background: color } }),
          jsx('span', { style: { font: `600 13px/1.3 ${SANS}`, color }, children: (card && card.status) || v }),
          jsx(VerdictLabel, { verdict: v })
        ]
      }),
      jsx('p', { style: { margin: 0, font: `600 20px/1.35 ${SANS}`, color: FG, textWrap: 'pretty' }, children: headline }),
      s.refs && s.refs.missing ? jsx(RefGrid, { refs: s.refs }) : null,
      card && card.next_step
        ? jsxs('div', {
            style: { border: `1px solid ${color}`, borderRadius: RADIUS, padding: '10px 12px', display: 'flex', flexDirection: 'column', gap: 4 },
            children: [jsx(Mono, { color, children: 'fix this first' }), jsx('span', { style: { font: `13px/1.5 ${SANS}`, color: FG }, children: card.next_step })]
          })
        : null,
      finished
        ? jsx('div', {
            children: jsx(Button, { size: 'sm', onClick: () => loadCard(s.done.run_id).then(openReport).catch(() => {}), children: 'Open report' })
          })
        : null
    ]
  })
}

function NowPanel({ s }) {
  const now = nowLine(s)
  return jsxs('section', {
    'aria-live': 'polite',
    style: { display: 'flex', flexDirection: 'column', gap: 8, minHeight: 64 },
    children: [
      jsx(Mono, { children: s.stage ? `now · ${PLAIN_STAGE[s.stage].toLowerCase()}` : 'now' }),
      jsx('p', { style: { margin: 0, font: `600 20px/1.35 ${SANS}`, color: s.blocked ? FAIL : FG, textWrap: 'pretty', overflowWrap: 'anywhere' }, children: now.text }),
      now.bar ? jsx(Bar, { done: now.bar.done, total: now.bar.total, active: true }) : null
    ]
  })
}

function RequestsPanel({ s, reduced }) {
  const steps = s.journeys.flatMap(j => j.steps.filter(Boolean))
  return jsx(Panel, {
    title: 'Test customer requests',
    aside: s.net ? s.net.name : null,
    children: steps.length
      ? jsx('ol', {
          'aria-label': 'Probe requests',
          style: { listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: 10 },
          children: steps.map((step, i) => jsx(RequestRow, { step, index: i, reduced }, i))
        })
      : jsx(Mono, { children: s.done ? 'no requests were made' : 'requests start once the app is up' })
  })
}

// Everything an engineer wants and nobody else needs, folded until asked for.
function Technical({ s, events, t, follow, reduced }) {
  const [open, setOpen] = useState(false)
  return jsxs('details', {
    open,
    onToggle: e => setOpen(e.currentTarget.open),
    style: { borderTop: `1px solid ${BORDER}`, paddingTop: 12 },
    children: [
      jsxs('summary', {
        style: { cursor: 'pointer', font: `600 13px/1.4 ${SANS}`, color: FG },
        children: ['Technical details ', jsx(Mono, { children: '· backup files, checks, requests, event log' })]
      }),
      open
        ? jsxs('div', {
            style: { display: 'flex', flexDirection: 'column', gap: 12, marginTop: 12 },
            children: [
              jsxs('div', {
                style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 12, alignItems: 'start' },
                children: [jsx(BackupPanel, { s }, 'b'), jsx(ChecksPanel, { s }, 'c'), jsx(RequestsPanel, { s, reduced }, 'r')]
              }),
              jsx(EventLog, { events, t, follow })
            ]
          })
        : null
    ]
  })
}

function ChecksPanel({ s }) {
  const failed = s.checks.filter(c => c.mandatory && (c.status === 'FAIL' || c.status === 'ERROR'))
  const passed = s.checks.filter(c => c.status === 'PASS')
  return jsx(Panel, {
    title: 'Checks',
    aside: s.checks.length ? `${passed.length} passed · ${failed.length} failed` : null,
    children: jsxs('div', {
      style: { display: 'flex', flexDirection: 'column', gap: 14 },
      children: [
        s.checks.length
          ? jsx('ul', {
              style: { listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: 6, fontSize: 13 },
              children: s.checks.map(c => jsx(CheckLine, { ok: c.status === 'PASS', text: sentence(c.name), fact: c.status === 'PASS' ? null : c.summary }, c.id))
            })
          : jsx(Mono, { children: s.done ? 'no checks ran' : 'checks run after the app starts' })
      ]
    })
  })
}

function DrillView({ runId, live }) {
  const { events, finished, error, loaded, arrivedAt } = useRunEvents(runId)
  const p = usePlayer(events, finished, live)
  const s = useMemo(() => reduceEvents(events, p.t), [events, p.t])
  const art = useArt()
  const reduced = prefersReducedMotion()
  const [, setNow] = useState(0)
  const running = !finished && p.mode === 'live'
  useEffect(() => {
    if (!running) return undefined
    return ctxRef.setInterval(() => setNow(n => n + 1), 500)
  }, [running])
  if (error && !events.length) {
    // A routing 404 ({"detail":"Not Found"}) means this Desktop half is newer than the running
    // backend; "no such run" is a real missing run. Say which, in words.
    const stale = /Not Found/.test(String(error)) && !/no such run/.test(String(error))
    const text = stale
      ? 'RestoreProof was updated but Hermes is still running the previous version. Restart Hermes to finish the update, then replay this drill.'
      : /no such run/.test(String(error))
        ? 'This drill is not on this Hermes host.'
        : `Could not load the drill’s events: ${error}`
    return jsx('div', { style: { padding: 20, color: stale ? MUTED : FAIL, font: `13px/1.5 ${SANS}` }, children: text })
  }
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
      gap: 18,
      font: `14px/1.55 ${SANS}`,
      color: FG,
      maxWidth: 880
    },
    children: [
      jsxs('header', {
        style: { display: 'flex', alignItems: 'center', gap: 14, minWidth: 0 },
        children: [
          art ? jsx('img', { src: art, alt: '', style: { height: 52, width: 'auto', borderRadius: RADIUS, flex: 'none', display: 'block' } }) : null,
          jsxs('div', {
            style: { display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0, flex: 1 },
            children: [
              jsx('span', { style: { font: `600 18px/1.3 ${SANS}`, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, children: s.app || 'Recovery drill' }),
              jsx(Mono, { style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, children: `recovery drill${s.label ? ` · backup ${s.label}` : ''}` })
            ]
          }),
          jsxs('div', {
            style: { display: 'flex', alignItems: 'center', gap: 8, flex: 'none' },
            children: [
              running ? jsx(GlyphSpinner, { ariaLabel: 'Drill running' }) : null,
              jsx(Mono, { size: 12, color: running ? PRIMARY : MUTED, children: `${status} · ${secs(elapsed)}` })
            ]
          })
        ]
      }),
      jsx(Steps, { s, failedAtEnd: s.cleanup && !s.cleanup.ok }),
      s.done ? jsx(Verdict, { s, finished }) : jsx(NowPanel, { s }),
      jsx(Sandbox, { s, finished }),
      jsx(Controls, { p, finished }),
      jsx(Technical, { s, events, t: p.t, follow: p.playing || running, reduced })
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
