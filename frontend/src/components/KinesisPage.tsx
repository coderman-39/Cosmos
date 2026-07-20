import { useEffect, useRef, useState, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import PageShell, { OfflineBanner } from './PageShell'
import type { Page } from '../store'

// ── Kinesis: demonstrate-once macro recorder. Record a GUI chore by hand, name
// it, and replay it forever. Mirrors the Skills page chrome (PageShell + card
// grid) with a record → review → save flow and a countdown-guarded replay. ──

interface MacroMeta {
  name: string; title: string; description: string
  steps: number; duration_ms: number; created: string
}
interface Param { name: string; value: string }
interface MacroFull extends Omit<MacroMeta, 'steps'> {
  steps: any[]; describe: string[]; params?: Param[]
}
interface RecStatus { recording: boolean; events: number; error: string | null; elapsed_ms: number }

const btn: React.CSSProperties = {
  fontFamily: 'var(--font-d)', fontSize: 10, fontWeight: 700, letterSpacing: '0.12em',
  padding: '8px 16px', borderRadius: 4, cursor: 'pointer', border: '1px solid var(--border-hi)',
  background: 'var(--cyan-10)', color: 'var(--cyan)',
}
const btnGhost: React.CSSProperties = { ...btn, background: 'transparent', color: 'var(--text)' }
const btnRec: React.CSSProperties = { ...btn, background: 'rgba(255,34,68,0.12)', color: 'var(--red)',
  borderColor: 'rgba(255,34,68,0.45)' }

function noteBox(color: string): React.CSSProperties {
  return { fontFamily: 'var(--font-b)', fontSize: 12, color, background: 'rgba(0,212,255,0.05)',
    border: `1px solid ${color === 'var(--cyan)' ? 'var(--border)' : color}`, borderRadius: 6,
    padding: '10px 14px', marginBottom: 14 }
}
const fmtDur = (ms: number) => ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`

type View = 'list' | 'recording' | 'review' | 'detail'

export default function KinesisPage({ page, onNavigate }: {
  page?: Page; onNavigate?: (p: Page) => void
}) {
  const [view, setView] = useState<View>('list')
  const [list, setList] = useState<MacroMeta[]>([])
  const [offline, setOffline] = useState(false)
  const [status, setStatus] = useState<{ msg: string; ok: boolean } | null>(null)

  // recording
  const [rec, setRec] = useState<RecStatus>({ recording: false, events: 0, error: null, elapsed_ms: 0 })
  const [countdown, setCountdown] = useState(0)          // 3..1 pre-record, 0 = live
  const pollRef = useRef<number | null>(null)
  const sawRecording = useRef(false)

  // review / save
  const [capSteps, setCapSteps] = useState<any[]>([])
  const [capDesc, setCapDesc] = useState<string[]>([])
  const [capDur, setCapDur] = useState(0)
  const [name, setName] = useState('')
  const [title, setTitle] = useState('')
  const [desc, setDesc] = useState('')
  const [busy, setBusy] = useState(false)
  const [understanding, setUnderstanding] = useState(false)
  const [cdp, setCdp] = useState<{ available: boolean } | null>(null)
  const [cdpBusy, setCdpBusy] = useState(false)

  // detail / replay
  const [sel, setSel] = useState<MacroFull | null>(null)
  const [replayFor, setReplayFor] = useState<{ name: string; title: string } | null>(null)
  const [replayCount, setReplayCount] = useState(0)
  const [runForm, setRunForm] = useState<{ name: string; title: string; defs: Param[] } | null>(null)
  const [eParams, setEParams] = useState<Param[]>([])
  const [editSteps, setEditSteps] = useState<any[] | null>(null)
  const [editDescribe, setEditDescribe] = useState<string[]>([])
  const [stepsBusy, setStepsBusy] = useState(false)

  // detail editing
  const [editing, setEditing] = useState(false)
  const [eName, setEName] = useState('')
  const [eTitle, setETitle] = useState('')
  const [eDesc, setEDesc] = useState('')
  const [eBusy, setEBusy] = useState(false)

  const loadList = useCallback(() => {
    fetch('/api/kinesis').then(r => r.ok ? r.json() : Promise.reject())
      .then(d => { setList(d.macros || []); setOffline(false) })
      .catch(() => setOffline(true))
  }, [])
  const loadCdp = useCallback(() => {
    fetch('/api/kinesis/cdp').then(r => r.json()).then(setCdp).catch(() => {})
  }, [])
  useEffect(() => {
    loadList(); loadCdp()
    // Poll CDP status so it flips to ON automatically once Chrome has relaunched
    // with the debug port (the HUD reloads during that relaunch).
    const iv = window.setInterval(loadCdp, 4000)
    return () => window.clearInterval(iv)
  }, [loadList, loadCdp])
  useEffect(() => () => { if (pollRef.current) window.clearInterval(pollRef.current) }, [])

  const enableTurbo = async () => {
    if (!confirm('Turbo opens a DEDICATED Kinesis Chrome window (Chrome blocks debugging your '
      + 'main profile). Your main Chrome is untouched — just do your chores in the new window '
      + 'and sign into sites there once. Continue?')) return
    setCdpBusy(true); setStatus({ msg: 'Opening the turbo browser…', ok: true })
    const r = await fetch('/api/kinesis/cdp/enable', { method: 'POST' })
      .then(x => x.json()).catch(() => ({ ok: false, message: 'Network error' }))
    setCdpBusy(false); setStatus({ msg: r.message, ok: !!r.ok }); loadCdp()
  }

  // ── recording control ───────────────────────────────────────────────────
  const beginCountdown = () => {
    setStatus(null); sawRecording.current = false; setCountdown(3); setView('recording')
    let n = 3
    const iv = window.setInterval(() => {
      n -= 1
      setCountdown(n)
      if (n <= 0) { window.clearInterval(iv); actuallyStart() }
    }, 800)
  }

  const actuallyStart = async () => {
    const r = await fetch('/api/kinesis/record/start', { method: 'POST' })
      .then(x => x.json()).catch(() => ({ ok: false, error: 'Network error' }))
    if (!r.ok) {
      setStatus({ msg: r.error || 'Could not start recording.', ok: false })
      setView('list'); return
    }
    pollRef.current = window.setInterval(async () => {
      const s: RecStatus = await fetch('/api/kinesis/status').then(x => x.json()).catch(() => rec)
      setRec(s)
      if (s.recording) sawRecording.current = true
      // ⌥⎋ stopped the capture on the backend — finalize and move to review.
      if (sawRecording.current && !s.recording) finishRecording(false)
    }, 700)
  }

  const finishRecording = async (viaButton: boolean) => {
    if (pollRef.current) { window.clearInterval(pollRef.current); pollRef.current = null }
    const r = await fetch('/api/kinesis/record/stop', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ via_button: viaButton }),
    }).then(x => x.json()).catch(() => ({ ok: false }))
    if (!r.ok || !r.count) {
      setStatus({ msg: 'No steps were captured — nothing to save.', ok: false })
      setView('list'); return
    }
    setCapSteps(r.steps || []); setCapDesc(r.describe || []); setCapDur(r.duration_ms || 0)
    setName(''); setTitle(''); setDesc(''); setView('review')
    runUnderstand(r.steps || [])       // auto-infer the intent + name
  }

  // Ask the model to read the steps and infer what the macro accomplishes.
  const runUnderstand = async (steps: any[]) => {
    if (!steps.length) return
    setUnderstanding(true)
    const u = await fetch('/api/kinesis/understand', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ steps }),
    }).then(x => x.json()).catch(() => ({ ok: false }))
    setUnderstanding(false)
    if (u.ok) { setName(u.name || ''); setTitle(u.title || ''); setDesc(u.description || '') }
  }

  // ── save ──────────────────────────────────────────────────────────────────
  const saveMacro = async () => {
    const n = name.trim().toLowerCase()
    if (!/^[a-z0-9][a-z0-9-]{1,40}$/.test(n)) {
      setStatus({ msg: 'Name must be kebab-case (a-z, 0-9, dashes).', ok: false }); return
    }
    setBusy(true)
    const r = await fetch('/api/kinesis/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: n, title: title.trim() || n, description: desc.trim(),
        steps: capSteps, duration_ms: capDur }),
    }).then(x => x.json()).catch(() => ({ ok: false, message: 'Network error' }))
    setBusy(false)
    if (r.ok) { setStatus({ msg: r.message, ok: true }); setView('list'); loadList() }
    else setStatus({ msg: r.message || 'Save failed.', ok: false })
  }

  // ── detail / delete ─────────────────────────────────────────────────────
  const openDetail = async (nm: string) => {
    const r = await fetch(`/api/kinesis/${nm}`)
    if (!r.ok) { setStatus({ msg: 'Could not load macro.', ok: false }); return }
    setSel(await r.json()); setStatus(null); setEditing(false); setEditSteps(null); setView('detail')
  }

  const startStepsEdit = () => {
    if (!sel) return
    setEditSteps([...sel.steps]); setEditDescribe([...(sel.describe || [])])
  }
  const moveStep = (i: number, dir: -1 | 1) => {
    const j = i + dir
    if (!editSteps || j < 0 || j >= editSteps.length) return
    const s = [...editSteps]; const d = [...editDescribe]
    const ts = s[i]; s[i] = s[j]; s[j] = ts
    const td = d[i]; d[i] = d[j]; d[j] = td
    setEditSteps(s); setEditDescribe(d)
  }
  const deleteStep = (i: number) => {
    if (!editSteps) return
    setEditSteps(editSteps.filter((_, j) => j !== i))
    setEditDescribe(editDescribe.filter((_, j) => j !== i))
  }
  const saveSteps = async () => {
    if (!sel || !editSteps) return
    setStepsBusy(true)
    const r = await fetch(`/api/kinesis/${sel.name}/edit`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ steps: editSteps }),
    }).then(x => x.json()).catch(() => ({ ok: false, message: 'Network error' }))
    setStepsBusy(false)
    if (r.ok) { setEditSteps(null); loadList(); openDetail(sel.name) }
    else setStatus({ msg: r.message || 'Save failed.', ok: false })
  }

  const startEdit = () => {
    if (!sel) return
    setEName(sel.name); setETitle(sel.title); setEDesc(sel.description || '')
    setEParams((sel.params || []).map(p => ({ ...p }))); setEditing(true)
  }
  const saveEdit = async () => {
    if (!sel) return
    const nn = eName.trim().toLowerCase()
    if (!/^[a-z0-9][a-z0-9-]{1,40}$/.test(nn)) {
      setStatus({ msg: 'Name must be kebab-case (a-z, 0-9, dashes).', ok: false }); return
    }
    setEBusy(true)
    const r = await fetch(`/api/kinesis/${sel.name}/edit`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: nn, title: eTitle.trim(), description: eDesc.trim(),
        params: eParams.filter(p => p.name.trim() && p.value.trim()) }),
    }).then(x => x.json()).catch(() => ({ ok: false, message: 'Network error' }))
    setEBusy(false)
    if (r.ok) { setEditing(false); loadList(); openDetail(r.name || sel.name) }
    else setStatus({ msg: r.message || 'Save failed.', ok: false })
  }
  const deleteMacro = async (nm: string) => {
    if (!confirm(`Delete macro "${nm}"?`)) return
    const r = await fetch(`/api/kinesis/${nm}`, { method: 'DELETE' })
      .then(x => x.json()).catch(() => ({ ok: false }))
    if (r.ok) { setSel(null); setView('list'); loadList() }
    else setStatus({ msg: r.message || 'Delete failed.', ok: false })
  }

  // ── replay (countdown → drive input) ──────────────────────────────────────
  const doReplay = (nm: string, ttl: string, params: Record<string, string>) => {
    setReplayFor({ name: nm, title: ttl }); setReplayCount(3)
    let n = 3
    const iv = window.setInterval(async () => {
      n -= 1; setReplayCount(n)
      if (n <= 0) {
        window.clearInterval(iv); setReplayCount(0)
        const r = await fetch(`/api/kinesis/${nm}/replay`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ params }),
        }).then(x => x.json()).catch(() => ({ ok: false, message: 'Network error' }))
        setReplayFor(null)
        setStatus({ msg: r.message || (r.ok ? 'Replayed.' : 'Replay failed.'), ok: !!r.ok })
      }
    }, 800)
  }
  // If the macro has variables, collect values first; otherwise run straight away.
  const runMacro = async (nm: string, ttl: string) => {
    const m = await fetch(`/api/kinesis/${nm}`).then(r => r.json()).catch(() => null)
    const defs: Param[] = (m && m.params) || []
    if (defs.length) setRunForm({ name: nm, title: ttl, defs: defs.map(d => ({ ...d })) })
    else doReplay(nm, ttl, {})
  }

  const headerRight = view === 'list'
    ? <button style={btnRec} onClick={beginCountdown}>● RECORD NEW</button>
    : <button style={btnGhost} onClick={() => { setView('list'); setStatus(null) }}>← ALL MACROS</button>

  const subtitle = view === 'list' ? `${list.length} recorded macro${list.length === 1 ? '' : 's'}`
    : view === 'recording' ? 'capturing your actions'
    : view === 'review' ? 'name it, then save' : sel ? `${sel.steps.length} steps` : ''

  return (
    <PageShell title="Kinesis" page={page} onNavigate={onNavigate} subtitle={subtitle} right={headerRight}>
      {offline && <div style={{ marginBottom: 16 }}><OfflineBanner onRetry={loadList} /></div>}
      {status && <div style={noteBox(status.ok ? 'var(--cyan)' : 'var(--red)')}>{status.msg}</div>}

      {/* ── Turbo (CDP) status ───────────────────────────────────────────── */}
      {view === 'list' && cdp && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 16,
          fontFamily: 'var(--font-m)', fontSize: 11, letterSpacing: '0.06em' }}>
          <span style={{ width: 7, height: 7, borderRadius: '50%',
            background: cdp.available ? 'var(--cyan)' : 'var(--text-lo)',
            boxShadow: cdp.available ? 'var(--glow-xs)' : 'none' }} />
          <span style={{ color: cdp.available ? 'var(--cyan)' : 'var(--text-lo)' }}>
            ⚡ TURBO (CDP): {cdp.available
              ? 'ON for recording — replay always runs in your own Chrome'
              : 'OFF — recording & replay both use your own Chrome'}
          </span>
          {!cdp.available && (
            <button style={{ ...btnGhost, opacity: cdpBusy ? 0.5 : 1, padding: '5px 12px' }}
              disabled={cdpBusy} onClick={enableTurbo}>{cdpBusy ? 'RELAUNCHING…' : 'ENABLE'}</button>
          )}
        </div>
      )}

      {/* ── LIST ─────────────────────────────────────────────────────────── */}
      {view === 'list' && (
        list.length === 0 && !offline ? (
          <div style={{ ...noteBox('var(--cyan)'), lineHeight: 1.7 }}>
            No macros yet. Click <b style={{ color: 'var(--red)' }}>● RECORD NEW</b>, do a repetitive
            chore by hand once, and Kinesis will save it as a one-click macro. Stop anytime with
            <b> ⌥⎋ (Option-Escape)</b>.
          </div>
        ) : (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: 16 }}>
            {list.map((m, i) => (
              <motion.div key={m.name} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}
                transition={{ delay: i * 0.03 }} whileHover={{ y: -4 }}
                className="skill-card v2-card v2-glow" style={{ padding: 20 }}>
                <div style={{ position: 'relative', zIndex: 1, display: 'flex', alignItems: 'center', gap: 9, marginBottom: 8 }}>
                  <span style={{ color: 'var(--cyan)', fontSize: 14, textShadow: 'var(--glow-xs)', lineHeight: 1 }}>◎</span>
                  <span style={{ fontFamily: 'var(--font-d)', fontSize: 13, fontWeight: 700, color: 'var(--text-hi)', flex: 1 }}>{m.title}</span>
                  <button title="Delete macro" onClick={e => { e.stopPropagation(); deleteMacro(m.name) }}
                    style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0, color: 'var(--text-lo)', fontSize: 14, lineHeight: 1 }}
                    onMouseEnter={e => (e.currentTarget.style.color = 'var(--red)')}
                    onMouseLeave={e => (e.currentTarget.style.color = 'var(--text-lo)')}>✕</button>
                </div>
                {m.description && <div style={{ position: 'relative', zIndex: 1, fontFamily: 'var(--font-b)', fontSize: 12, lineHeight: 1.5, color: 'var(--text)', marginBottom: 10, minHeight: 34 }}>{m.description}</div>}
                <div style={{ position: 'relative', zIndex: 1, fontFamily: 'var(--font-m)', fontSize: 8.5, color: 'var(--text-lo)', letterSpacing: '0.08em', marginBottom: 12 }}>
                  {m.steps} steps · {fmtDur(m.duration_ms)}
                </div>
                <div style={{ position: 'relative', zIndex: 1, display: 'flex', gap: 8 }}>
                  <button style={btn} onClick={() => runMacro(m.name, m.title)}>▶ RUN</button>
                  <button style={btnGhost} onClick={() => openDetail(m.name)}>DETAILS</button>
                </div>
              </motion.div>
            ))}
          </div>
        )
      )}

      {/* ── RECORDING ────────────────────────────────────────────────────── */}
      {view === 'recording' && (
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 22, padding: '36px 0' }}>
          {countdown > 0 ? (
            <>
              <motion.div key={countdown} initial={{ scale: 0.6, opacity: 0 }} animate={{ scale: 1, opacity: 1 }}
                style={{ fontFamily: 'var(--font-d)', fontSize: 96, fontWeight: 900, color: 'var(--cyan)', textShadow: 'var(--glow-sm)' }}>
                {countdown}
              </motion.div>
              <div style={{ fontFamily: 'var(--font-m)', fontSize: 12, color: 'var(--text-lo)', letterSpacing: '0.16em' }}>
                GET READY — SWITCH TO YOUR APP AND DO THE TASK
              </div>
            </>
          ) : (
            <>
              <motion.div animate={{ opacity: [1, 0.35, 1] }} transition={{ duration: 1.4, repeat: Infinity }}
                style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                <span style={{ width: 16, height: 16, borderRadius: '50%', background: 'var(--red)', boxShadow: '0 0 18px var(--red)' }} />
                <span style={{ fontFamily: 'var(--font-d)', fontSize: 22, fontWeight: 800, letterSpacing: '0.1em', color: 'var(--text-hi)' }}>RECORDING</span>
              </motion.div>
              <div style={{ display: 'flex', gap: 30, fontFamily: 'var(--font-m)', fontSize: 11, color: 'var(--text-lo)', letterSpacing: '0.1em' }}>
                <span><b style={{ color: 'var(--cyan)', fontSize: 16 }}>{rec.events}</b> actions captured</span>
                <span><b style={{ color: 'var(--cyan)', fontSize: 16 }}>{(rec.elapsed_ms / 1000).toFixed(0)}s</b> elapsed</span>
              </div>
              <div style={{ ...noteBox('var(--cyan)'), maxWidth: 500, textAlign: 'center', marginBottom: 0, lineHeight: 1.7 }}>
                Do your chore now. Kinesis captures each step by <b style={{ color: 'var(--text-hi)' }}>what it is</b> —
                the button's label, the field's name, the page URL, scroll and keypresses — not by pixel
                position, so replay finds them again even if the window moved or the layout shifted.<br />
                Stop with <b style={{ color: 'var(--text-hi)' }}>⌥⎋ (Option-Escape)</b> without leaving your app,
                or come back and hit Stop.
              </div>
              <button style={{ ...btnRec, fontSize: 12, padding: '11px 26px' }} onClick={() => finishRecording(true)}>■ STOP</button>
            </>
          )}
        </div>
      )}

      {/* ── REVIEW / SAVE ────────────────────────────────────────────────── */}
      {view === 'review' && (
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
            <div style={{ fontFamily: 'var(--font-d)', fontSize: 10, fontWeight: 700, letterSpacing: '0.16em', color: 'var(--cyan)' }}>
              {understanding ? '✦ UNDERSTANDING WHAT YOU TAUGHT…' : '✦ NAMED FROM WHAT YOU DID'}
            </div>
            <div style={{ flex: 1 }} />
            <button style={{ ...btnGhost, opacity: understanding ? 0.5 : 1 }} disabled={understanding}
              onClick={() => runUnderstand(capSteps)}>↻ RE-NAME</button>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginBottom: 14 }}>
            <input autoFocus value={name} onChange={e => setName(e.target.value)} placeholder="macro-name (kebab-case)"
              onKeyDown={e => { if (e.key === 'Enter') saveMacro() }} style={inp} />
            <input value={title} onChange={e => setTitle(e.target.value)} placeholder="Display title (optional)" style={inp} />
          </div>
          <input value={desc} onChange={e => setDesc(e.target.value)} placeholder="What does this chore do? (optional)"
            style={{ ...inp, width: '100%', boxSizing: 'border-box', marginBottom: 14 }} />

          <div style={{ fontFamily: 'var(--font-d)', fontSize: 10, fontWeight: 700, letterSpacing: '0.16em', color: 'var(--cyan)', marginBottom: 10 }}>
            ◎ CAPTURED — {capDesc.length} STEPS · {fmtDur(capDur)}
          </div>
          <StepList lines={capDesc} />

          <div style={{ display: 'flex', gap: 8, marginTop: 16 }}>
            <button style={{ ...btn, opacity: busy ? 0.5 : 1 }} disabled={busy} onClick={saveMacro}>
              {busy ? 'SAVING…' : '✓ SAVE MACRO'}
            </button>
            <button style={btnGhost} onClick={() => { setView('list'); setStatus(null) }}>DISCARD</button>
          </div>
        </div>
      )}

      {/* ── DETAIL ───────────────────────────────────────────────────────── */}
      {view === 'detail' && sel && (
        <div>
          {editing ? (
            <div style={{ background: 'var(--bg-card)', border: '1px solid var(--border-hi)',
              borderRadius: 8, padding: 16, marginBottom: 16 }}>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginBottom: 10 }}>
                <label style={fieldLabel}>NAME (RENAME)
                  <input value={eName} onChange={e => setEName(e.target.value)}
                    style={{ ...inp, width: '100%', boxSizing: 'border-box', marginTop: 6 }} /></label>
                <label style={fieldLabel}>TITLE
                  <input value={eTitle} onChange={e => setETitle(e.target.value)}
                    style={{ ...inp, width: '100%', boxSizing: 'border-box', marginTop: 6 }} /></label>
              </div>
              <label style={fieldLabel}>DESCRIPTION
                <textarea value={eDesc} onChange={e => setEDesc(e.target.value)}
                  style={{ ...inp, width: '100%', boxSizing: 'border-box', minHeight: 60,
                    resize: 'vertical', marginTop: 6 }} /></label>

              {/* Variables — name a recorded value so it can be swapped at run time */}
              <div style={{ marginTop: 14 }}>
                <div style={fieldLabel}>VARIABLES — swap these values each run</div>
                {eParams.map((p, i) => (
                  <div key={i} style={{ display: 'flex', gap: 8, marginTop: 6 }}>
                    <input value={p.name} placeholder="name (e.g. query)"
                      onChange={e => setEParams(a => a.map((x, j) => j === i ? { ...x, name: e.target.value } : x))}
                      style={{ ...inp, flex: 1, boxSizing: 'border-box' }} />
                    <input value={p.value} placeholder="recorded value to replace (e.g. staging)"
                      onChange={e => setEParams(a => a.map((x, j) => j === i ? { ...x, value: e.target.value } : x))}
                      style={{ ...inp, flex: 2, boxSizing: 'border-box' }} />
                    <button style={{ ...btnGhost, padding: '5px 10px', color: 'var(--red)' }}
                      onClick={() => setEParams(a => a.filter((_, j) => j !== i))}>✕</button>
                  </div>
                ))}
                <button style={{ ...btnGhost, padding: '6px 12px', marginTop: 8 }}
                  onClick={() => setEParams(a => [...a, { name: '', value: '' }])}>+ ADD VARIABLE</button>
              </div>

              <div style={{ display: 'flex', gap: 8, marginTop: 14 }}>
                <button style={{ ...btn, opacity: eBusy ? 0.5 : 1 }} disabled={eBusy} onClick={saveEdit}>
                  {eBusy ? 'SAVING…' : '✓ SAVE'}</button>
                <button style={btnGhost} onClick={() => setEditing(false)}>CANCEL</button>
              </div>
            </div>
          ) : (
            sel.description && <div style={{ ...noteBox('var(--cyan)'), lineHeight: 1.6 }}>{sel.description}</div>
          )}
          <div style={{ display: 'flex', gap: 8, marginBottom: 16 }}>
            <button style={btn} onClick={() => runMacro(sel.name, sel.title)}>▶ RUN MACRO</button>
            {!editing && <button style={btnGhost} onClick={startEdit}>✎ EDIT</button>}
            <div style={{ flex: 1 }} />
            <button style={{ ...btnGhost, color: 'var(--red)', borderColor: 'rgba(255,34,68,0.4)' }}
              onClick={() => deleteMacro(sel.name)}>DELETE</button>
          </div>
          <div style={{ fontFamily: 'var(--font-m)', fontSize: 10.5, color: 'var(--text-lo)', lineHeight: 1.6, marginBottom: 14 }}>
            Targets are matched by element (label / role / selector), not pixel position — if one moved or
            the page changed, Cosmos's model picks the best match from the live page. Runs in your real
            Chrome, so saved logins & autofill carry sign-in steps through without touching your passwords.
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
            <div style={{ fontFamily: 'var(--font-d)', fontSize: 10, fontWeight: 700, letterSpacing: '0.16em', color: 'var(--cyan)' }}>
              ◎ {(editSteps || sel.steps).length} STEPS · {fmtDur(sel.duration_ms)}
            </div>
            <div style={{ flex: 1 }} />
            {editSteps ? (
              <>
                <button style={{ ...btn, opacity: stepsBusy ? 0.5 : 1 }} disabled={stepsBusy} onClick={saveSteps}>
                  {stepsBusy ? 'SAVING…' : '✓ SAVE STEPS'}</button>
                <button style={btnGhost} onClick={() => setEditSteps(null)}>CANCEL</button>
              </>
            ) : (
              <button style={btnGhost} onClick={startStepsEdit}>✎ EDIT STEPS</button>
            )}
          </div>
          {editSteps ? (
            <div style={{ background: 'var(--bg-deep)', border: '1px solid var(--border)', borderRadius: 8,
              padding: 8, maxHeight: 380, overflowY: 'auto' }}>
              {editSteps.length === 0 && <div style={{ color: 'var(--text-lo)', fontFamily: 'var(--font-m)', fontSize: 12, padding: 6 }}>All steps removed.</div>}
              {editSteps.map((_, i) => (
                <div key={i} style={{ display: 'flex', gap: 8, alignItems: 'center', padding: '5px 6px',
                  borderBottom: i < editSteps.length - 1 ? '1px solid rgba(255,255,255,0.04)' : 'none' }}>
                  <span style={{ fontFamily: 'var(--font-m)', fontSize: 9, color: 'var(--text-lo)', minWidth: 20, textAlign: 'right' }}>{i + 1}</span>
                  <span style={{ flex: 1, fontFamily: 'var(--font-m)', fontSize: 12, color: 'var(--text)' }}>{editDescribe[i]}</span>
                  <button style={miniBtn} disabled={i === 0} onClick={() => moveStep(i, -1)}>▲</button>
                  <button style={miniBtn} disabled={i === editSteps.length - 1} onClick={() => moveStep(i, 1)}>▼</button>
                  <button style={{ ...miniBtn, color: 'var(--red)' }} onClick={() => deleteStep(i)}>✕</button>
                </div>
              ))}
            </div>
          ) : (
            <StepList lines={sel.describe || []} />
          )}
        </div>
      )}

      {/* ── RUN-WITH-VARIABLES FORM ──────────────────────────────────────── */}
      <AnimatePresence>
        {runForm && (
          <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
            style={{ position: 'fixed', inset: 0, zIndex: 200, display: 'flex', flexDirection: 'column',
              alignItems: 'center', justifyContent: 'center', gap: 14,
              background: 'rgba(1,6,16,0.82)', backdropFilter: 'blur(4px)' }}>
            <div style={{ background: 'var(--bg-card)', border: '1px solid var(--border-hi)',
              borderRadius: 12, padding: 24, width: 440, maxWidth: '90vw', boxShadow: 'var(--glow-md)' }}>
              <div style={{ fontFamily: 'var(--font-d)', fontSize: 13, fontWeight: 700, color: 'var(--text-hi)', marginBottom: 4 }}>
                Run “{runForm.title}”
              </div>
              <div style={{ fontFamily: 'var(--font-m)', fontSize: 10.5, color: 'var(--text-lo)', marginBottom: 14 }}>
                Set the variables for this run (defaults are what you recorded).
              </div>
              {runForm.defs.map((d, i) => (
                <label key={i} style={{ ...fieldLabel, marginTop: i ? 10 : 0 }}>{d.name}
                  <input autoFocus={i === 0} value={d.value}
                    onChange={e => setRunForm(f => f && ({ ...f, defs: f.defs.map((x, j) => j === i ? { ...x, value: e.target.value } : x) }))}
                    style={{ ...inp, width: '100%', boxSizing: 'border-box', marginTop: 6 }} /></label>
              ))}
              <div style={{ display: 'flex', gap: 8, marginTop: 18 }}>
                <button style={btn} onClick={() => {
                  const params = Object.fromEntries(runForm.defs.map(d => [d.name, d.value]))
                  const { name, title } = runForm; setRunForm(null); doReplay(name, title, params)
                }}>▶ RUN</button>
                <button style={btnGhost} onClick={() => setRunForm(null)}>CANCEL</button>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* ── REPLAY COUNTDOWN OVERLAY ─────────────────────────────────────── */}
      <AnimatePresence>
        {replayFor && (
          <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
            style={{ position: 'fixed', inset: 0, zIndex: 200, display: 'flex', flexDirection: 'column',
              alignItems: 'center', justifyContent: 'center', gap: 18,
              background: 'rgba(1,6,16,0.82)', backdropFilter: 'blur(4px)' }}>
            {replayCount > 0 ? (
              <>
                <motion.div key={replayCount} initial={{ scale: 0.6, opacity: 0 }} animate={{ scale: 1, opacity: 1 }}
                  style={{ fontFamily: 'var(--font-d)', fontSize: 110, fontWeight: 900, color: 'var(--cyan)', textShadow: 'var(--glow-sm)' }}>
                  {replayCount}
                </motion.div>
                <div style={{ fontFamily: 'var(--font-d)', fontSize: 15, fontWeight: 700, letterSpacing: '0.08em', color: 'var(--text-hi)' }}>
                  Replaying “{replayFor.title}”
                </div>
                <div style={{ fontFamily: 'var(--font-m)', fontSize: 11, color: 'var(--text-lo)', letterSpacing: '0.14em' }}>
                  GET YOUR WINDOWS IN POSITION — HANDS OFF THE MOUSE
                </div>
              </>
            ) : (
              <>
                <motion.div animate={{ rotate: 360 }} transition={{ duration: 1.1, repeat: Infinity, ease: 'linear' }}
                  style={{ width: 40, height: 40, borderRadius: '50%', border: '3px solid var(--cyan-10)', borderTopColor: 'var(--cyan)' }} />
                <div style={{ fontFamily: 'var(--font-d)', fontSize: 14, fontWeight: 700, color: 'var(--text-hi)' }}>Replaying…</div>
              </>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </PageShell>
  )
}

// ── shared bits ─────────────────────────────────────────────────────────────
const inp: React.CSSProperties = {
  background: 'var(--bg-deep)', border: '1px solid var(--border)', borderRadius: 4,
  padding: '9px 12px', color: 'var(--text-hi)', fontFamily: 'var(--font-m)', fontSize: 13, outline: 'none',
}
const fieldLabel: React.CSSProperties = {
  fontFamily: 'var(--font-d)', fontSize: 9, fontWeight: 700, letterSpacing: '0.14em',
  color: 'var(--text-lo)', display: 'block',
}
const miniBtn: React.CSSProperties = {
  background: 'none', border: '1px solid var(--border)', borderRadius: 4, cursor: 'pointer',
  color: 'var(--text)', fontSize: 10, padding: '2px 7px', lineHeight: 1,
}

function StepList({ lines }: { lines: string[] }) {
  return (
    <div style={{ background: 'var(--bg-deep)', border: '1px solid var(--border)', borderRadius: 8,
      padding: 12, maxHeight: 360, overflowY: 'auto' }}>
      {lines.length === 0 && <div style={{ color: 'var(--text-lo)', fontFamily: 'var(--font-m)', fontSize: 12 }}>No steps.</div>}
      {lines.map((l, i) => (
        <div key={i} style={{ display: 'flex', gap: 12, alignItems: 'baseline', padding: '5px 6px',
          borderBottom: i < lines.length - 1 ? '1px solid rgba(255,255,255,0.04)' : 'none' }}>
          <span style={{ fontFamily: 'var(--font-m)', fontSize: 9, color: 'var(--text-lo)', minWidth: 22, textAlign: 'right' }}>{i + 1}</span>
          <span style={{ fontFamily: 'var(--font-m)', fontSize: 12, color: 'var(--text)' }}>{l}</span>
        </div>
      ))}
    </div>
  )
}
