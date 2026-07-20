import { useCallback, useEffect, useRef, useState } from 'react'
import { motion } from 'framer-motion'
import PageShell, { OfflineBanner } from './PageShell'
import type { Page } from '../store'

// ── Mutate — COSMOS's self-modification panel. ────────────────────────────────
// It reads its own failure evidence (audit / traces / tool health), proposes
// fixes to its OWN codebase, and applies them with a test-gated hot apply that
// ends in the backend exec'ing itself. The page POLLS (no WS state) on purpose:
// polling rides straight through the self-restart gap, and the fetch failures
// during that gap ARE the "it's restarting" signal we render.

interface MutFile { path: string; action: string }
interface Mutation {
  id: string; title: string; diagnosis: string; fix_hint: string
  source: 'auto' | 'user'; area: string; confidence: number
  evidence: string[]; status: string; files: MutFile[]
  diff: string; log: string[]; error: string; note?: string
  created: string; updated: string
}

const btn: React.CSSProperties = {
  fontFamily: 'var(--font-d)', fontSize: 10, fontWeight: 700, letterSpacing: '0.12em',
  padding: '8px 16px', borderRadius: 4, cursor: 'pointer', border: '1px solid var(--border-hi)',
  background: 'var(--cyan-10)', color: 'var(--cyan)',
}
const btnGhost: React.CSSProperties = { ...btn, background: 'transparent', color: 'var(--text)' }

const STATUS_COLOR: Record<string, string> = {
  proposed: 'var(--cyan)', analyzing: 'var(--amber)', patching: 'var(--amber)',
  testing: 'var(--amber)', restarting: 'var(--amber)', applied: 'var(--green)',
  failed: 'var(--red)', rolled_back: 'var(--red)', dismissed: 'var(--text-lo)',
}
const PIPELINE = ['analyzing', 'patching', 'testing', 'restarting', 'applied']

function chip(color: string): React.CSSProperties {
  return {
    fontFamily: 'var(--font-d)', fontSize: 8.5, fontWeight: 700, letterSpacing: '0.14em',
    padding: '3px 8px', borderRadius: 3, color, border: `1px solid ${color}`,
    opacity: 0.95, whiteSpace: 'nowrap',
  }
}

function DiffView({ diff }: { diff: string }) {
  return (
    <pre style={{ background: 'var(--bg-deep)', border: '1px solid var(--border)',
      borderRadius: 6, padding: 12, margin: 0, overflowX: 'auto', maxHeight: 340,
      fontFamily: 'var(--font-m)', fontSize: 11, lineHeight: 1.5 }}>
      {diff.split('\n').map((l, i) => (
        <div key={i} style={{
          color: l.startsWith('+') && !l.startsWith('+++') ? 'var(--green)'
            : l.startsWith('-') && !l.startsWith('---') ? 'var(--red)'
            : l.startsWith('@@') ? 'var(--cyan)' : 'var(--text)' }}>{l || ' '}</div>
      ))}
    </pre>
  )
}

export default function MutatePage({ page, onNavigate }: {
  page?: Page; onNavigate?: (p: Page) => void
}) {
  const [muts, setMuts] = useState<Mutation[]>([])
  const [busyId, setBusyId] = useState<string | null>(null)
  const [offline, setOffline] = useState(false)
  const [restartGap, setRestartGap] = useState(false)
  const [scanning, setScanning] = useState(false)
  const [suggestion, setSuggestion] = useState('')
  const [openId, setOpenId] = useState<string | null>(null)
  const [status, setStatus] = useState<{ msg: string; ok: boolean } | null>(null)
  // Ref so the poll interval sees the latest "was a restart in flight" state.
  const restartingRef = useRef(false)

  const load = useCallback(() => {
    fetch('/api/mutate').then(r => r.ok ? r.json() : Promise.reject())
      .then(d => {
        const list: Mutation[] = d.mutations || []
        setMuts(list); setBusyId(d.busy || null); setOffline(false)
        const inFlight = list.some(m => m.status === 'restarting') || !!d.busy
        if (restartingRef.current && !inFlight) {
          const applied = list.find(m => m.status === 'applied')
          setStatus({ ok: true, msg: applied
            ? `✓ Mutation applied — COSMOS restarted itself and survived: “${applied.title}”`
            : 'COSMOS is back online.' })
        }
        restartingRef.current = inFlight
        setRestartGap(false)
      })
      .catch(() => {
        // Mid-restart the backend is briefly gone — that's the feature working,
        // not an outage. Only show the offline banner when nothing is in flight.
        if (restartingRef.current) setRestartGap(true)
        else setOffline(true)
      })
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, 2000)
    return () => clearInterval(t)
  }, [load])

  const scan = async () => {
    setScanning(true); setStatus(null)
    try {
      const r = await fetch('/api/mutate/scan', { method: 'POST' })
      const d = await r.json()
      setStatus({ ok: !d.error, msg: d.error || d.message || 'Scan complete.' })
    } catch { setStatus({ ok: false, msg: 'Scan failed — backend unreachable.' }) }
    setScanning(false); load()
  }

  const suggest = async () => {
    const text = suggestion.trim()
    if (!text) return
    const r = await fetch('/api/mutate/suggest', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    }).catch(() => null)
    if (r?.ok) { setSuggestion(''); setStatus({ ok: true, msg: 'Suggestion queued as a proposal — hit FIX to apply it.' }) }
    else setStatus({ ok: false, msg: 'Could not submit suggestion.' })
    load()
  }

  const fix = async (id: string) => {
    setStatus(null)
    const r = await fetch(`/api/mutate/${id}/fix`, { method: 'POST' }).catch(() => null)
    const d = await r?.json().catch(() => null)
    if (d?.error) setStatus({ ok: false, msg: d.error })
    else setOpenId(id)
    load()
  }

  const dismiss = async (id: string) => {
    await fetch(`/api/mutate/${id}/dismiss`, { method: 'POST' }).catch(() => null)
    load()
  }

  const visible = muts.filter(m => m.status !== 'dismissed')
  const active = visible.find(m => m.id === busyId || m.status === 'restarting')

  return (
    <PageShell title="Mutate" page={page} onNavigate={onNavigate}
      subtitle="SELF-HEALING · COSMOS REWRITES ITS OWN CODE"
      right={<button style={{ ...btn, opacity: scanning ? 0.5 : 1 }} disabled={scanning}
        onClick={scan}>{scanning ? 'SCANNING…' : '⟳ SCAN MY MISTAKES'}</button>}>

      {offline && <div style={{ marginBottom: 16 }}><OfflineBanner onRetry={load} /></div>}

      {restartGap && (
        <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }}
          style={{ background: 'rgba(255,149,0,0.06)', border: '1px solid rgba(255,149,0,0.4)',
            borderRadius: 8, padding: '16px 20px', marginBottom: 16, display: 'flex',
            alignItems: 'center', gap: 14 }}>
          <motion.span animate={{ rotate: 360 }} transition={{ repeat: Infinity, duration: 1.2, ease: 'linear' }}
            style={{ fontSize: 18, color: 'var(--amber)', display: 'inline-block' }}>⟳</motion.span>
          <div style={{ fontFamily: 'var(--font-b)', fontSize: 13, color: 'var(--text-hi)' }}>
            <b>COSMOS is restarting itself</b> to load the mutated code — same process, new brain.
            This page reconnects automatically.
          </div>
        </motion.div>
      )}

      {/* Suggest-a-mutation box */}
      <div style={{ background: 'var(--bg-card)', border: '1px solid var(--border-hi)',
        borderRadius: 8, padding: 16, marginBottom: 18 }}>
        <div style={{ fontFamily: 'var(--font-d)', fontSize: 10, fontWeight: 700,
          letterSpacing: '0.16em', color: 'var(--cyan)', marginBottom: 10 }}>
          ✦ SUGGEST A MUTATION
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
          <textarea value={suggestion} onChange={e => setSuggestion(e.target.value)}
            placeholder='Tell COSMOS what to change about itself — “make the orb pulse red when a tool fails”, “add retries to the weather fetch”…'
            onKeyDown={e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) suggest() }}
            style={{ flex: 1, minHeight: 52, resize: 'vertical', boxSizing: 'border-box',
              background: 'var(--bg-deep)', border: '1px solid var(--border)', borderRadius: 5,
              padding: '10px 12px', color: 'var(--text-hi)', fontFamily: 'var(--font-b)',
              fontSize: 13, outline: 'none' }} />
          <button style={{ ...btn, opacity: suggestion.trim() ? 1 : 0.5, whiteSpace: 'nowrap' }}
            disabled={!suggestion.trim()} onClick={suggest}>PROPOSE</button>
        </div>
        <div style={{ fontFamily: 'var(--font-m)', fontSize: 8.5, color: 'var(--text-lo)',
          marginTop: 8, letterSpacing: '0.08em' }}>
          Proposals are applied with a test-gated hot apply: backup → patch → py_compile →
          boot-check → pytest → (frontend) build → self-restart. Any gate failure rolls back. ⌘⏎ to propose.
        </div>
      </div>

      {status && (
        <div style={{ background: status.ok ? 'rgba(0,255,136,0.05)' : 'rgba(255,34,68,0.06)',
          border: `1px solid ${status.ok ? 'rgba(0,255,136,0.35)' : 'rgba(255,34,68,0.4)'}`,
          borderRadius: 8, padding: '12px 16px', marginBottom: 16,
          fontFamily: 'var(--font-b)', fontSize: 13,
          color: status.ok ? 'var(--green)' : 'var(--red)' }}>{status.msg}</div>
      )}

      {/* Live pipeline strip while a fix is in flight */}
      {active && active.status !== 'applied' && (
        <div style={{ background: 'var(--bg-card)', border: '1px solid var(--border-hi)',
          borderRadius: 8, padding: 16, marginBottom: 18 }}>
          <div style={{ fontFamily: 'var(--font-d)', fontSize: 10, fontWeight: 700,
            letterSpacing: '0.16em', color: 'var(--amber)', marginBottom: 12 }}>
            ⚡ MUTATING: {active.title}
          </div>
          <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
            {PIPELINE.map((s, i) => {
              const idx = PIPELINE.indexOf(active.status)
              const state = i < idx ? 'done' : i === idx ? 'now' : 'todo'
              return (
                <div key={s} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <span style={{ fontFamily: 'var(--font-d)', fontSize: 9.5, fontWeight: 700,
                    letterSpacing: '0.12em', padding: '5px 10px', borderRadius: 3,
                    color: state === 'done' ? 'var(--green)' : state === 'now' ? 'var(--amber)' : 'var(--text-lo)',
                    border: `1px solid ${state === 'done' ? 'rgba(0,255,136,0.35)' : state === 'now' ? 'rgba(255,149,0,0.5)' : 'var(--border)'}`,
                    background: state === 'now' ? 'rgba(255,149,0,0.07)' : 'transparent' }}>
                    {state === 'done' ? '✓ ' : ''}{s.toUpperCase()}
                  </span>
                  {i < PIPELINE.length - 1 && <span style={{ color: 'var(--text-lo)', fontSize: 10 }}>→</span>}
                </div>
              )
            })}
          </div>
          {(active.log || []).length > 0 && (
            <pre style={{ margin: '12px 0 0', maxHeight: 140, overflowY: 'auto',
              fontFamily: 'var(--font-m)', fontSize: 10.5, lineHeight: 1.6,
              color: 'var(--text)', whiteSpace: 'pre-wrap' }}>
              {(active.log || []).slice(-8).join('\n')}
            </pre>
          )}
        </div>
      )}

      {/* Proposal cards */}
      {visible.length === 0 && !offline && (
        <div style={{ fontFamily: 'var(--font-b)', fontSize: 13.5, color: 'var(--text-lo)',
          padding: '40px 0', textAlign: 'center', lineHeight: 1.8 }}>
          No mutations yet.<br />
          Hit <b style={{ color: 'var(--cyan)' }}>SCAN MY MISTAKES</b> to have COSMOS study its own
          activity log, or type a suggestion above.
        </div>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        {visible.map((m, i) => {
          const open = openId === m.id
          const fixable = ['proposed', 'failed', 'rolled_back'].includes(m.status) && !busyId
          const frontendOnly = m.status === 'applied' &&
            (m.files || []).every(f => f.path.startsWith('frontend/')) && (m.files || []).length > 0
          return (
            <motion.div key={m.id} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}
              transition={{ delay: i * 0.03 }} className="v2-card"
              style={{ padding: 18, cursor: 'pointer' }}
              onClick={() => setOpenId(open ? null : m.id)}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                <span style={chip(STATUS_COLOR[m.status] || 'var(--text)')}>{m.status.replace('_', ' ').toUpperCase()}</span>
                <span style={chip(m.source === 'auto' ? 'var(--amber)' : 'var(--cyan)')}>
                  {m.source === 'auto' ? '◉ SELF-DIAGNOSED' : '✦ USER'}</span>
                {m.area !== 'either' && <span style={chip('var(--text-lo)')}>{m.area.toUpperCase()}</span>}
                <span style={{ fontFamily: 'var(--font-d)', fontSize: 13.5, fontWeight: 700,
                  color: 'var(--text-hi)', flex: 1, minWidth: 200 }}>{m.title}</span>
                <div style={{ display: 'flex', gap: 8 }} onClick={e => e.stopPropagation()}>
                  {frontendOnly && <button style={btn} onClick={() => location.reload()}>↻ RELOAD HUD</button>}
                  {fixable && <button style={btn} onClick={() => fix(m.id)}>⚡ FIX</button>}
                  {!['analyzing', 'patching', 'testing', 'restarting'].includes(m.status) &&
                    <button style={{ ...btnGhost, padding: '8px 10px' }} onClick={() => dismiss(m.id)}>✕</button>}
                </div>
              </div>

              <div style={{ fontFamily: 'var(--font-b)', fontSize: 12.5, lineHeight: 1.6,
                color: 'var(--text)', marginTop: 10 }}>{m.diagnosis}</div>

              {open && (
                <div onClick={e => e.stopPropagation()} style={{ marginTop: 12 }}>
                  {m.fix_hint && (
                    <div style={{ fontFamily: 'var(--font-b)', fontSize: 12, color: 'var(--text)',
                      background: 'var(--bg-deep)', border: '1px solid var(--border)', borderRadius: 6,
                      padding: 12, marginBottom: 10 }}>
                      <b style={{ color: 'var(--cyan)' }}>Fix direction:</b> {m.fix_hint}
                    </div>
                  )}
                  {(m.evidence || []).length > 0 && (
                    <pre style={{ margin: '0 0 10px', fontFamily: 'var(--font-m)', fontSize: 10.5,
                      lineHeight: 1.6, color: 'var(--text-lo)', whiteSpace: 'pre-wrap',
                      borderLeft: '2px solid var(--border-hi)', paddingLeft: 10 }}>
                      {(m.evidence || []).join('\n')}
                    </pre>
                  )}
                  {(m.files || []).length > 0 && (
                    <div style={{ fontFamily: 'var(--font-m)', fontSize: 10.5, color: 'var(--text)',
                      marginBottom: 10 }}>
                      {(m.files || []).map(f => (
                        <span key={f.path} style={{ marginRight: 12 }}>
                          {f.action === 'created' ? '＋' : '±'} {f.path}
                        </span>
                      ))}
                    </div>
                  )}
                  {m.diff && <DiffView diff={m.diff} />}
                  {(m.log || []).length > 0 && (
                    <pre style={{ margin: '10px 0 0', maxHeight: 180, overflowY: 'auto',
                      fontFamily: 'var(--font-m)', fontSize: 10.5, lineHeight: 1.6,
                      color: 'var(--text-lo)', whiteSpace: 'pre-wrap' }}>
                      {(m.log || []).join('\n')}
                    </pre>
                  )}
                  {m.error && (
                    <pre style={{ margin: '10px 0 0', fontFamily: 'var(--font-m)', fontSize: 10.5,
                      lineHeight: 1.6, color: 'var(--red)', whiteSpace: 'pre-wrap' }}>{m.error}</pre>
                  )}
                </div>
              )}

              <div style={{ fontFamily: 'var(--font-m)', fontSize: 8.5, color: 'var(--text-lo)',
                letterSpacing: '0.08em', marginTop: 10 }}>
                {m.id} · {m.created}{m.confidence ? ` · confidence ${(m.confidence * 100) | 0}%` : ''}
                {open ? '  ·  click to collapse' : '  ·  click for evidence + diff'}
              </div>
            </motion.div>
          )
        })}
      </div>
    </PageShell>
  )
}
