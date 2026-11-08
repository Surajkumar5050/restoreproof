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
 * theme's runtime tokens (--dt-card, --dt-border, --dt-foreground, --dt-muted-foreground,
 * --dt-destructive, --dt-font-sans, --dt-font-mono, --radius-scalar). Pass and partial use the app's
 * status green and yellow (--ui-green, --ui-yellow).
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

// Desktop compiles its --color-* names into utility classes (Tailwind's @theme inline), so they do not
// exist at runtime; the live tokens are --dt-* and --ui-*. Each name falls back to the next.
const FG = 'var(--dt-foreground, var(--color-foreground, currentColor))'
const MUTED = 'var(--dt-muted-foreground, var(--color-muted-foreground, currentColor))'
const BORDER = 'var(--dt-border, var(--ui-stroke-secondary, var(--color-border, rgba(127, 127, 127, 0.3))))'
const SURFACE = 'var(--dt-card, var(--ui-bg-editor, var(--color-card, transparent)))'
const FAIL = 'var(--dt-destructive, var(--ui-red, var(--color-destructive, #cf2d56)))'
const OK = 'var(--ui-green, #4caf50)'
const WARN = 'var(--ui-yellow, #ffa726)'
const PRIMARY = 'var(--dt-primary, var(--ui-blue, var(--color-primary, #0053fd)))'
const SANS = 'var(--dt-font-sans, var(--font-sans, system-ui, sans-serif))'
const MONO = 'var(--dt-font-mono, var(--font-mono, ui-monospace, monospace))'
const RADIUS = 'calc(var(--radius-scalar, 1) * 0.625rem)'

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
      !compact && art ? jsx('img', { src: art, alt: '', style: { display: 'block', width: '100%', height: 120, objectFit: 'cover' } }) : null,
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

// The halftone Hermes emblem, inlined so it needs no request. Written by scripts/build_halftone_art.py.
const EMBLEM = 'data:image/jpeg;base64,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DyUxs6rERIxZREnfqMmwlUTxv/eMFopEembdYEcZAbl05jHk1DvEit+M+eHBInxjBS0fxRfnCMTXlIwSJA1eCFCWrlShCwjkvv4BB93v9FIggbqZL8Yr2icRc1t6wF3QrfUvbKtYM6/3lKEAGsa6wZRiDrFvilO5+cUiiW6I9RGNZMFpakBDEviil15j8/R+J8p1ZzyOj+tOcpUUScFjvGVZMyGG36sJKpCkF4exlcEgeDWLrMRD3YpknESdJSTTjxGS6ynit5LnKCINN098aimwoBHLxhoFh5FduH1TuXJOTR8aMMn2nJ9rWVNISrjxgQDQjzmmvaQpynEmCIHsoC8V1hqDWhqVs7yy/t5pxoaiqSY375uFUJWzjrzkyfADS4y1Au9niU0EOIFaKnhxGbFLksdTtwrxJBPCmyMBpIzm1yK1XqnVugbKn/mHCkCkv8mKB77+oo6E5CSTKbul4lyerscJDDvF9hG8SQBBtJ88g3UPYmIfbOY56BzayYVoZV7d1icbRYpF+zHtTaOWwjfqhiii0PuffJpSUOM7yUkfQuin8YkNXuLqfjFgnLish5X37ZLT1fQQRG739OWRVJau1sh/W4uJyMYFwBn5R84g7z22bJ34xEMZBxJ5X2cDTpSHLeqL9UcJA02SZZGTfCecb1KYmq61tXh1kgta7dfnPd3i3w6yQ6pcXxgwzi/qKj4Os6g23lDADWEfUGRIRKlN17f5P//aAAwDAQACAAMAAAAQ99999999999992kDfGR729u++++++++++999999999999U7476ZnKz899999999999999999999999b5leFvvhtL89999999999999999999992bKCEXlZKnEb+9999999999999999999qauEG/JlPmIRbc9999999999999999999+I7AAS7zwcqmR99999999999999999999999stSQhGuD2V9999999999999999999999qB56yNNNte29999999999999999999999t9+/I8cJ8k999999999999999999999999S69va3+4+999999999999999999999999uVQDw9W229999999999999999999999999npxZcbTc99999999999/8QAJhEAAgIBAwMEAwEAAAAAAAAAAAER8CExQEFR4fEQMGGhcYHR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'

function useArt() {
  return EMBLEM
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
          art ? jsx('img', { src: art, alt: '', style: { height: 52, width: 52, objectFit: 'cover', borderRadius: RADIUS, flex: 'none', display: 'block' } }) : null,
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
