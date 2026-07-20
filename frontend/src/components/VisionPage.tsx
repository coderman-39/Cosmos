import { useEffect, useRef, useState, useCallback } from 'react'
import type { Page } from '../store'

// ── VISION — screen watchers. Drag a rectangle on a screenshot, say what to
// watch and (optionally) when to alert. The backend polls the region: a cheap
// pixel-hash gate skips unchanged frames, the vision model reads the value when
// pixels move, and alerts land as TTS + a HUD card + a macOS notification. ──

interface HistoryRow { ts: string; value: string; alert: boolean; reason: string }
interface Reflex {
  kind: 'none' | 'macro' | 'prompt'
  macro: string; prompt: string; cooldown_s: number
  last_fired: string; last_result: string; fires: number
}
interface Watcher {
  id: string; name: string; question: string; condition: string
  type?: string; url?: string
  region: { x: number; y: number; w: number; h: number }
  interval_s: number; enabled: boolean; created: string
  last_checked: string; last_value: string; last_error: string
  alerts_fired: number; history: HistoryRow[]
  reflex?: Reflex
}

const EMPTY_REFLEX: Reflex = { kind: 'none', macro: '', prompt: '', cooldown_s: 600,
  last_fired: '', last_result: '', fires: 0 }

const INTERVALS = [
  { label: '30s', s: 30 }, { label: '1m', s: 60 },
  { label: '5m', s: 300 }, { label: '15m', s: 900 },
]

type Sel = { x1: number; y1: number; x2: number; y2: number } | null

export default function VisionPage({ onNavigate }: { page?: Page; onNavigate?: (p: Page) => void }) {
  const [list, setList] = useState<Watcher[]>([])
  const [offline, setOffline] = useState(false)
  const [status, setStatus] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)       // id being checked

  // new-watcher flow. URL mode is the default: the watcher owns the PAGE (a
  // headless render at a fixed viewport), so the region is in page coordinates
  // and never depends on what's on the user's screen. The preview is a LIVE
  // remote-controlled browser — click, type, scroll, even log in; the session
  // persists in the watch profile so future polls stay authenticated.
  const [picking, setPicking] = useState(false)
  const [mode, setMode] = useState<'url' | 'screen'>('url')
  const [url, setUrl] = useState('')
  const [liveUrl, setLiveUrl] = useState('')                  // where the session actually is
  const [viewport, setViewport] = useState<{ w: number; h: number } | null>(null)
  const [loadingPrev, setLoadingPrev] = useState(false)
  const [snap, setSnap] = useState<{ src: string } | null>(null)
  const [live, setLive] = useState(false)                     // interactive session running
  const [interact, setInteract] = useState<'browse' | 'select'>('browse')
  const [sel, setSel] = useState<Sel>(null)
  const [dragging, setDragging] = useState(false)
  const imgRef = useRef<HTMLImageElement>(null)
  const [form, setForm] = useState({ name: '', question: '', condition: '', interval_s: 60 })

  // Reflex: the action a watcher fires when it alerts. Shared editor state for
  // both the creation form and the per-card editor (only one open at a time).
  const [reflexDraft, setReflexDraft] = useState<Reflex>({ ...EMPTY_REFLEX })
  const [reflexFor, setReflexFor] = useState<string | null>(null)   // card id being edited
  const [macros, setMacros] = useState<{ name: string; title: string }[]>([])
  useEffect(() => {
    fetch('/api/kinesis').then(r => r.json())
      .then(d => setMacros(d.macros || [])).catch(() => {})
  }, [])

  const saveCardReflex = async (wid: string) => {
    const r = await fetch(`/api/watchers/${wid}/reflex`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(reflexDraft),
    }).then(x => x.json()).catch(() => ({ ok: false, message: 'Backend unreachable.' }))
    setStatus(r.message || null)
    if (r.ok) { setReflexFor(null); await load() }
  }

  // Per-card metadata editor (name / question / condition / interval / URL).
  const [editFor, setEditFor] = useState<string | null>(null)
  const [editDraft, setEditDraft] = useState({ name: '', question: '', condition: '',
    interval_s: 60, url: '' })
  const openEdit = (w: Watcher) => {
    setReflexFor(null)
    setEditDraft({ name: w.name, question: w.question, condition: w.condition || '',
      interval_s: w.interval_s, url: w.url || '' })
    setEditFor(w.id)
  }
  const saveEdit = async (wid: string) => {
    const r = await fetch(`/api/watchers/${wid}/edit`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(editDraft),
    }).then(x => x.json()).catch(() => ({ ok: false, message: 'Backend unreachable.' }))
    setStatus(r.message || null)
    if (r.ok) { setEditFor(null); await load() }
  }

  const load = useCallback(async () => {
    try {
      const d = await fetch('/api/watchers').then(r => r.ok ? r.json() : Promise.reject())
      setList(d.watchers || []); setOffline(false)
    } catch { setOffline(true) }
  }, [])
  useEffect(() => {
    load()
    const t = setInterval(load, 5000)
    return () => clearInterval(t)
  }, [load])

  const startPick = () => {
    setPicking(true); setSnap(null); setSel(null); setViewport(null); setLive(false)
    setStatus(mode === 'url'
      ? 'Enter the page URL and open the live preview.'
      : 'Screen mode: you get 3s to bring the target window forward.')
  }

  // ── live interactive session — a WebSocket remote-desktop pipe. Chrome
  // pushes a screencast frame the instant the page repaints; inputs go back on
  // the same socket. No HTTP round-trips, no polling delay. ──
  const wsRef = useRef<WebSocket | null>(null)

  const openStream = useCallback(() => {
    wsRef.current?.close()
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${location.host}/ws/watcher-session`)
    ws.onmessage = (ev) => {
      try {
        const m = JSON.parse(ev.data)
        if (m.type === 'frame') {
          setSnap({ src: `data:image/jpeg;base64,${m.data}` })
          if (m.url) setLiveUrl(m.url)
        }
      } catch { /* skip bad frame */ }
    }
    ws.onclose = () => { if (wsRef.current === ws) wsRef.current = null }
    wsRef.current = ws
  }, [])

  const startLive = async () => {
    if (!url.trim()) { setStatus('Enter a URL first.'); return }
    setLoadingPrev(true); setSel(null)
    setStatus('Starting the live preview browser…')
    try {
      const d = await fetch('/api/watchers/session/start', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url }),
      }).then(r => r.json())
      if (!d.ok) { setStatus(d.error || 'Session failed.'); setLoadingPrev(false); return }
      setLive(true); setInteract('browse'); setViewport(d.viewport)
      openStream()
      setStatus('LIVE — click, type and scroll inside the preview (log in if needed), then hit SELECT REGION.')
    } catch { setStatus('Backend unreachable.') }
    setLoadingPrev(false)
  }

  const stopLive = useCallback(async () => {
    wsRef.current?.close(); wsRef.current = null
    setLive(false)
    try { await fetch('/api/watchers/session/stop', { method: 'POST' }) } catch { /* gone */ }
  }, [])
  useEffect(() => () => {
    wsRef.current?.close()
    fetch('/api/watchers/session/stop', { method: 'POST' }).catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const sendInput = (payload: object) => {
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(payload))
  }
  const goTo = () => sendInput({ type: 'navigate', url: liveUrl || url })

  // Wheel events fire in bursts — accumulate and flush at ~12Hz so the remote
  // page scrolls smoothly without flooding the socket.
  const wheelAcc = useRef({ dy: 0, x: 0.5, y: 0.5, timer: 0 as any })
  const queueScroll = (x: number, y: number, dy: number) => {
    const acc = wheelAcc.current
    acc.dy += dy; acc.x = x; acc.y = y
    if (!acc.timer) {
      acc.timer = setTimeout(() => {
        sendInput({ type: 'scroll', x: acc.x, y: acc.y, dy: acc.dy })
        acc.dy = 0; acc.timer = 0
      }, 80)
    }
  }

  const loadScreenSnap = async () => {
    setLoadingPrev(true); setSnap(null); setSel(null)
    setStatus('Capturing your screen — switch to the window you want to watch within 3s…')
    await new Promise(r => setTimeout(r, 3000))
    try {
      const d = await fetch('/api/watchers/snap').then(r => r.json())
      if (!d.ok) { setStatus(d.error || 'Screenshot failed.'); setLoadingPrev(false); return }
      setSnap({ src: `data:${d.media_type};base64,${d.image_b64}` })
      setStatus('Drag a rectangle around the thing to watch.')
    } catch { setStatus('Backend unreachable.') }
    setLoadingPrev(false)
  }

  const relPos = (e: React.PointerEvent | React.WheelEvent) => {
    const img = imgRef.current!
    const r = img.getBoundingClientRect()
    return {
      x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
      y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)),
    }
  }
  const browsing = live && interact === 'browse'
  const onPickDown = (e: React.PointerEvent) => {
    if (browsing) {
      const p = relPos(e)
      sendInput({ type: 'click', x: p.x, y: p.y })
      ;(e.currentTarget as HTMLElement).focus()
      return
    }
    const p = relPos(e); setDragging(true)
    setSel({ x1: p.x, y1: p.y, x2: p.x, y2: p.y })
  }
  const onPickMove = (e: React.PointerEvent) => {
    if (browsing || !dragging || !sel) return
    const p = relPos(e)
    setSel({ ...sel, x2: p.x, y2: p.y })
  }
  const onPickUp = () => setDragging(false)
  const onPickWheel = (e: React.WheelEvent) => {
    if (!browsing) return
    e.preventDefault()
    const p = relPos(e)
    queueScroll(p.x, p.y, e.deltaY * 2)
  }
  const FORWARD_KEYS = ['Enter', 'Backspace', 'Tab', 'Escape', 'Delete',
    'ArrowLeft', 'ArrowUp', 'ArrowRight', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End']
  const onPickKey = (e: React.KeyboardEvent) => {
    if (!browsing) return
    if (e.metaKey || e.ctrlKey) return          // don't eat browser shortcuts
    if (e.key.length === 1) {
      e.preventDefault(); sendInput({ type: 'text', text: e.key })
    } else if (FORWARD_KEYS.includes(e.key)) {
      e.preventDefault(); sendInput({ type: 'key', key: e.key })
    }
  }
  const onPickPaste = (e: React.ClipboardEvent) => {
    if (!browsing) return
    const t = e.clipboardData.getData('text')
    if (t) { e.preventDefault(); sendInput({ type: 'text', text: t }) }
  }

  const region = sel ? {
    x: Math.min(sel.x1, sel.x2), y: Math.min(sel.y1, sel.y2),
    w: Math.abs(sel.x2 - sel.x1), h: Math.abs(sel.y2 - sel.y1),
  } : null

  const saveWatcher = async () => {
    if (!region || region.w < 0.01 || region.h < 0.01) { setStatus('Drag a rectangle first (SELECT REGION mode).'); return }
    if (!form.question.trim()) { setStatus('Say what to watch — e.g. "the settlement count".'); return }
    // The watcher polls the URL the session is CURRENTLY on (you may have
    // navigated well past the address you typed).
    const watchUrl = mode === 'url' ? (liveUrl || url) : ''
    if (live) { setStatus('Saving your session (login persists) …'); await stopLive() }
    const r = await fetch('/api/watchers', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...form, name: form.name || form.question.slice(0, 40), region,
        type: mode, url: watchUrl, viewport, reflex: reflexDraft }),
    }).then(x => x.json()).catch(() => ({ ok: false, message: 'Backend unreachable.' }))
    setStatus(r.message || null)
    if (r.ok) {
      setPicking(false); setSnap(null); setSel(null); setUrl(''); setLiveUrl(''); setViewport(null)
      setForm({ name: '', question: '', condition: '', interval_s: 60 })
      setReflexDraft({ ...EMPTY_REFLEX })
      await load()
      if (r.id) { setBusy(r.id); await fetch(`/api/watchers/${r.id}/check`, { method: 'POST' }); setBusy(null); await load() }
    }
  }

  const cancelPick = async () => {
    if (live) await stopLive()
    setPicking(false); setSnap(null); setSel(null); setLiveUrl('')
  }

  const checkNow = async (id: string) => {
    setBusy(id)
    const r = await fetch(`/api/watchers/${id}/check`, { method: 'POST' })
      .then(x => x.json()).catch(() => null)
    setBusy(null)
    if (r) setStatus(r.alert ? `⚠ Alert: ${r.reason || r.value}` : r.ok ? `Read: ${r.value || '(no change)'}` : r.message)
    await load()
  }
  const toggle = async (id: string) => { await fetch(`/api/watchers/${id}/toggle`, { method: 'POST' }); await load() }
  const remove = async (id: string) => {
    if (!confirm('Remove this watcher?')) return
    await fetch(`/api/watchers/${id}`, { method: 'DELETE' }); await load()
  }

  const S = STYLES
  return (
    <div style={S.root}>
      <div style={S.top}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
          <button style={S.back} onClick={() => onNavigate?.('home')}>◄ HOME</button>
          <div style={S.brand}>👁 VISION</div>
          <div style={S.sub}>SCREEN WATCHERS · PING ON CHANGE</div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
          <div style={S.stats}>
            <span><b>{list.length}</b> watchers</span>
            <span><b style={{ color: '#ffbf47' }}>{list.reduce((a, w) => a + (w.alerts_fired || 0), 0)}</b> alerts fired</span>
          </div>
          {!picking && <button style={S.cta} onClick={startPick}>+ NEW WATCHER</button>}
        </div>
      </div>

      {status && <div style={S.statusBar}>{status}
        <button style={S.statusX} onClick={() => setStatus(null)}>✕</button></div>}

      <div style={S.body}>
        {/* ── region picker ── */}
        {picking && (
          <div style={S.picker}>
            {/* mode toggle */}
            <div style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
              {(['url', 'screen'] as const).map(m => (
                <button key={m} onClick={async () => { if (live) await stopLive(); setMode(m); setSnap(null); setSel(null) }}
                  style={{ ...S.chipBtn, padding: '8px 16px',
                    borderColor: mode === m ? '#00e5ff' : 'rgba(255,255,255,0.14)',
                    color: mode === m ? '#00e5ff' : '#9fc2d4' }}>
                  {m === 'url' ? '🌐 WEB PAGE (recommended)' : '🖥 SCREEN REGION'}
                </button>
              ))}
            </div>

            {mode === 'url' ? (
              <div style={{ display: 'flex', gap: 10, marginBottom: 12, alignItems: 'center' }}>
                <input style={{ ...S.input, flex: 1 }}
                  placeholder="https://your-dashboard.example.com/…"
                  value={live ? (liveUrl || url) : url}
                  onChange={e => { setUrl(e.target.value); if (live) setLiveUrl(e.target.value) }}
                  onKeyDown={e => { if (e.key === 'Enter') (live ? goTo() : startLive()) }} />
                {!live ? (
                  <button style={S.cta} disabled={loadingPrev} onClick={startLive}>
                    {loadingPrev ? 'STARTING…' : '▶ OPEN LIVE PREVIEW'}</button>
                ) : (
                  <>
                    <button style={S.ghost} onClick={goTo}>GO</button>
                    <button style={S.ghost} onClick={() => sendInput({ type: 'back' })}>◄ BACK</button>
                    <span style={{ fontSize: 10, color: '#43f5b0', letterSpacing: '0.12em',
                      display: 'flex', alignItems: 'center', gap: 6 }}>
                      <span style={{ width: 7, height: 7, borderRadius: '50%', background: '#43f5b0',
                        boxShadow: '0 0 8px #43f5b0' }} />LIVE
                    </span>
                  </>
                )}
              </div>
            ) : (
              !snap && (
                <div style={{ marginBottom: 12 }}>
                  <button style={S.cta} disabled={loadingPrev} onClick={loadScreenSnap}>
                    {loadingPrev ? 'CAPTURING…' : '📸 CAPTURE SCREEN (3s delay)'}</button>
                </div>
              )
            )}

            {loadingPrev && !snap && <div style={S.pickWait}>
              {mode === 'url' ? 'Rendering page…' : 'Capturing screen…'}</div>}

            {snap && (
              <>
                {live && (
                  <div style={{ display: 'flex', gap: 8, marginBottom: 10, alignItems: 'center' }}>
                    {(['browse', 'select'] as const).map(m => (
                      <button key={m}
                        onClick={() => {
                          setInteract(m)
                          if (m === 'browse') setSel(null)
                          // Polls render pages from the TOP — selection must be
                          // made against the top-of-page view.
                          if (m === 'select') sendInput({ type: 'scrolltop' })
                        }}
                        style={{ ...S.chipBtn, padding: '7px 14px',
                          borderColor: interact === m ? '#00e5ff' : 'rgba(255,255,255,0.14)',
                          color: interact === m ? '#00e5ff' : '#9fc2d4' }}>
                        {m === 'browse' ? '🖱 BROWSE (click · type · scroll)' : '⬚ SELECT REGION'}
                      </button>
                    ))}
                    <span style={{ fontSize: 10, color: '#5f8ea0' }}>
                      {interact === 'browse'
                        ? 'interacting with the real page — log in, navigate, scroll to your target'
                        : 'drag a rectangle around the thing to watch'}
                    </span>
                  </div>
                )}
                {/* The wrapper shrink-wraps the img (inline-block), so the
                    overlay + fraction math always align with the ACTUAL pixels
                    — no stretching, no letterbox offset. maxHeight keeps the
                    whole viewport visible without scrolling the picker. */}
                <div style={{ position: 'relative', userSelect: 'none', outline: 'none',
                    display: 'inline-block', maxWidth: '100%' }}
                  tabIndex={0} onKeyDown={onPickKey} onPaste={onPickPaste}>
                  <img ref={imgRef} src={snap.src} draggable={false}
                    style={{ display: 'block', maxWidth: '100%', maxHeight: '66vh',
                      width: 'auto', height: 'auto', borderRadius: 10,
                      border: `1px solid ${browsing ? 'rgba(67,245,176,0.45)' : 'rgba(0,229,255,0.25)'}`,
                      cursor: browsing ? 'default' : 'crosshair' }}
                    onPointerDown={onPickDown} onPointerMove={onPickMove} onPointerUp={onPickUp}
                    onWheel={onPickWheel} />
                  {region && !browsing && (
                    <div style={{ position: 'absolute', pointerEvents: 'none',
                      left: `${region.x * 100}%`, top: `${region.y * 100}%`,
                      width: `${region.w * 100}%`, height: `${region.h * 100}%`,
                      border: '2px solid #00e5ff', background: 'rgba(0,229,255,0.12)',
                      boxShadow: '0 0 22px rgba(0,229,255,0.5)' }} />
                  )}
                </div>
                <div style={S.form}>
                  <input style={S.input} placeholder='What to watch — e.g. "the settlement count number"'
                    value={form.question} onChange={e => setForm({ ...form, question: e.target.value })} />
                  <input style={S.input} placeholder='Alert when (optional) — e.g. "it drops below 100" · empty = any change'
                    value={form.condition} onChange={e => setForm({ ...form, condition: e.target.value })} />
                  <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
                    <input style={{ ...S.input, flex: 1 }} placeholder="Name (optional)"
                      value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} />
                    <span style={{ fontSize: 10, color: '#5f8ea0', letterSpacing: '0.1em' }}>EVERY</span>
                    {INTERVALS.map(iv => (
                      <button key={iv.s} onClick={() => setForm({ ...form, interval_s: iv.s })}
                        style={{ ...S.chipBtn, borderColor: form.interval_s === iv.s ? '#00e5ff' : 'rgba(255,255,255,0.14)',
                          color: form.interval_s === iv.s ? '#00e5ff' : '#9fc2d4' }}>{iv.label}</button>
                    ))}
                  </div>
                  {/* REFLEX — the action this watcher fires when it alerts */}
                  <div style={{ border: '1px solid rgba(255,191,71,0.22)', borderRadius: 10,
                    padding: '12px 14px', background: 'rgba(255,191,71,0.04)' }}>
                    <div style={{ fontSize: 10, letterSpacing: '0.2em', color: '#ffbf47', marginBottom: 8 }}>
                      ⚡ REFLEX — WHEN THIS ALERTS…
                    </div>
                    <ReflexEditor draft={reflexDraft} setDraft={setReflexDraft} macros={macros} />
                  </div>
                  <div style={{ display: 'flex', gap: 10 }}>
                    <button style={S.cta} onClick={saveWatcher}>▶ START WATCHING</button>
                    {mode === 'screen' && <button style={S.ghost} onClick={loadScreenSnap}>↺ RETAKE SHOT</button>}
                    <button style={S.ghost} onClick={cancelPick}>CANCEL</button>
                  </div>
                </div>
              </>
            )}
          </div>
        )}

        {/* ── watcher cards ── */}
        {!picking && (
          offline ? <div style={S.note}>Backend offline — start it and reload.</div> :
          list.length === 0 ? (
            <div style={S.note}>
              No watchers yet. Hit <b style={{ color: '#00e5ff' }}>+ NEW WATCHER</b>, drag a box around any
              number / status / region on your screen, and COSMOS will ping you the moment it changes —
              spoken, in the HUD, and as a macOS notification.
            </div>
          ) : (
            <div style={S.grid}>
              {list.map(w => (
                <div key={w.id} style={{ ...S.card, opacity: w.enabled ? 1 : 0.55 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span style={{ width: 8, height: 8, borderRadius: '50%', flex: 'none',
                      background: w.last_error ? '#ff5a5a' : w.enabled ? '#43f5b0' : '#7f9cad',
                      boxShadow: w.enabled && !w.last_error ? '0 0 8px #43f5b0' : 'none' }} />
                    <div style={S.cardName}>{w.name}</div>
                    {w.alerts_fired > 0 && <span style={S.alertBadge}>{w.alerts_fired}⚡</span>}
                  </div>
                  <img src={`/api/watchers/${w.id}/thumb?t=${w.last_checked}`} alt=""
                    style={S.thumb} onError={e => { (e.target as HTMLImageElement).style.display = 'none' }} />
                  <div style={S.q}>{w.question}</div>
                  <div style={{ fontSize: 10, color: '#5f8ea0', marginTop: 4, overflow: 'hidden',
                    textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {w.type === 'url' ? `🌐 ${w.url}` : '🖥 screen region'}
                  </div>
                  {w.condition && <div style={S.cond}>alert when: {w.condition}</div>}
                  {w.reflex && w.reflex.kind !== 'none' && (
                    <div style={{ fontSize: 10.5, color: '#ffbf47', marginTop: 4 }}>
                      ⚡ reflex: {w.reflex.kind === 'macro' ? `macro “${w.reflex.macro}”` : 'agent task'}
                      {w.reflex.fires > 0 && ` · fired ${w.reflex.fires}× · last ${w.reflex.last_fired.slice(11, 16)}`}
                    </div>
                  )}
                  {w.reflex && w.reflex.kind !== 'none' && w.reflex.last_result && (
                    <div style={{ fontSize: 10, color: '#8a7a52', marginTop: 2, overflow: 'hidden',
                      textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                      title={w.reflex.last_result}>↳ {w.reflex.last_result}</div>
                  )}
                  <div style={S.value}>{w.last_error
                    ? <span style={{ color: '#ff5a5a' }}>{w.last_error}</span>
                    : (w.last_value || '— not read yet —')}</div>
                  <div style={S.meta}>
                    every {w.interval_s >= 60 ? `${w.interval_s / 60}m` : `${w.interval_s}s`}
                    {w.last_checked && ` · checked ${w.last_checked.slice(11, 16)}`}
                  </div>
                  <div style={{ display: 'flex', gap: 8, marginTop: 10 }}>
                    <button style={S.mini} disabled={busy === w.id} onClick={() => checkNow(w.id)}>
                      {busy === w.id ? '…' : 'CHECK NOW'}</button>
                    <button style={S.mini} onClick={() => toggle(w.id)}>{w.enabled ? 'PAUSE' : 'RESUME'}</button>
                    <button style={S.mini} onClick={() => setExpanded(expanded === w.id ? null : w.id)}>
                      HISTORY {expanded === w.id ? '▴' : '▾'}</button>
                    <button style={S.mini}
                      onClick={() => editFor === w.id ? setEditFor(null) : openEdit(w)}>✎ EDIT</button>
                    <button style={{ ...S.mini, color: '#ffbf47', borderColor: 'rgba(255,191,71,0.3)' }}
                      onClick={() => {
                        if (reflexFor === w.id) { setReflexFor(null); return }
                        setEditFor(null)
                        setReflexDraft({ ...EMPTY_REFLEX, ...(w.reflex || {}) })
                        setReflexFor(w.id)
                      }}>⚡ REFLEX</button>
                    <button style={{ ...S.mini, color: '#ff6a6a', borderColor: 'rgba(255,90,90,0.3)' }}
                      onClick={() => remove(w.id)}>✕</button>
                  </div>
                  {reflexFor === w.id && (
                    <div style={{ marginTop: 10, border: '1px solid rgba(255,191,71,0.22)', borderRadius: 10,
                      padding: '12px 14px', background: 'rgba(255,191,71,0.04)' }}>
                      <ReflexEditor draft={reflexDraft} setDraft={setReflexDraft} macros={macros} />
                      <div style={{ display: 'flex', gap: 8, marginTop: 10 }}>
                        <button style={{ ...S.mini, color: '#ffbf47', borderColor: 'rgba(255,191,71,0.4)' }}
                          onClick={() => saveCardReflex(w.id)}>SAVE REFLEX</button>
                        <button style={S.mini} onClick={() => setReflexFor(null)}>CANCEL</button>
                      </div>
                    </div>
                  )}
                  {editFor === w.id && (
                    <div style={{ marginTop: 10, border: '1px solid rgba(0,229,255,0.22)', borderRadius: 10,
                      padding: '12px 14px', background: 'rgba(0,229,255,0.03)',
                      display: 'flex', flexDirection: 'column', gap: 8 }}>
                      <input style={S.input} placeholder="Name" value={editDraft.name}
                        onChange={e => setEditDraft({ ...editDraft, name: e.target.value })} />
                      <input style={S.input} placeholder="What to watch" value={editDraft.question}
                        onChange={e => setEditDraft({ ...editDraft, question: e.target.value })} />
                      <input style={S.input} placeholder="Alert when (empty = any change)" value={editDraft.condition}
                        onChange={e => setEditDraft({ ...editDraft, condition: e.target.value })} />
                      {w.type === 'url' && (
                        <input style={S.input} placeholder="URL" value={editDraft.url}
                          onChange={e => setEditDraft({ ...editDraft, url: e.target.value })} />
                      )}
                      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                        <span style={{ fontSize: 10, color: '#5f8ea0', letterSpacing: '0.1em' }}>EVERY</span>
                        {INTERVALS.map(iv => (
                          <button key={iv.s} onClick={() => setEditDraft({ ...editDraft, interval_s: iv.s })}
                            style={{ ...S.chipBtn, padding: '4px 10px',
                              borderColor: editDraft.interval_s === iv.s ? '#00e5ff' : 'rgba(255,255,255,0.14)',
                              color: editDraft.interval_s === iv.s ? '#00e5ff' : '#9fc2d4' }}>{iv.label}</button>
                        ))}
                      </div>
                      <div style={{ fontSize: 9.5, color: '#5f8ea0' }}>
                        To move the watched region, delete this watcher and redraw — regions need the picker.
                      </div>
                      <div style={{ display: 'flex', gap: 8 }}>
                        <button style={{ ...S.mini, color: '#00e5ff', borderColor: 'rgba(0,229,255,0.4)' }}
                          onClick={() => saveEdit(w.id)}>SAVE CHANGES</button>
                        <button style={S.mini} onClick={() => setEditFor(null)}>CANCEL</button>
                      </div>
                    </div>
                  )}
                  {expanded === w.id && (
                    <div style={S.hist}>
                      {(w.history || []).slice().reverse().map((h, i) => (
                        <div key={i} style={{ ...S.histRow, color: h.alert ? '#ffbf47' : '#9fc2d4' }}>
                          <span style={{ color: '#5f8ea0' }}>{h.ts.slice(11, 19)}</span>
                          {h.alert ? ' ⚡ ' : '  '}{h.value}{h.reason ? ` — ${h.reason}` : ''}
                        </div>
                      ))}
                      {(!w.history || w.history.length === 0) && <div style={S.histRow}>no reads yet</div>}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )
        )}
      </div>
    </div>
  )
}

// Shared reflex editor — used by the creation form and the per-card editor.
function ReflexEditor({ draft, setDraft, macros }: {
  draft: Reflex; setDraft: (r: Reflex) => void
  macros: { name: string; title: string }[]
}) {
  const S = STYLES
  const KINDS: { k: Reflex['kind']; label: string }[] = [
    { k: 'none', label: 'JUST NOTIFY' },
    { k: 'macro', label: '▶ RUN MACRO' },
    { k: 'prompt', label: '◈ ASK THE AGENT' },
  ]
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <div style={{ display: 'flex', gap: 8 }}>
        {KINDS.map(({ k, label }) => (
          <button key={k} onClick={() => setDraft({ ...draft, kind: k })}
            style={{ ...S.chipBtn, padding: '6px 12px',
              borderColor: draft.kind === k ? '#ffbf47' : 'rgba(255,255,255,0.14)',
              color: draft.kind === k ? '#ffbf47' : '#9fc2d4' }}>{label}</button>
        ))}
      </div>
      {draft.kind === 'macro' && (
        macros.length === 0
          ? <div style={{ fontSize: 11, color: '#8a7a52' }}>No Kinesis macros recorded yet — record one on the Kinesis page first.</div>
          : <select value={draft.macro} onChange={e => setDraft({ ...draft, macro: e.target.value })}
              style={{ ...S.input, cursor: 'pointer' } as React.CSSProperties}>
              <option value="">— pick a macro —</option>
              {macros.map(m => <option key={m.name} value={m.name}>{m.title || m.name}</option>)}
            </select>
      )}
      {draft.kind === 'prompt' && (
        <>
          <textarea value={draft.prompt} onChange={e => setDraft({ ...draft, prompt: e.target.value })}
            placeholder={'What should COSMOS do? e.g.\n"Post to #tech_it on Slack: pre-push alerts went from {previous} to {value}."'}
            rows={3} style={{ ...S.input, resize: 'vertical', minHeight: 60 } as React.CSSProperties} />
          <div style={{ fontSize: 9.5, color: '#8a7a52' }}>
            placeholders: {'{value} {previous} {reason} {name} {url}'} — runs headless with full tool access
          </div>
        </>
      )}
      {draft.kind !== 'none' && (
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <span style={{ fontSize: 10, color: '#5f8ea0', letterSpacing: '0.1em' }}>AT MOST ONCE PER</span>
          {[{ label: '5m', s: 300 }, { label: '10m', s: 600 }, { label: '30m', s: 1800 }, { label: '2h', s: 7200 }].map(c => (
            <button key={c.s} onClick={() => setDraft({ ...draft, cooldown_s: c.s })}
              style={{ ...S.chipBtn, padding: '4px 10px',
                borderColor: draft.cooldown_s === c.s ? '#ffbf47' : 'rgba(255,255,255,0.14)',
                color: draft.cooldown_s === c.s ? '#ffbf47' : '#9fc2d4' }}>{c.label}</button>
          ))}
        </div>
      )}
    </div>
  )
}

const font = "'Share Tech Mono', ui-monospace, monospace"
const STYLES: Record<string, React.CSSProperties> = {
  root: { position: 'fixed', inset: 0, background: '#040a16', color: '#cfefff', fontFamily: font,
    display: 'flex', flexDirection: 'column', overflow: 'hidden' },
  top: { height: 52, flex: '0 0 52px', display: 'flex', alignItems: 'center', justifyContent: 'space-between',
    padding: '0 20px', borderBottom: '1px solid rgba(0,229,255,0.18)', background: 'rgba(6,14,28,0.6)', backdropFilter: 'blur(10px)' },
  back: { cursor: 'pointer', fontFamily: font, fontSize: 11, letterSpacing: '0.14em', color: '#9fd6ea',
    padding: '6px 12px', borderRadius: 7, border: '1px solid rgba(0,229,255,0.22)', background: 'rgba(8,18,34,0.5)' },
  brand: { fontFamily: 'Orbitron, monospace', fontWeight: 800, letterSpacing: '0.24em', fontSize: 15,
    color: '#eafaff', textShadow: '0 0 14px rgba(0,229,255,0.5)' },
  sub: { fontSize: 10, letterSpacing: '0.18em', color: '#5f8ea0' },
  stats: { display: 'flex', gap: 16, fontSize: 12, color: '#9fc2d4' },
  cta: { cursor: 'pointer', fontFamily: font, fontSize: 11, fontWeight: 700, letterSpacing: '0.1em',
    color: '#02121c', background: 'linear-gradient(180deg,#26e0ff,#00b6e6)', border: 'none',
    padding: '9px 16px', borderRadius: 8, boxShadow: '0 0 18px rgba(0,212,255,0.4)' },
  ghost: { cursor: 'pointer', fontFamily: font, fontSize: 11, letterSpacing: '0.1em', color: '#9fd6ea',
    background: 'transparent', border: '1px solid rgba(0,229,255,0.25)', padding: '9px 16px', borderRadius: 8 },
  statusBar: { flex: '0 0 auto', padding: '7px 20px', fontSize: 11.5, color: '#8fdcff',
    background: 'rgba(0,60,90,0.25)', borderBottom: '1px solid rgba(0,229,255,0.12)',
    display: 'flex', justifyContent: 'space-between', alignItems: 'center' },
  statusX: { cursor: 'pointer', background: 'none', border: 'none', color: '#5f8ea0', fontSize: 12 },
  body: { flex: 1, minHeight: 0, overflowY: 'auto', padding: 20 },
  picker: { maxWidth: 980, margin: '0 auto' },
  pickWait: { textAlign: 'center', padding: 60, color: '#8fdcff', fontSize: 13, letterSpacing: '0.1em' },
  form: { display: 'flex', flexDirection: 'column', gap: 10, marginTop: 14 },
  input: { fontFamily: font, fontSize: 12.5, color: '#eafaff', background: 'rgba(8,18,34,0.7)',
    border: '1px solid rgba(0,229,255,0.2)', borderRadius: 8, padding: '10px 12px', outline: 'none' },
  chipBtn: { cursor: 'pointer', fontFamily: font, fontSize: 10.5, background: 'transparent',
    border: '1px solid', borderRadius: 6, padding: '6px 10px' },
  note: { maxWidth: 640, margin: '40px auto', fontSize: 13, lineHeight: 1.8, color: '#9fc2d4',
    border: '1px solid rgba(0,229,255,0.15)', borderRadius: 12, padding: 22, background: 'rgba(8,18,34,0.4)' },
  grid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))', gap: 16 },
  card: { border: '1px solid rgba(0,229,255,0.16)', borderRadius: 12, padding: 16,
    background: 'linear-gradient(160deg, rgba(9,25,44,0.65), rgba(5,13,26,0.5))' },
  cardName: { fontSize: 13.5, fontWeight: 700, color: '#eafaff', letterSpacing: '0.04em', flex: 1,
    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
  alertBadge: { fontSize: 10, color: '#ffbf47', border: '1px solid rgba(255,191,71,0.4)',
    borderRadius: 9, padding: '1px 7px' },
  thumb: { width: '100%', maxHeight: 110, objectFit: 'cover', borderRadius: 8, marginTop: 10,
    border: '1px solid rgba(255,255,255,0.08)' },
  q: { fontSize: 12, color: '#bfe6f2', marginTop: 10, lineHeight: 1.5 },
  cond: { fontSize: 10.5, color: '#ffbf47', marginTop: 4 },
  value: { fontFamily: 'JetBrains Mono, monospace', fontSize: 14, color: '#43f5b0', marginTop: 10,
    padding: '8px 10px', background: 'rgba(0,0,0,0.25)', borderRadius: 7,
    border: '1px solid rgba(255,255,255,0.06)', minHeight: 20, wordBreak: 'break-word' },
  meta: { fontSize: 10, color: '#5f8ea0', marginTop: 8, letterSpacing: '0.06em' },
  mini: { cursor: 'pointer', fontFamily: font, fontSize: 9.5, letterSpacing: '0.08em', color: '#9fd6ea',
    background: 'rgba(0,180,220,0.06)', border: '1px solid rgba(0,229,255,0.2)', borderRadius: 6, padding: '5px 10px' },
  hist: { marginTop: 10, borderTop: '1px solid rgba(255,255,255,0.07)', paddingTop: 8,
    maxHeight: 160, overflowY: 'auto' },
  histRow: { fontSize: 10.5, lineHeight: 1.9, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' },
}
