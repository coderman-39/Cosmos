import { useEffect, useRef, useState, useCallback } from 'react'
import type { Page } from '../store'

// ── DOSSIER — a living per-person intelligence map. The backend sweeps Slack
// DMs + watched channels + Gmail into per-person files (promises they made,
// what they're working on, tasks they assigned you) and classifies each person
// against your org tree. This page renders that as a nexus-style animated graph
// (people orbit YOU, tiered by org relationship) plus an org-tree sidebar and a
// per-person detail panel. Canvas is imperative (rAF); selection is React. ──

interface Item { text: string; source?: string; evidence?: string }
interface Person {
  key: string; name: string; email: string; slack_id?: string; title?: string
  relationship: string; role?: string; sources: string[]; summary?: string
  promises: Item[]; working_on: Item[]; assigned_to_me: Item[]
  message_count: number; last_ts?: string; updated?: string
}
interface Dossier {
  generated?: string; window_days?: number; channels?: string[]
  org?: { me?: any; manager?: any; skip?: any }
  people: Person[]; stats?: { people: number; with_tasks: number; promises: number }
  sweeping?: boolean
  progress?: { running: boolean; phase: string; done: number; total: number }
}

const REL: Record<string, { color: string; label: string; order: number }> = {
  me:       { color: '#eafaff', label: 'You',           order: 0 },
  skip:     { color: '#9a7bff', label: 'Skip manager',  order: 1 },
  manager:  { color: '#00e5ff', label: 'Manager',       order: 2 },
  direct:   { color: '#22e0c8', label: 'Direct team',   order: 3 },
  extended: { color: '#3aa6bd', label: 'Extended team', order: 4 },
  hr:       { color: '#ffbf47', label: 'HR',            order: 5 },
  external: { color: '#ff6a3d', label: 'External',      order: 6 },
}
const relColor = (r: string) => (REL[r] || REL.external).color

type Node = { key: string; x: number; y: number; r: number; color: string; p: Person; bob: number }

export default function DossierPage({ onNavigate }: { page?: Page; onNavigate?: (p: Page) => void }) {
  const [data, setData] = useState<Dossier | null>(null)
  const [selKey, setSelKey] = useState<string | null>(null)
  const [sweeping, setSweeping] = useState(false)
  const [offline, setOffline] = useState(false)
  const [status, setStatus] = useState<string | null>(null)

  const canvasRef = useRef<HTMLCanvasElement>(null)
  const nodesRef = useRef<Node[]>([])
  const selRef = useRef<string | null>(null)
  const hoverRef = useRef<string | null>(null)
  const camRef = useRef({ zoom: 1, panX: 0, panY: 0 })
  selRef.current = selKey

  const load = useCallback(async (): Promise<Dossier | null> => {
    try {
      const d: Dossier = await fetch('/api/dossier').then(r => r.ok ? r.json() : Promise.reject())
      setData(d); setOffline(false)
      return d
    } catch { setOffline(true); return null }
  }, [])
  useEffect(() => { load() }, [load])

  // The sweep runs server-side for minutes — the POST only STARTS it; we poll
  // GET /api/dossier until progress.running flips false (survives page reloads
  // and any proxy timeout, which is what killed the old blocking request).
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const stopPoll = useCallback(() => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }
  }, [])
  const startPoll = useCallback(() => {
    stopPoll()
    pollRef.current = setInterval(async () => {
      const d = await load()
      if (!d) return
      const pr = d.progress
      if (pr?.running) {
        setSweeping(true)
        setStatus(pr.total > 0
          ? `Sweeping — ${pr.phase} (${pr.done}/${pr.total})…`
          : `Sweeping — ${pr.phase || 'gathering'}…`)
      } else {
        stopPoll(); setSweeping(false)
        setStatus(d.generated
          ? `Sweep complete — ${d.stats?.people ?? 0} people, ${d.stats?.promises ?? 0} promises, ${d.stats?.with_tasks ?? 0} owe-you.`
          : 'Sweep finished but produced no data — check Slack/Gmail connectors.')
      }
    }, 3000)
  }, [load, stopPoll])
  useEffect(() => stopPoll, [stopPoll])

  // If a sweep is already running when the page opens (e.g. after a reload),
  // resume showing its progress.
  useEffect(() => {
    if (data?.progress?.running && !pollRef.current) { setSweeping(true); startPoll() }
  }, [data, startPoll])

  const sweep = useCallback(async () => {
    if (sweeping) return
    setSweeping(true); setStatus('Starting sweep — reading the last 2 weeks of Slack + Gmail…')
    try {
      const r = await fetch('/api/dossier/sweep', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ days: 14 }),
      }).then(x => x.json())
      if (r.started === false && r.ok === false && !r.message?.includes('already')) {
        setStatus(r.message || 'Sweep failed to start.'); setSweeping(false); return
      }
      setStatus(r.message || 'Sweep started.')
      startPoll()
    } catch {
      setStatus('Couldn\'t reach the backend — is it running?')
      setSweeping(false)
    }
  }, [sweeping, startPoll])

  // ── Build the org layout: YOU at centre, people tiered by relationship. ──
  useEffect(() => {
    if (!data) return
    const me: Node = {
      key: '__me__', x: 0, y: 0, r: 26, color: REL.me.color, bob: 0,
      p: { key: '__me__', name: data.org?.me?.name || 'You', email: data.org?.me?.email || '',
           relationship: 'me', sources: [], promises: [], working_on: [], assigned_to_me: [],
           message_count: 0, role: 'You' } as Person,
    }
    const groups: Record<string, Person[]> = {}
    for (const p of data.people || []) (groups[p.relationship] ||= []).push(p)

    // (baseAngle in radians, angular spread, radius). y is DOWN in canvas space.
    const UP = -Math.PI / 2, DOWN = Math.PI / 2, RIGHT = 0, LEFT = Math.PI
    const layout: Record<string, { a: number; spread: number; rad: number }> = {
      manager:  { a: UP,    spread: 0,          rad: 150 },
      skip:     { a: UP,    spread: 0,          rad: 300 },
      direct:   { a: DOWN,  spread: Math.PI * 1.15, rad: 210 },
      extended: { a: DOWN,  spread: Math.PI * 1.5,  rad: 360 },
      hr:       { a: RIGHT, spread: Math.PI * 0.35, rad: 250 },
      external: { a: LEFT,  spread: Math.PI * 0.9,  rad: 330 },
    }
    const nodes: Node[] = [me]
    for (const rel of Object.keys(layout)) {
      const members = (groups[rel] || []).slice().sort((a, b) =>
        (b.assigned_to_me.length + b.promises.length) - (a.assigned_to_me.length + a.promises.length))
      const cfg = layout[rel]
      const n = members.length
      members.forEach((p, i) => {
        const t = n === 1 ? 0 : (i / (n - 1) - 0.5)          // -0.5..0.5
        const ang = cfg.a + t * cfg.spread
        const jitter = ((i * 37) % 11 - 5) * 4                 // deterministic wobble
        const rad = cfg.rad + jitter + (rel === 'extended' ? (i % 3) * 26 : 0)
        const imp = p.assigned_to_me.length + p.promises.length
        nodes.push({
          key: p.key, x: Math.cos(ang) * rad, y: Math.sin(ang) * rad,
          r: 8 + Math.min(6, imp * 1.5) + (rel === 'manager' || rel === 'skip' ? 5 : 0),
          color: relColor(rel), p, bob: (i * 1.7) % 6.28,
        })
      })
    }
    nodesRef.current = nodes
  }, [data])

  // ── Canvas renderer ──
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')!
    let raf = 0, disposed = false
    let W = 0, H = 0, DPR = 1, cx = 0, cy = 0
    const resize = () => {
      DPR = Math.min(window.devicePixelRatio || 1, 2)
      const par = canvas.parentElement!
      W = par.clientWidth; H = par.clientHeight
      canvas.width = W * DPR; canvas.height = H * DPR
      canvas.style.width = W + 'px'; canvas.style.height = H + 'px'
      ctx.setTransform(DPR, 0, 0, DPR, 0, 0)
      cx = W / 2; cy = H / 2
    }
    resize()
    const onResize = () => resize()
    window.addEventListener('resize', onResize)

    const stars = Array.from({ length: 140 }, () => ({
      x: Math.random(), y: Math.random(), z: Math.random() * 0.8 + 0.2,
      r: Math.random() * 1.2 + 0.3, tw: Math.random() * 6.28 }))
    const rgba = (h: string, a: number) => {
      const c = [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)]
      return `rgba(${c[0]},${c[1]},${c[2]},${a})`
    }
    const screen = (n: { x: number; y: number }) => {
      const { zoom, panX, panY } = camRef.current
      return { x: cx + n.x * zoom + panX, y: cy + n.y * zoom + panY }
    }

    const frame = (now: number) => {
      if (disposed) return
      raf = requestAnimationFrame(frame)
      const t = now * 0.001
      const nodes = nodesRef.current
      const me = nodes[0]
      // background
      const bg = ctx.createRadialGradient(cx, cy, 40, cx, cy, Math.max(W, H) * 0.8)
      bg.addColorStop(0, '#081527'); bg.addColorStop(0.6, '#050d1c'); bg.addColorStop(1, '#02060f')
      ctx.fillStyle = bg; ctx.fillRect(0, 0, W, H)
      ctx.globalCompositeOperation = 'lighter'
      for (const s of stars) {
        const a = 0.1 + (0.4 + 0.4 * Math.sin(t * s.z + s.tw)) * s.z
        ctx.fillStyle = `rgba(170,220,255,${a.toFixed(3)})`
        ctx.beginPath(); ctx.arc(s.x * W, s.y * H, s.r * s.z, 0, 7); ctx.fill()
      }
      if (!me) { ctx.globalCompositeOperation = 'source-over'; return }

      // apply gentle bob to positions
      for (const n of nodes) {
        (n as any)._sx = n.x + (n.key === '__me__' ? 0 : Math.sin(t * 0.6 + n.bob) * 5)
        ;(n as any)._sy = n.y + (n.key === '__me__' ? 0 : Math.cos(t * 0.55 + n.bob) * 5)
      }
      const sel = selRef.current, hov = hoverRef.current
      const mePos = screen({ x: (me as any)._sx, y: (me as any)._sy })

      // edges: me → everyone (+ the manager→skip chain)
      for (const n of nodes) {
        if (n.key === '__me__') continue
        const rel = n.p.relationship
        const from = rel === 'skip'
          ? nodes.find(m => m.p.relationship === 'manager') || me : me
        const a = screen({ x: (from as any)._sx, y: (from as any)._sy })
        const b = screen({ x: (n as any)._sx, y: (n as any)._sy })
        const focused = sel && (n.key === sel || from.key === sel)
        const base = (rel === 'manager' || rel === 'skip') ? 0.4 : 0.18
        ctx.strokeStyle = rgba(n.color, focused ? 0.75 : base)
        ctx.lineWidth = focused ? 2 : (rel === 'manager' || rel === 'skip' ? 1.6 : 1)
        ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke()
        // travelling synapse particle
        const fp = ((t * 0.25 + n.bob) % 1)
        const px = a.x + (b.x - a.x) * fp, py = a.y + (b.y - a.y) * fp
        ctx.fillStyle = rgba(n.color, focused ? 0.9 : 0.5)
        ctx.beginPath(); ctx.arc(px, py, focused ? 2.6 : 1.8, 0, 7); ctx.fill()
      }

      // nodes
      const zoom = camRef.current.zoom
      const draw = (n: Node) => {
        const pos = screen({ x: (n as any)._sx, y: (n as any)._sy })
        const isSel = n.key === sel, isHov = n.key === hov
        const rad = n.r * zoom * (isSel ? 1.35 : isHov ? 1.15 : 1)
        const hasTasks = n.p.assigned_to_me.length > 0
        // halo
        const g = ctx.createRadialGradient(pos.x, pos.y, 0, pos.x, pos.y, rad * 3.4)
        g.addColorStop(0, rgba(n.color, 0.5)); g.addColorStop(0.5, rgba(n.color, 0.14)); g.addColorStop(1, rgba(n.color, 0))
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(pos.x, pos.y, rad * 3.4, 0, 7); ctx.fill()
        // core
        ctx.fillStyle = rgba(n.color, 0.95); ctx.beginPath(); ctx.arc(pos.x, pos.y, rad, 0, 7); ctx.fill()
        ctx.fillStyle = rgba('#ffffff', 0.9); ctx.beginPath(); ctx.arc(pos.x, pos.y, rad * 0.4, 0, 7); ctx.fill()
        // red ring if they've assigned you tasks
        if (hasTasks) {
          ctx.strokeStyle = rgba('#ff3b3b', 0.8 * (0.6 + 0.4 * Math.sin(t * 3 + n.bob)))
          ctx.lineWidth = 1.6; ctx.beginPath(); ctx.arc(pos.x, pos.y, rad * 1.9, 0, 7); ctx.stroke()
        }
        return pos
      }
      ctx.globalCompositeOperation = 'lighter'
      for (const n of nodes) if (n.key !== '__me__') draw(n)
      // me last, on top
      ;(() => {
        const g = ctx.createRadialGradient(mePos.x, mePos.y, 0, mePos.x, mePos.y, me.r * zoom * 4)
        g.addColorStop(0, rgba(me.color, 0.6)); g.addColorStop(0.5, rgba('#00e5ff', 0.18)); g.addColorStop(1, 'rgba(0,0,0,0)')
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(mePos.x, mePos.y, me.r * zoom * 4, 0, 7); ctx.fill()
        ctx.fillStyle = rgba('#00e5ff', 0.9); ctx.beginPath(); ctx.arc(mePos.x, mePos.y, me.r * zoom, 0, 7); ctx.fill()
        ctx.fillStyle = '#ffffff'; ctx.beginPath(); ctx.arc(mePos.x, mePos.y, me.r * zoom * 0.5, 0, 7); ctx.fill()
      })()

      // labels
      ctx.globalCompositeOperation = 'source-over'
      ctx.textAlign = 'center'; ctx.textBaseline = 'top'
      for (const n of nodes) {
        const pos = screen({ x: (n as any)._sx, y: (n as any)._sy })
        const isSel = n.key === sel, isHov = n.key === hov
        const big = n.key === '__me__' || n.p.relationship === 'manager' || n.p.relationship === 'skip'
        if (!isSel && !isHov && !big && zoom < 0.85) continue
        const rad = n.r * zoom
        ctx.font = `${big ? 700 : 400} ${big ? 12 : 10.5}px 'Share Tech Mono', monospace`
        ctx.shadowColor = rgba(n.color, 0.9); ctx.shadowBlur = isSel || isHov ? 10 : 5
        ctx.fillStyle = isSel || isHov ? '#eafaff' : rgba(n.color, 0.9)
        const nm = n.p.name.length > 22 ? n.p.name.slice(0, 21) + '…' : n.p.name
        ctx.fillText(nm, pos.x, pos.y + rad + 6)
        ctx.shadowBlur = 0
      }
    }
    raf = requestAnimationFrame(frame)

    // interaction
    const hit = (mx: number, my: number): Node | null => {
      const { zoom, panX, panY } = camRef.current
      let best: Node | null = null, bd = 1e9
      for (const n of nodesRef.current) {
        const x = cx + ((n as any)._sx ?? n.x) * zoom + panX
        const y = cy + ((n as any)._sy ?? n.y) * zoom + panY
        const r = Math.max(12, n.r * zoom * 1.6)
        const d = Math.hypot(mx - x, my - y)
        if (d < r && d < bd) { bd = d; best = n }
      }
      return best
    }
    let dragging = false, moved = false, lx = 0, ly = 0
    const onDown = (e: PointerEvent) => { dragging = true; moved = false; lx = e.clientX; ly = e.clientY; canvas.setPointerCapture(e.pointerId) }
    const onMove = (e: PointerEvent) => {
      const rect = canvas.getBoundingClientRect()
      const mx = e.clientX - rect.left, my = e.clientY - rect.top
      if (dragging) {
        const dx = e.clientX - lx, dy = e.clientY - ly; lx = e.clientX; ly = e.clientY
        if (Math.abs(dx) + Math.abs(dy) > 2) moved = true
        camRef.current.panX += dx; camRef.current.panY += dy
        return
      }
      const h = hit(mx, my)
      hoverRef.current = h ? h.key : null
      canvas.style.cursor = h ? 'pointer' : 'grab'
    }
    const onUp = (e: PointerEvent) => {
      if (!dragging) return
      dragging = false
      if (!moved) {
        const rect = canvas.getBoundingClientRect()
        const h = hit(e.clientX - rect.left, e.clientY - rect.top)
        setSelKey(h && h.key !== '__me__' ? h.key : null)
      }
    }
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const f = Math.exp(-e.deltaY * 0.0012)
      camRef.current.zoom = Math.max(0.4, Math.min(2.6, camRef.current.zoom * f))
    }
    canvas.addEventListener('pointerdown', onDown)
    canvas.addEventListener('pointermove', onMove)
    canvas.addEventListener('pointerup', onUp)
    canvas.addEventListener('wheel', onWheel, { passive: false })

    return () => {
      disposed = true; cancelAnimationFrame(raf)
      window.removeEventListener('resize', onResize)
      canvas.removeEventListener('pointerdown', onDown)
      canvas.removeEventListener('pointermove', onMove)
      canvas.removeEventListener('pointerup', onUp)
      canvas.removeEventListener('wheel', onWheel)
    }
  }, [])

  const people = data?.people || []
  const sel = people.find(p => p.key === selKey) || null
  const grouped: Record<string, Person[]> = {}
  for (const p of people) (grouped[p.relationship] ||= []).push(p)
  const groupOrder = Object.keys(REL).filter(r => r !== 'me' && grouped[r]?.length)

  const focusPerson = (key: string) => {
    setSelKey(key)
    const n = nodesRef.current.find(x => x.key === key)
    if (n) { camRef.current.panX = -n.x * camRef.current.zoom; camRef.current.panY = -n.y * camRef.current.zoom }
  }

  const S = STYLES
  return (
    <div style={S.root}>
      {/* top bar */}
      <div style={S.top}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
          <button style={S.back} onClick={() => onNavigate?.('home')}>◄ HOME</button>
          <div style={S.brand}>◈ DOSSIER</div>
          <div style={S.sub}>PEOPLE · PROMISES · TASKS</div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
          {data?.stats && (
            <div style={S.stats}>
              <span><b>{data.stats.people}</b> people</span>
              <span><b style={{ color: '#ff6a3d' }}>{data.stats.with_tasks}</b> owe you</span>
              <span><b style={{ color: '#00e5ff' }}>{data.stats.promises}</b> promises</span>
            </div>
          )}
          <button style={{ ...S.sweep, opacity: sweeping ? 0.6 : 1 }} disabled={sweeping} onClick={sweep}>
            {sweeping ? 'SWEEPING…' : '⟳ SWEEP LAST 2 WEEKS'}
          </button>
        </div>
      </div>

      {status && <div style={S.statusBar}>{status}</div>}

      <div style={S.body}>
        {/* org-tree sidebar */}
        <div style={S.side}>
          <div style={S.sideHdr}>ORG TREE</div>
          {data?.org?.skip?.name && (
            <div style={S.orgHead}><span style={{ color: REL.skip.color }}>▲ SKIP</span> {data.org.skip.name}</div>
          )}
          {data?.org?.manager?.name && (
            <div style={S.orgHead}><span style={{ color: REL.manager.color }}>▲ MGR</span> {data.org.manager.name}</div>
          )}
          <div style={{ ...S.orgHead, color: '#eafaff', borderColor: 'rgba(0,229,255,0.4)' }}>◉ YOU</div>
          {groupOrder.length === 0 && !offline && (
            <div style={S.empty}>No people yet. Hit <b>SWEEP</b> to pull the last 2 weeks from Slack &amp; Gmail.</div>
          )}
          {offline && <div style={{ ...S.empty, color: '#ff6a3d' }}>Backend offline — start it and reload.</div>}
          {groupOrder.map(rel => (
            <div key={rel} style={{ marginBottom: 10 }}>
              <div style={{ ...S.groupHdr, color: relColor(rel) }}>
                {REL[rel].label} · {grouped[rel].length}
              </div>
              {grouped[rel].slice().sort((a, b) => b.assigned_to_me.length - a.assigned_to_me.length).map(p => (
                <button key={p.key} onClick={() => focusPerson(p.key)}
                  style={{ ...S.pRow, borderColor: p.key === selKey ? relColor(rel) : 'transparent',
                    background: p.key === selKey ? 'rgba(0,180,220,0.1)' : 'transparent' }}>
                  <span style={{ ...S.dot, background: relColor(rel) }} />
                  <span style={S.pName}>{p.name}</span>
                  {p.assigned_to_me.length > 0 && <span style={S.taskBadge}>{p.assigned_to_me.length}</span>}
                </button>
              ))}
            </div>
          ))}
        </div>

        {/* graph */}
        <div style={S.graphWrap}>
          <canvas ref={canvasRef} style={{ display: 'block', width: '100%', height: '100%' }} />
          <div style={S.legend}>
            {['manager', 'skip', 'direct', 'extended', 'hr', 'external'].map(r => (
              <div key={r} style={S.legRow}><span style={{ ...S.dot, background: relColor(r) }} />{REL[r].label}</div>
            ))}
            <div style={{ ...S.legRow, marginTop: 6, color: '#ff6a3d' }}><span style={{ ...S.ring }} />assigned you a task</div>
          </div>
          {data?.generated && <div style={S.gen}>swept {new Date(data.generated).toLocaleString()} · {data.window_days}d window</div>}
        </div>

        {/* detail panel */}
        {sel && (
          <div style={{ ...S.detail, borderColor: relColor(sel.relationship) }}>
            <button style={S.close} onClick={() => setSelKey(null)}>✕</button>
            <div style={{ ...S.kicker, color: relColor(sel.relationship) }}>{REL[sel.relationship]?.label || sel.relationship}</div>
            <div style={S.dName}>{sel.name}</div>
            {sel.role && <div style={S.role}>{sel.role}</div>}
            {sel.email && <a href={`mailto:${sel.email}`} style={S.email}>{sel.email}</a>}
            {sel.sources?.length > 0 && (
              <div style={S.chips}>{sel.sources.map(s => <span key={s} style={S.chip}>{s}</span>)}</div>
            )}
            {sel.summary && <div style={S.summary}>{sel.summary}</div>}
            <Section title="TASKS THEY ASSIGNED YOU" accent="#ff6a3d" items={sel.assigned_to_me} />
            <Section title="THEIR PROMISES" accent="#00e5ff" items={sel.promises} />
            <Section title="WORKING ON" accent="#9a7bff" items={sel.working_on} />
            {sel.assigned_to_me.length + sel.promises.length + sel.working_on.length === 0 && (
              <div style={S.noneYet}>No tasks or promises captured yet — they're in your org tree.</div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function Section({ title, accent, items }: { title: string; accent: string; items: Item[] }) {
  if (!items || items.length === 0) return null
  return (
    <div style={{ marginTop: 16 }}>
      <div style={{ fontFamily: 'var(--font-d,monospace)', fontSize: 9.5, letterSpacing: '0.18em',
        color: accent, textTransform: 'uppercase', marginBottom: 8 }}>{title} · {items.length}</div>
      {items.map((it, i) => (
        <div key={i} style={{ borderLeft: `2px solid ${accent}`, paddingLeft: 10, marginBottom: 10 }}>
          <div style={{ fontSize: 13, color: '#dff2ff', lineHeight: 1.5 }}>{it.text}</div>
          {it.evidence && <div style={{ fontSize: 10.5, color: '#7fa6b8', fontStyle: 'italic', marginTop: 3 }}>"{it.evidence}"</div>}
          {it.source && <div style={{ fontSize: 9, color: '#5f8ea0', letterSpacing: '0.1em', marginTop: 3 }}>{it.source}</div>}
        </div>
      ))}
    </div>
  )
}

const font = "'Share Tech Mono', ui-monospace, monospace"
const STYLES: Record<string, React.CSSProperties> = {
  root: { position: 'fixed', inset: 0, background: '#040a16', color: '#cfefff', fontFamily: font,
    display: 'flex', flexDirection: 'column', overflow: 'hidden', userSelect: 'none' },
  top: { height: 52, flex: '0 0 52px', display: 'flex', alignItems: 'center', justifyContent: 'space-between',
    padding: '0 20px', borderBottom: '1px solid rgba(0,229,255,0.18)', background: 'rgba(6,14,28,0.6)', backdropFilter: 'blur(10px)' },
  back: { cursor: 'pointer', fontFamily: font, fontSize: 11, letterSpacing: '0.14em', color: '#9fd6ea',
    padding: '6px 12px', borderRadius: 7, border: '1px solid rgba(0,229,255,0.22)', background: 'rgba(8,18,34,0.5)' },
  brand: { fontFamily: 'Orbitron, monospace', fontWeight: 800, letterSpacing: '0.28em', fontSize: 15,
    color: '#eafaff', textShadow: '0 0 14px rgba(0,229,255,0.5)' },
  sub: { fontSize: 10, letterSpacing: '0.18em', color: '#5f8ea0' },
  stats: { display: 'flex', gap: 16, fontSize: 12, color: '#9fc2d4' },
  sweep: { cursor: 'pointer', fontFamily: font, fontSize: 11, fontWeight: 700, letterSpacing: '0.1em',
    color: '#02121c', background: 'linear-gradient(180deg,#26e0ff,#00b6e6)', border: 'none',
    padding: '9px 16px', borderRadius: 8, boxShadow: '0 0 18px rgba(0,212,255,0.4)' },
  statusBar: { flex: '0 0 auto', padding: '7px 20px', fontSize: 11.5, color: '#8fdcff',
    background: 'rgba(0,60,90,0.25)', borderBottom: '1px solid rgba(0,229,255,0.12)' },
  body: { flex: 1, minHeight: 0, display: 'flex', position: 'relative' },
  side: { width: 250, flex: '0 0 250px', overflowY: 'auto', padding: '14px 12px',
    borderRight: '1px solid rgba(0,229,255,0.12)', background: 'rgba(4,10,22,0.5)' },
  sideHdr: { fontSize: 9.5, letterSpacing: '0.24em', color: '#5f8ea0', marginBottom: 12 },
  orgHead: { fontSize: 11.5, color: '#bfe6f2', padding: '6px 8px', marginBottom: 4, borderRadius: 6,
    border: '1px solid rgba(255,255,255,0.06)', letterSpacing: '0.04em' },
  empty: { fontSize: 11.5, color: '#7fa6b8', lineHeight: 1.7, marginTop: 14, padding: '0 4px' },
  groupHdr: { fontSize: 9.5, letterSpacing: '0.18em', textTransform: 'uppercase', margin: '4px 0 6px 2px' },
  pRow: { display: 'flex', alignItems: 'center', gap: 8, width: '100%', textAlign: 'left', cursor: 'pointer',
    background: 'transparent', border: '1px solid transparent', borderRadius: 6, padding: '5px 8px', color: '#cfefff', fontFamily: font },
  dot: { width: 8, height: 8, borderRadius: '50%', flex: 'none' },
  ring: { width: 9, height: 9, borderRadius: '50%', border: '1.6px solid #ff3b3b', flex: 'none' },
  pName: { fontSize: 12, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
  taskBadge: { fontSize: 9.5, fontWeight: 700, color: '#04121f', background: '#ff6a3d', borderRadius: 10, padding: '1px 7px' },
  graphWrap: { flex: 1, minWidth: 0, position: 'relative' },
  legend: { position: 'absolute', left: 14, bottom: 14, background: 'rgba(8,18,34,0.5)', backdropFilter: 'blur(10px)',
    border: '1px solid rgba(0,229,255,0.18)', borderRadius: 10, padding: '10px 12px', pointerEvents: 'none' },
  legRow: { display: 'flex', alignItems: 'center', gap: 8, fontSize: 11, color: '#bfe6f2', lineHeight: 1.9 },
  gen: { position: 'absolute', right: 14, bottom: 14, fontSize: 10, color: '#4f7688', letterSpacing: '0.06em' },
  detail: { position: 'absolute', right: 0, top: 0, bottom: 0, width: 'min(340px,42vw)', overflowY: 'auto',
    background: 'linear-gradient(180deg,rgba(9,20,38,0.94),rgba(6,14,28,0.97))', backdropFilter: 'blur(16px)',
    borderLeft: '1px solid', padding: 22, boxShadow: '-14px 0 50px rgba(0,0,0,0.5)' },
  close: { position: 'absolute', top: 12, right: 12, width: 26, height: 26, borderRadius: 7, cursor: 'pointer',
    color: '#9fd0e0', background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.1)', fontSize: 13 },
  kicker: { fontSize: 10, letterSpacing: '0.22em', textTransform: 'uppercase', marginBottom: 8 },
  dName: { fontFamily: 'Orbitron, monospace', fontWeight: 700, fontSize: 20, color: '#f2fbff', lineHeight: 1.2, paddingRight: 24 },
  role: { fontSize: 12, color: '#9fc2d4', marginTop: 6 },
  email: { display: 'inline-block', fontSize: 11.5, color: '#00e5ff', marginTop: 6, textDecoration: 'none' },
  chips: { display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 12 },
  chip: { fontSize: 10, color: '#bfe6f2', padding: '3px 8px', borderRadius: 6, background: 'rgba(0,180,220,0.08)', border: '1px solid rgba(0,180,220,0.18)' },
  summary: { fontSize: 12.5, color: '#a9cdda', lineHeight: 1.6, marginTop: 14, fontStyle: 'italic' },
  noneYet: { fontSize: 12, color: '#7fa6b8', marginTop: 16, lineHeight: 1.6 },
}
