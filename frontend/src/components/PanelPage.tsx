import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent, ReactNode } from 'react'
import PageShell, { OfflineBanner } from './PageShell'
import Markdown from './Markdown'
import type { Page } from '../store'

// ══════════════════════════════════════════════════════════════════════════
// PANEL — a workspace of independent agent sessions, rebuilt for instant
// legibility. 2D, full page width, nothing fights the scroll wheel:
//
//   · every session is a readable card in a fluid grid (who / status / model
//     / what it's doing RIGHT NOW)
//   · directed links are drawn as SVG energy lines between cards — a constant
//     slow flow shows direction; a bright pulse rides the line when context
//     actually transfers (peer_send / peer_fetch)
//   · PROJECT MEMORY is a permanent right-hand rail (never a drawer):
//     the shared ledger, grouped by connected component, auto-following
//   · click a card → full 2D chat view. ⚡ starts a two-click link flow with
//     a loud banner and backend errors surfaced verbatim.
//
// Accessibility: cards and actions are keyboard operable, status is always
// color + text, icon buttons carry aria-labels, and every ambient animation
// honours prefers-reduced-motion or the in-app data-motion='reduced' toggle.
// ══════════════════════════════════════════════════════════════════════════

// ─── Types (mirror the backend contract) ──────────────────────────────────
interface ChatEntry { who: string; text: string; t: string }
interface FeedRow {
  kind: string; t: string; text?: string; tool?: string; label?: string
  ok?: boolean; detail?: string; from?: string; to?: string
}
interface Session {
  id: string; name: string; model: string; status: string; seat: number
  created?: string; current?: string; persona?: string
  chat: ChatEntry[]; feed: FeedRow[]; inbox?: unknown[]; ruflo_id?: string
}
interface Conn { from: string; to: string }
interface LedgerEntry { t: string; author: string; author_name?: string; line: string; audience?: string[] }
interface Rect { x: number; y: number; w: number; h: number }
interface Template { id: string; label: string; desc: string; size: number; mode: string }
interface Deliverable {
  id: string; title: string; content: string
  author: string; author_name: string; task?: string; team?: string[]; t: string
}
interface GroupTeam {
  members: string[]; names: string[]; merger: string
  done: number; total: number; merge_started: boolean; merged: boolean
}
interface GroupTask { id: string; text: string; t: string; teams: GroupTeam[] }

// Quick-pick roles for new sessions; free text always allowed in the editor.
const PERSONA_PRESETS: { label: string; text: string }[] = [
  { label: 'Devil\'s advocate', text: 'The devil\'s advocate. Attack every proposal: failure modes, hidden costs, edge cases, risks. If something survives your attack, say exactly what convinced you.' },
  { label: 'Judge', text: 'The impartial judge. Weigh every argument on evidence and logic, demand support for claims, and deliver a decisive, balanced verdict with the reasoning spelled out.' },
  { label: 'Advocate', text: 'The advocate. Make the strongest possible case FOR the proposal: benefits, opportunities, why it works. Steelman it; concede nothing without a fight.' },
  { label: 'Pragmatist', text: 'The pragmatist. Ground everything in cost, effort and time-to-ship. Prefer boring proven approaches; flag overengineering ruthlessly.' },
  { label: 'Visionary', text: 'The visionary. Propose bold, unconventional ideas others wouldn\'t dare. Ignore short-term constraints; optimize for wow and long-term leverage.' },
  { label: 'Researcher', text: 'The researcher. Gather evidence before opining: search, read, verify claims against primary sources, and cite what you found.' },
  { label: 'Security reviewer', text: 'The security reviewer. Hunt injection, authz gaps, secret leaks, unsafe input handling, path traversal. Cite exact locations for every finding.' },
  { label: 'User advocate', text: 'The user advocate. Champion the end user: simplicity, clarity, delight. Reject anything user-hostile no matter how clever.' },
  { label: 'Synthesizer', text: 'The synthesizer. Merge everyone\'s findings into one coherent, de-duplicated answer: what\'s known, what\'s uncertain, what to do next.' },
]

// ─── Status + identity constants ──────────────────────────────────────────
const STATUS: Record<string, { color: string; label: string }> = {
  idle:    { color: '#5d8aa8', label: 'IDLE' },
  working: { color: '#33c6ff', label: 'WORKING' },
  error:   { color: '#ff5566', label: 'ERROR' },
}
const statusOf = (s?: string) => STATUS[s || 'idle'] || STATUS.idle

// One stable color per agent (by seat) — the same hue tags its card, its
// outgoing edges, its ledger lines and its chat bubbles, so "who did what"
// reads at a glance everywhere.
const AGENT_COLORS = ['#00d4ff', '#00ff88', '#b07aff', '#ffb340', '#ff6fae', '#ffe14d', '#6f9bff', '#5ee8d8']
const colorOf = (s?: Session) => (s ? AGENT_COLORS[Math.abs(s.seat) % AGENT_COLORS.length] : '#5d8aa8')

// Each connected cluster gets its own accent — its memory panel and the
// group tags on its cards share it, so cluster ↔ ledger pairing is obvious.
const GROUP_ACCENTS = ['#00d4ff', '#00ff88', '#b07aff', '#ffb340', '#ff6fae', '#5ee8d8']
const groupLabel = (i: number) => `GROUP ${'ABCDEFGH'[i] || i + 1}`

const mono: CSSProperties = { fontFamily: 'var(--font-m)', letterSpacing: '0.05em' }
const disp: CSSProperties = { fontFamily: 'var(--font-d)' }

const shortModel = (m: string) => {
  if (!m) return 'default'
  const parts = m.replace(/^claude-/, '').split('-')
  return parts.slice(0, 2).join('-') || m
}
const fmtT = (t?: string) => {
  if (!t) return '--:--:--'
  const d = new Date(t)
  if (!isNaN(d.getTime())) return d.toTimeString().slice(0, 8)
  return t.length >= 19 ? t.slice(11, 19) : t
}

// ─── Feed row → glyph + text + tone (shared by cards, focus view, rail) ───
function feedView(e: FeedRow, nameOf: (sid?: string) => string): { icon: string; text: string; color: string } {
  switch (e.kind) {
    case 'thought':    return { icon: '✦', text: e.text || 'thinking…', color: 'var(--text)' }
    case 'tool':       return { icon: '→', text: `${e.tool || 'tool'} ${e.label || e.detail || ''}`.trim(), color: 'var(--cyan)' }
    case 'tool_done':  return e.ok === false
      ? { icon: '✕', text: e.label || e.tool || e.detail || 'failed', color: '#ff5566' }
      : { icon: '✓', text: e.label || e.tool || e.detail || 'done', color: '#00ff88' }
    case 'peer_out':   return { icon: '↗', text: `sent → ${nameOf(e.to)}${e.text ? ` · ${e.text}` : ''}`, color: '#b07aff' }
    case 'peer_in':    return { icon: '↘', text: `received ← ${nameOf(e.from)}${e.text ? ` · ${e.text}` : ''}`, color: '#b07aff' }
    case 'peer_fetch': return { icon: '⇣', text: `fetched ${nameOf(e.from || e.to)}'s transcript`, color: '#b07aff' }
    case 'error':      return { icon: '⚠', text: e.text || e.detail || 'error', color: '#ff5566' }
    default:           return { icon: '·', text: e.text || e.detail || e.kind, color: 'var(--text-lo)' }
  }
}

// ─── Reduced motion: OS preference OR the in-app Settings toggle ──────────
function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() =>
    (typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches) ||
    document.documentElement.getAttribute('data-motion') === 'reduced')
  useEffect(() => {
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)')
    const compute = () => setReduced(mq.matches || document.documentElement.getAttribute('data-motion') === 'reduced')
    mq.addEventListener('change', compute)
    const mo = new MutationObserver(compute)
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-motion'] })
    return () => { mq.removeEventListener('change', compute); mo.disconnect() }
  }, [])
  return reduced
}

// ─── Component-scoped CSS: keyframes, hover states, responsive columns ────
const PP_CSS = `
@keyframes ppFlow { to { stroke-dashoffset: -14; } }
@keyframes ppRowIn { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: none; } }
@keyframes ppCardIn { from { opacity: 0; transform: translateY(8px) scale(0.985); } to { opacity: 1; transform: none; } }
@keyframes ppFadeIn { from { opacity: 0; } to { opacity: 1; } }
@keyframes ppPanelIn { from { opacity: 0; transform: translateY(12px) scale(0.992); } to { opacity: 1; transform: none; } }
@keyframes ppDot { 0%, 100% { opacity: 0.25; } 50% { opacity: 1; } }
.pp-flow { animation: ppFlow 0.85s linear infinite; }
.pp-card { animation: ppCardIn 0.24s ease-out; transition: border-color 0.15s, box-shadow 0.15s, transform 0.15s; }
.pp-card:hover { transform: translateY(-2px); }
.pp-card:focus-visible { outline: 1.5px solid var(--cyan-50); outline-offset: 3px; }
.pp-row-in { animation: ppRowIn 0.22s ease-out; }
.pp-overlay { animation: ppFadeIn 0.16s ease-out; }
.pp-panel-in { animation: ppPanelIn 0.2s ease-out; }
.pp-main { display: grid; grid-template-columns: minmax(0, 1fr) clamp(330px, 26vw, 480px); gap: 16px; align-items: start; }
.pp-focus-body { display: grid; grid-template-columns: minmax(0, 1fr) 340px; min-height: 0; }
@media (max-width: 1180px) {
  .pp-main { grid-template-columns: 1fr; }
  .pp-mem { position: static !important; max-height: 440px !important; }
}
@media (max-width: 940px) { .pp-focus-body { grid-template-columns: 1fr; } .pp-focus-side { display: none; } }
@media (prefers-reduced-motion: reduce) {
  .pp-flow, .pp-card, .pp-row-in, .pp-overlay, .pp-panel-in, .pp-dot { animation: none !important; }
  .pp-card:hover { transform: none; }
}
html[data-motion='reduced'] .pp-flow, html[data-motion='reduced'] .pp-card,
html[data-motion='reduced'] .pp-row-in, html[data-motion='reduced'] .pp-overlay,
html[data-motion='reduced'] .pp-panel-in, html[data-motion='reduced'] .pp-dot { animation: none !important; }
html[data-motion='reduced'] .pp-card:hover { transform: none; }
`

// ─── Small atoms ───────────────────────────────────────────────────────────
function HudButton({ children, onClick, color = 'var(--cyan)', border = 'var(--border-hi)', bg = 'transparent',
  title, ariaLabel, disabled, style }: {
  children: ReactNode; onClick?: () => void; color?: string; border?: string; bg?: string
  title?: string; ariaLabel?: string; disabled?: boolean; style?: CSSProperties
}) {
  return (
    <button type="button" onClick={onClick} title={title} aria-label={ariaLabel || title} disabled={disabled}
      style={{ ...mono, fontSize: 11, fontWeight: 700, letterSpacing: '0.1em', color, background: bg,
        border: `1px solid ${border}`, borderRadius: 6, padding: '8px 13px', cursor: disabled ? 'default' : 'pointer',
        opacity: disabled ? 0.4 : 1, whiteSpace: 'nowrap', minHeight: 32, ...style }}>
      {children}
    </button>
  )
}

// Destructive actions arm on first click ("SURE?") instead of a modal.
function ConfirmButton({ label, confirmLabel = '✕ SURE?', onConfirm, ariaLabel }: {
  label: string; confirmLabel?: string; onConfirm: () => void; ariaLabel: string
}) {
  const [armed, setArmed] = useState(false)
  useEffect(() => {
    if (!armed) return
    const id = window.setTimeout(() => setArmed(false), 2600)
    return () => window.clearTimeout(id)
  }, [armed])
  return (
    <HudButton color="#ff5566" border={armed ? '#ff5566' : 'rgba(255,85,102,0.4)'}
      bg={armed ? 'rgba(255,34,68,0.12)' : 'transparent'}
      ariaLabel={armed ? `confirm: ${ariaLabel}` : ariaLabel}
      onClick={() => { if (armed) { setArmed(false); onConfirm() } else setArmed(true) }}>
      {armed ? confirmLabel : label}
    </HudButton>
  )
}

function StatusBadge({ status, reduced }: { status: string; reduced: boolean }) {
  const st = statusOf(status)
  return (
    <span style={{ ...mono, display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 10,
      fontWeight: 700, letterSpacing: '0.16em', color: st.color }}>
      <span className={status === 'working' && !reduced ? 'status-dot' : undefined}
        style={{ width: 7, height: 7, borderRadius: '50%', background: st.color,
          boxShadow: `0 0 8px ${st.color}`, flexShrink: 0 }} />
      {st.label}
    </span>
  )
}

// Working ellipsis — three pulsing dots (static when reduced motion).
function WorkingDots({ color = 'var(--cyan)' }: { color?: string }) {
  return (
    <span aria-hidden="true" style={{ display: 'inline-flex', gap: 3, marginLeft: 2 }}>
      {[0, 1, 2].map(i => (
        <span key={i} className="pp-dot" style={{ width: 4, height: 4, borderRadius: '50%', background: color,
          animation: `ppDot 1.1s ease-in-out ${i * 0.18}s infinite` }} />
      ))}
    </span>
  )
}

// ─── The at-a-glance activity block shared by cards ────────────────────────
function ActivityGlance({ s, nameOf }: { s: Session; nameOf: (sid?: string) => string }) {
  const last = s.feed && s.feed.length ? s.feed[s.feed.length - 1] : undefined
  if (s.status === 'working') {
    const fv = last ? feedView(last, nameOf) : null
    return (
      <div className={undefined} style={{ display: 'flex', flexDirection: 'column', gap: 5, minWidth: 0 }}>
        {s.current ? (
          <div style={{ fontFamily: 'var(--font-b)', fontSize: 12, color: 'var(--text)', lineHeight: 1.45,
            overflow: 'hidden', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' }}>
            {s.current}
          </div>
        ) : null}
        <div className="tool-running" style={{ ...mono, fontSize: 11.5, color: fv ? fv.color : 'var(--cyan)',
          display: 'flex', alignItems: 'baseline', gap: 7, padding: '5px 8px', borderRadius: 5,
          background: 'rgba(0,212,255,0.05)', border: '1px solid rgba(0,212,255,0.12)', minWidth: 0 }}>
          <span aria-hidden="true" style={{ flexShrink: 0 }}>{fv ? fv.icon : '·'}</span>
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}>
            {fv ? fv.text : 'working'}
          </span>
          <WorkingDots />
        </div>
      </div>
    )
  }
  if (s.status === 'error') {
    const err = s.feed ? [...s.feed].reverse().find(e => e.kind === 'error') : undefined
    return (
      <div style={{ ...mono, fontSize: 11.5, color: '#ff8896', display: 'flex', gap: 7, alignItems: 'baseline',
        padding: '5px 8px', borderRadius: 5, background: 'rgba(255,34,68,0.06)',
        border: '1px solid rgba(255,34,68,0.25)' }}>
        <span aria-hidden="true">⚠</span>
        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}>
          {err ? (err.text || err.detail || 'error') : 'errored — open to inspect'}
        </span>
      </div>
    )
  }
  // idle → last exchange snippet
  const chat = s.chat || []
  const lastMsg = chat[chat.length - 1]
  if (!lastMsg) {
    return <div style={{ ...mono, fontSize: 11.5, color: 'var(--text-lo)' }}>new session — click to start chatting</div>
  }
  const prev = chat.length > 1 ? chat[chat.length - 2] : undefined
  const row = (m: ChatEntry, key: string) => (
    <div key={key} style={{ display: 'flex', gap: 7, alignItems: 'baseline', minWidth: 0 }}>
      <span style={{ ...mono, fontSize: 9.5, fontWeight: 700, letterSpacing: '0.12em', flexShrink: 0,
        color: m.who === 'you' ? 'var(--amber)' : 'var(--cyan)' }}>
        {m.who === 'you' ? 'YOU' : m.who.toUpperCase()}
      </span>
      <span style={{ fontFamily: 'var(--font-b)', fontSize: 12, color: 'var(--text)', overflow: 'hidden',
        textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}>{m.text}</span>
    </div>
  )
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0 }}>
      {prev ? row(prev, 'p') : null}
      {row(lastMsg, 'l')}
    </div>
  )
}

// ─── Session card ──────────────────────────────────────────────────────────
function SessionCard({ s, sessions, connections, connectFrom, reduced, nameOf, refCb, groupIdx, groupAccent,
  onOpen, onPickTarget, onCancelConnect, onStartConnect, onStop, onHover }: {
  s: Session; sessions: Record<string, Session>; connections: Conn[]
  connectFrom: string | null; reduced: boolean; nameOf: (sid?: string) => string
  refCb: (el: HTMLDivElement | null) => void; groupIdx: number; groupAccent: string
  onOpen: () => void; onPickTarget: () => void; onCancelConnect: () => void
  onStartConnect: () => void; onStop: () => void; onHover: (sid: string | null) => void
}) {
  const st = statusOf(s.status)
  const ac = colorOf(s)
  const connecting = !!connectFrom
  const isSource = connectFrom === s.id
  // Links are bidirectional — one merged peer list, whichever way the edge is stored.
  const linked = [...new Set(connections
    .filter(c => c.from === s.id || c.to === s.id)
    .map(c => (c.from === s.id ? c.to : c.from))
    .filter(sid => sessions[sid]))]
  const inboxN = Array.isArray(s.inbox) ? s.inbox.length : 0

  const border = isSource ? 'var(--amber)'
    : connecting ? 'rgba(255,149,0,0.45)'
    : s.status === 'working' ? 'rgba(0,212,255,0.45)'
    : s.status === 'error' ? 'rgba(255,34,68,0.45)'
    : 'var(--border)'
  const glow = s.status === 'working' ? '0 0 24px rgba(0,212,255,0.14), 0 8px 30px rgba(0,0,0,0.45)'
    : isSource ? '0 0 24px rgba(255,149,0,0.2), 0 8px 30px rgba(0,0,0,0.45)'
    : '0 8px 30px rgba(0,0,0,0.45)'

  const click = () => {
    if (isSource) onCancelConnect()
    else if (connecting) onPickTarget()
    else onOpen()
  }
  const keydown = (e: ReactKeyboardEvent) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); click() }
  }
  const aria = isSource ? `linking from ${s.name} — press to cancel`
    : connecting ? `link ${nameOf(connectFrom || undefined)} with ${s.name} (both ways)`
    : `open ${s.name} session — ${st.label.toLowerCase()}`

  return (
    <div ref={refCb} role="button" tabIndex={0} aria-label={aria} onClick={click} onKeyDown={keydown}
      onMouseEnter={() => onHover(s.id)} onMouseLeave={() => onHover(null)}
      onFocus={() => onHover(s.id)} onBlur={() => onHover(null)}
      className="pp-card"
      style={{ position: 'relative', zIndex: 1, cursor: 'pointer', borderRadius: 10, padding: '13px 14px 12px',
        background: 'linear-gradient(165deg, rgba(7,24,46,0.92), rgba(4,14,30,0.88))',
        border: `1px solid ${border}`, boxShadow: glow, minWidth: 0,
        outlineOffset: 3, borderLeft: `3px solid ${ac}` }}>

      {/* header: identity + status */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 9, minWidth: 0 }}>
        <span style={{ ...disp, fontSize: 16, fontWeight: 800, color: 'var(--text-hi)', letterSpacing: '0.04em',
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}>
          {s.name}
        </span>
        {s.ruflo_id ? (
          <span title={`RUFLO fabric: ${s.ruflo_id}`} aria-label="RUFLO coordination attached"
            style={{ ...mono, fontSize: 10, color: '#b07aff', flexShrink: 0 }}>⬡</span>
        ) : null}
        <span style={{ flex: 1 }} />
        <StatusBadge status={s.status} reduced={reduced} />
      </div>

      {/* meta row: model + inbox */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, margin: '7px 0 9px', flexWrap: 'wrap' }}>
        <span style={{ ...mono, fontSize: 9.5, color: 'var(--text-lo)', border: '1px solid var(--border)',
          borderRadius: 4, padding: '2px 7px', letterSpacing: '0.08em' }}>
          {shortModel(s.model)}
        </span>
        {s.persona ? (
          <span title={`role: ${s.persona}`}
            style={{ ...mono, fontSize: 9.5, color: '#ffb340', border: '1px solid rgba(255,179,64,0.35)',
              background: 'rgba(255,179,64,0.06)', borderRadius: 4, padding: '2px 7px',
              maxWidth: 130, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            ☰ {s.persona.replace(/^The /i, '').split(/[.—-]/)[0].trim().slice(0, 24)}
          </span>
        ) : null}
        {groupIdx >= 0 ? (
          <span title={`shares one project memory with its group — see \"${groupLabel(groupIdx)}\" in the memory rail`}
            style={{ ...mono, fontSize: 9.5, fontWeight: 700, color: groupAccent,
              border: `1px solid ${groupAccent}55`, background: `${groupAccent}0d`,
              borderRadius: 4, padding: '2px 7px', letterSpacing: '0.08em' }}>
            ◈ {groupLabel(groupIdx)}
          </span>
        ) : null}
        {inboxN > 0 ? (
          <span style={{ ...mono, fontSize: 9.5, color: 'var(--amber)', border: '1px solid rgba(255,149,0,0.35)',
            borderRadius: 4, padding: '2px 7px' }}>✉ {inboxN} queued</span>
        ) : null}
        <span style={{ flex: 1 }} />
        {!connecting && s.status === 'working' ? (
          <button type="button" aria-label={`stop ${s.name}'s current turn`} title="stop this turn"
            onClick={e => { e.stopPropagation(); onStop() }}
            style={{ ...mono, fontSize: 9.5, fontWeight: 700, color: '#ff8896', background: 'transparent',
              border: '1px solid rgba(255,85,102,0.4)', borderRadius: 4, padding: '3px 8px', cursor: 'pointer' }}>
            ■ STOP
          </button>
        ) : null}
        {!connecting ? (
          <button type="button" aria-label={`link ${s.name} with another session`} title="link with another session — context flows BOTH ways"
            onClick={e => { e.stopPropagation(); onStartConnect() }}
            style={{ ...mono, fontSize: 9.5, fontWeight: 700, color: 'var(--amber)', background: 'transparent',
              border: '1px solid rgba(255,149,0,0.35)', borderRadius: 4, padding: '3px 8px', cursor: 'pointer' }}>
            ⚡ LINK
          </button>
        ) : (
          <span style={{ ...mono, fontSize: 9.5, fontWeight: 700, letterSpacing: '0.14em',
            color: 'var(--amber)', border: `1px dashed ${isSource ? 'var(--amber)' : 'rgba(255,149,0,0.5)'}`,
            borderRadius: 4, padding: '3px 8px' }}>
            {isSource ? 'SOURCE · CANCEL' : '⇋ LINK HERE'}
          </span>
        )}
      </div>

      {/* the glance: what is this agent doing right now */}
      <ActivityGlance s={s} nameOf={nameOf} />

      {/* wiring row — one chip per linked peer (links are bidirectional) */}
      {linked.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5, marginTop: 10 }}>
          {linked.map(sid => (
            <span key={`l${sid}`} title={`${s.name} ⇋ ${nameOf(sid)} — linked both ways`}
              style={{ ...mono, fontSize: 9.5, color: colorOf(sessions[sid]), borderRadius: 4, padding: '2px 7px',
                border: '1px solid rgba(0,212,255,0.2)', background: 'rgba(0,212,255,0.05)' }}>
              ⇋ {nameOf(sid)}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

// ─── Directed energy edges (SVG overlay behind the cards) ─────────────────
interface EdgeGeo {
  p1x: number; p1y: number; c1x: number; c1y: number
  c2x: number; c2y: number; p2x: number; p2y: number; d: string
}

function edgeGeometry(a: Rect, b: Rect, bow: number): EdgeGeo {
  const acx = a.x + a.w / 2, acy = a.y + a.h / 2
  const bcx = b.x + b.w / 2, bcy = b.y + b.h / 2
  const dx = bcx - acx, dy = bcy - acy
  let p1x: number, p1y: number, p2x: number, p2y: number, c1x: number, c1y: number, c2x: number, c2y: number
  if (Math.abs(dx) >= Math.abs(dy)) {
    const dir = dx >= 0 ? 1 : -1
    p1x = dir > 0 ? a.x + a.w : a.x; p1y = acy
    p2x = dir > 0 ? b.x - 8 : b.x + b.w + 8; p2y = bcy
    const k = Math.min(Math.max(Math.abs(p2x - p1x) * 0.42, 34), 190)
    c1x = p1x + dir * k; c1y = p1y + bow
    c2x = p2x - dir * k; c2y = p2y + bow
  } else {
    const dir = dy >= 0 ? 1 : -1
    p1x = acx; p1y = dir > 0 ? a.y + a.h : a.y
    p2x = bcx; p2y = dir > 0 ? b.y - 8 : b.y + b.h + 8
    const k = Math.min(Math.max(Math.abs(p2y - p1y) * 0.42, 34), 190)
    c1x = p1x + bow; c1y = p1y + dir * k
    c2x = p2x + bow; c2y = p2y - dir * k
  }
  return { p1x, p1y, c1x, c1y, c2x, c2y, p2x, p2y,
    d: `M ${p1x} ${p1y} C ${c1x} ${c1y}, ${c2x} ${c2y}, ${p2x} ${p2y}` }
}
const bezPoint = (g: EdgeGeo, t: number): [number, number] => {
  const u = 1 - t
  return [
    u * u * u * g.p1x + 3 * u * u * t * g.c1x + 3 * u * t * t * g.c2x + t * t * t * g.p2x,
    u * u * u * g.p1y + 3 * u * u * t * g.c1y + 3 * u * t * t * g.c2y + t * t * t * g.p2y,
  ]
}
const bezAngle = (g: EdgeGeo, t: number): number => {
  const u = 1 - t
  const dx = 3 * u * u * (g.c1x - g.p1x) + 6 * u * t * (g.c2x - g.c1x) + 3 * t * t * (g.p2x - g.c2x)
  const dy = 3 * u * u * (g.c1y - g.p1y) + 6 * u * t * (g.c2y - g.c1y) + 3 * t * t * (g.p2y - g.c2y)
  return Math.atan2(dy, dx)
}
const arrowAt = (x: number, y: number, ang: number, L: number): string => {
  const W = 0.48
  return `${x},${y} ${x - L * Math.cos(ang - W)},${y - L * Math.sin(ang - W)} ${x - L * Math.cos(ang + W)},${y - L * Math.sin(ang + W)}`
}

// One drawn link: base line + arrowheads at BOTH ends + a name label
// ("Orion ⇋ Vega") pinned to the midpoint. Links are bidirectional — either
// side can message the other and both share group memory; the only directed
// thing is a LIVE transfer, shown by the pulse riding the line (ghost edges
// carry reverse-direction transfers).
function Edge({ geo, from, to, color, toColor, hotKind, dimmed, boosted, reduced, ghost }: {
  geo: EdgeGeo; from: string; to: string; color: string; toColor: string
  hotKind?: string; dimmed: boolean; boosted: boolean; reduced: boolean; ghost?: boolean
}) {
  const hotColor = hotKind === 'fetch' ? '#c084ff' : '#8fe9ff'
  const stroke = hotKind ? hotColor : color
  const lineOp = dimmed ? 0.08 : hotKind ? 0.95 : boosted ? 0.9 : 0.5
  const w = hotKind ? 2.6 : boosted ? 2.2 : 1.6
  const mid = bezPoint(geo, 0.5)
  const label = `${from} ⇋ ${to}`
  const lw = label.length * 6 + 22
  return (
    <g>
      <title>{`${from} ⇋ ${to} — linked both ways${hotKind ? (hotKind === 'fetch' ? ' · transcript fetch in flight' : ' · context transfer in flight') : ''}`}</title>
      <path d={geo.d} fill="none" stroke={stroke} strokeOpacity={lineOp} strokeWidth={w}
        strokeDasharray={ghost ? '5 6' : undefined} />
      {!reduced && !dimmed && (
        <path d={geo.d} fill="none" stroke={hotKind ? '#ffffff' : color} strokeOpacity={hotKind ? 0.9 : 0.5}
          strokeWidth={hotKind ? 2.8 : 2} strokeLinecap="round" strokeDasharray="3 11" className="pp-flow" />
      )}
      {/* arrowheads at BOTH ends — the link carries context in either direction */}
      <polygon points={arrowAt(geo.p1x, geo.p1y, bezAngle(geo, 0) + Math.PI, 11)} fill={stroke}
        fillOpacity={dimmed ? 0.1 : 0.9} />
      <polygon points={arrowAt(geo.p2x, geo.p2y, bezAngle(geo, 1), 11)} fill={stroke}
        fillOpacity={dimmed ? 0.1 : 0.9} />
      {/* who ⇋ whom, pinned to the line */}
      <g opacity={dimmed ? 0.06 : 1}>
        <rect x={mid[0] - lw / 2} y={mid[1] - 10} width={lw} height={20} rx={10}
          fill="rgba(2,10,22,0.94)" stroke={hotKind ? hotColor : boosted ? 'rgba(0,212,255,0.55)' : 'rgba(0,212,255,0.28)'} strokeWidth={1} />
        <text x={mid[0]} y={mid[1] + 3.5} textAnchor="middle"
          style={{ fontFamily: 'var(--font-m)', fontSize: 10, letterSpacing: '0.04em' }}>
          <tspan fill={color}>{from}</tspan>
          <tspan fill="#7fa3c0"> ⇋ </tspan>
          <tspan fill={toColor}>{to}</tspan>
        </text>
      </g>
      {/* bright pulse riding the line on live transfers (sender → receiver) */}
      {hotKind && !reduced && (
        <g>
          <circle r={4} fill="#ffffff">
            <animateMotion dur="1.05s" repeatCount="indefinite" path={geo.d} />
          </circle>
          <circle r={2.5} fill={hotColor} opacity={0.85}>
            <animateMotion dur="1.05s" begin="0.5s" repeatCount="indefinite" path={geo.d} />
          </circle>
        </g>
      )}
    </g>
  )
}

function EdgeLayer({ size, rects, connections, sessions, hot, reduced, nameOf, hoverSid }: {
  size: { w: number; h: number }; rects: Record<string, Rect>; connections: Conn[]
  sessions: Record<string, Session>; hot: Record<string, string>; reduced: boolean
  nameOf: (sid?: string) => string; hoverSid: string | null
}) {
  const pairSeen: Record<string, boolean> = {}
  const drawn: Record<string, boolean> = {}
  return (
    <svg width={size.w} height={size.h} aria-hidden="true"
      style={{ position: 'absolute', top: 0, left: 0, zIndex: 0, pointerEvents: 'none', overflow: 'visible' }}>
      {connections.map((c, i) => {
        const a = rects[c.from], b = rects[c.to]
        const sa = sessions[c.from], sb = sessions[c.to]
        if (!a || !b || !sa || !sb) return null
        // if the reverse edge also exists, bow the two apart so both read
        const revKey = `${c.to}>${c.from}`
        const hasRev = connections.some(x => x.from === c.to && x.to === c.from)
        const key = `${c.from}>${c.to}`
        pairSeen[key] = true
        drawn[key] = true
        const bow = hasRev ? (pairSeen[revKey] ? 30 : -30) : 0
        const related = !!hoverSid && (c.from === hoverSid || c.to === hoverSid)
        return <Edge key={`${key}-${i}`} geo={edgeGeometry(a, b, bow)} from={sa.name} to={sb.name}
          color={colorOf(sa)} toColor={colorOf(sb)} hotKind={hot[key]}
          dimmed={!!hoverSid && !related} boosted={related} reduced={reduced} />
      })}
      {/* live transfers on pairs with no drawn link (group-wide awareness,
          e.g. a transcript fetch across the cluster) — dashed ghost line */}
      {Object.keys(hot).map(key => {
        if (drawn[key]) return null
        const parts = key.split('>')
        const a = rects[parts[0]], b = rects[parts[1]]
        const sa = sessions[parts[0]], sb = sessions[parts[1]]
        if (!a || !b || !sa || !sb) return null
        const related = !!hoverSid && (parts[0] === hoverSid || parts[1] === hoverSid)
        return <Edge key={`ghost-${key}`} geo={edgeGeometry(a, b, -34)} from={sa.name} to={sb.name}
          color={colorOf(sa)} toColor={colorOf(sb)} hotKind={hot[key]}
          dimmed={!!hoverSid && !related} boosted={related} reduced={reduced} ghost />
      })}
    </svg>
  )
}

// ─── SPACE VIEW — free-floating stars in 3D, zero dependencies ───────────
// Hand-rolled perspective projection on <canvas>: sessions are glowing stars
// clustered by group, links are arcing energy streams whose particles flow in
// the send direction (that flow IS the arrow, plus a solid arrowhead near the
// receiver). Drag rotates — the wheel is never captured, so the page still
// scrolls. The canvas is focusable: ←/→ pick a star, Enter opens/links it.
interface SpacePos { x: number; y: number; z: number }

function rrPath(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  ctx.beginPath()
  ctx.moveTo(x + r, y)
  ctx.arcTo(x + w, y, x + w, y + h, r)
  ctx.arcTo(x + w, y + h, x, y + h, r)
  ctx.arcTo(x, y + h, x, y, r)
  ctx.arcTo(x, y, x + w, y, r)
  ctx.closePath()
}

function SpaceView({ sessions, ordered, connections, hot, basePos, groupIndexOf, connectFrom, reduced, nameOf, onActivate }: {
  sessions: Record<string, Session>; ordered: Session[]; connections: Conn[]
  hot: Record<string, string>; basePos: Record<string, SpacePos>
  groupIndexOf: Record<string, number>; connectFrom: string | null; reduced: boolean
  nameOf: (sid?: string) => string; onActivate: (sid: string) => void
}) {
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const [selName, setSelName] = useState('')
  const P = useRef({ sessions, ordered, connections, hot, basePos, groupIndexOf, connectFrom, reduced, nameOf, onActivate })
  P.current = { sessions, ordered, connections, hot, basePos, groupIndexOf, connectFrom, reduced, nameOf, onActivate }
  const V = useRef({ t: Math.random() * 40, yaw: -0.6, pitch: -0.17, dragging: false,
    lastX: 0, lastY: 0, downX: 0, downY: 0, hover: '', sel: -1, dirty: true })
  V.current.dirty = true // any data/prop change repaints even in reduced-motion mode
  const hits = useRef<{ sid: string; sx: number; sy: number; r: number }[]>([])
  const stars = useRef<{ x: number; y: number; p: number; r: number }[]>([])

  useEffect(() => {
    let raf = 0
    const draw = () => {
      const c = canvasRef.current, wrap = wrapRef.current
      if (!c || !wrap) return
      const dpr = Math.min(window.devicePixelRatio || 1, 2)
      const W = wrap.clientWidth, H = wrap.clientHeight
      if (!W || !H) return
      if (c.width !== Math.round(W * dpr) || c.height !== Math.round(H * dpr)) {
        c.width = Math.round(W * dpr); c.height = Math.round(H * dpr)
      }
      const ctx = c.getContext('2d')
      if (!ctx) return
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      ctx.clearRect(0, 0, W, H)
      const pp = P.current
      const st = V.current
      const t = st.t
      const cx = W / 2, cy = H / 2
      const D = 640, f = Math.min(W * 0.9, H) * 0.92
      const cosY = Math.cos(st.yaw), sinY = Math.sin(st.yaw)
      const cosX = Math.cos(st.pitch), sinX = Math.sin(st.pitch)

      // backdrop starfield with slight yaw parallax
      if (!stars.current.length) {
        for (let i = 0; i < 150; i++) stars.current.push({ x: Math.random(), y: Math.random(), p: Math.random() * 6.28, r: 0.5 + Math.random() * 1.1 })
      }
      stars.current.forEach(s => {
        ctx.globalAlpha = pp.reduced ? 0.3 : 0.14 + 0.24 * (0.5 + 0.5 * Math.sin(t * 1.1 + s.p))
        ctx.fillStyle = '#9fd8ff'
        const px = (((s.x + st.yaw * 0.012 * s.r) % 1) + 1) % 1
        ctx.beginPath(); ctx.arc(px * W, s.y * H, s.r, 0, 6.29); ctx.fill()
      })
      ctx.globalAlpha = 1

      // project nodes
      const proj: Record<string, { sx: number; sy: number; s: number; z: number }> = {}
      pp.ordered.forEach((s, i) => {
        const b = pp.basePos[s.id]; if (!b) return
        const dr = pp.reduced ? 0 : 1
        const x = b.x + dr * 8 * Math.sin(t * 0.5 + i * 1.7)
        const y = b.y + dr * 10 * Math.sin(t * 0.37 + i * 2.3)
        const z = b.z + dr * 8 * Math.cos(t * 0.43 + i * 1.1)
        const x1 = x * cosY - z * sinY, z1 = x * sinY + z * cosY
        const y2 = y * cosX - z1 * sinX, z2 = y * sinX + z1 * cosX
        const sc = f / (D + z2)
        proj[s.id] = { sx: cx + x1 * sc, sy: cy + y2 * sc, s: sc, z: z2 }
      })

      const isRelated = (sid: string) => !!st.hover && (sid === st.hover ||
        pp.connections.some(cn => (cn.from === st.hover && cn.to === sid) || (cn.to === st.hover && cn.from === sid)))

      // edges (far behind nodes)
      const drawEdge = (fromSid: string, toSid: string, kind: string | undefined, ghost: boolean) => {
        const A = proj[fromSid], B = proj[toSid]
        const sa = pp.sessions[fromSid], sb = pp.sessions[toSid]
        if (!A || !B || !sa || !sb) return
        const related = !!st.hover && (fromSid === st.hover || toSid === st.hover)
        const dim = !!st.hover && !related
        const dx = B.sx - A.sx, dy = B.sy - A.sy
        const len = Math.hypot(dx, dy) || 1
        const mx = (A.sx + B.sx) / 2 - (dy / len) * len * 0.15
        const my = (A.sy + B.sy) / 2 + (dx / len) * len * 0.15
        const depth = (A.s + B.s) / 2
        const hotColor = kind === 'fetch' ? '#c084ff' : '#8fe9ff'
        const grad = ctx.createLinearGradient(A.sx, A.sy, B.sx, B.sy)
        grad.addColorStop(0, colorOf(sa)); grad.addColorStop(1, colorOf(sb))
        ctx.strokeStyle = kind ? hotColor : grad
        ctx.lineWidth = (kind ? 2.4 : related ? 2.1 : 1.3) * Math.max(0.6, depth)
        ctx.globalAlpha = dim ? 0.05 : kind ? 0.9 : Math.max(0.2, Math.min(0.7, depth * 0.8))
        if (ghost) ctx.setLineDash([5, 6])
        ctx.beginPath(); ctx.moveTo(A.sx, A.sy); ctx.quadraticCurveTo(mx, my, B.sx, B.sy); ctx.stroke()
        ctx.setLineDash([])
        const Q = (tt: number) => {
          const u = 1 - tt
          return { x: u * u * A.sx + 2 * u * tt * mx + tt * tt * B.sx, y: u * u * A.sy + 2 * u * tt * my + tt * tt * B.sy }
        }
        // energy particles flowing sender → receiver
        const nP = kind ? 4 : 2
        for (let j = 0; j < nP; j++) {
          const tt = pp.reduced ? (j + 0.5) / nP
            : ((t * (kind ? 0.55 : 0.15)) + j / nP + ((fromSid.charCodeAt(0) || 1) % 7) / 7) % 1
          const p = Q(tt)
          ctx.globalAlpha = dim ? 0.04 : 0.95
          ctx.fillStyle = kind ? '#ffffff' : colorOf(sa)
          ctx.shadowColor = kind ? hotColor : colorOf(sa)
          ctx.shadowBlur = 9
          ctx.beginPath(); ctx.arc(p.x, p.y, (kind ? 2.7 : 1.9) * Math.max(0.7, depth), 0, 6.29); ctx.fill()
          ctx.shadowBlur = 0
        }
        // arrowhead near the receiver
        const p1 = Q(0.88), p2 = Q(0.95)
        const ang = Math.atan2(p2.y - p1.y, p2.x - p1.x)
        const L = 9 * Math.max(0.7, depth)
        ctx.globalAlpha = dim ? 0.06 : 0.95
        ctx.fillStyle = kind ? hotColor : colorOf(sa)
        ctx.beginPath()
        ctx.moveTo(p2.x, p2.y)
        ctx.lineTo(p2.x - L * Math.cos(ang - 0.5), p2.y - L * Math.sin(ang - 0.5))
        ctx.lineTo(p2.x - L * Math.cos(ang + 0.5), p2.y - L * Math.sin(ang + 0.5))
        ctx.fill()
        ctx.globalAlpha = 1
      }
      const drawnKeys: Record<string, boolean> = {}
      pp.connections.forEach(cn => {
        const key = `${cn.from}>${cn.to}`
        drawnKeys[key] = true
        drawEdge(cn.from, cn.to, pp.hot[key], false)
      })
      Object.keys(pp.hot).forEach(key => {
        if (drawnKeys[key]) return
        const pr = key.split('>')
        drawEdge(pr[0], pr[1], pp.hot[key], true)
      })

      // nodes, painter-sorted far → near
      hits.current = []
      const order = pp.ordered.slice().sort((a, b) => ((proj[b.id] && proj[b.id].z) || 0) - ((proj[a.id] && proj[a.id].z) || 0))
      order.forEach(s => {
        const p = proj[s.id]; if (!p) return
        const ac = colorOf(s)
        const stt = statusOf(s.status)
        const gi = pp.groupIndexOf[s.id]
        const working = s.status === 'working'
        const pulse = working && !pp.reduced ? 1 + 0.14 * Math.sin(t * 3 + s.seat) : 1
        const R = (working ? 15 : 12) * p.s * pulse
        const dim = !!st.hover && st.hover !== s.id && !isRelated(s.id)
        const selected = st.sel >= 0 && pp.ordered[st.sel] && pp.ordered[st.sel].id === s.id

        ctx.globalAlpha = dim ? 0.22 : 1
        const g = ctx.createRadialGradient(p.sx, p.sy, 0, p.sx, p.sy, R * 3)
        g.addColorStop(0, 'rgba(255,255,255,0.95)')
        g.addColorStop(0.28, ac + 'd9')
        g.addColorStop(1, ac + '00')
        ctx.fillStyle = g
        ctx.beginPath(); ctx.arc(p.sx, p.sy, R * 3, 0, 6.29); ctx.fill()
        ctx.fillStyle = '#ffffff'
        ctx.beginPath(); ctx.arc(p.sx, p.sy, Math.max(2, R * 0.42), 0, 6.29); ctx.fill()

        if (gi !== undefined) {
          ctx.strokeStyle = GROUP_ACCENTS[gi % GROUP_ACCENTS.length]
          ctx.lineWidth = 1.4
          ctx.globalAlpha = dim ? 0.12 : 0.8
          ctx.beginPath(); ctx.arc(p.sx, p.sy, R * 1.7, 0, 6.29); ctx.stroke()
        }
        if (s.status === 'error') {
          ctx.strokeStyle = '#ff5566'
          ctx.lineWidth = 1.6
          ctx.globalAlpha = dim ? 0.15 : 0.9
          ctx.setLineDash([4, 4])
          ctx.beginPath(); ctx.arc(p.sx, p.sy, R * 2.15, 0, 6.29); ctx.stroke()
          ctx.setLineDash([])
        }
        if (pp.connectFrom === s.id) {
          ctx.strokeStyle = '#ff9500'
          ctx.lineWidth = 2
          ctx.globalAlpha = 0.5 + (pp.reduced ? 0.4 : 0.45 * (0.5 + 0.5 * Math.sin(t * 4)))
          ctx.beginPath(); ctx.arc(p.sx, p.sy, R * 2.5, 0, 6.29); ctx.stroke()
        }
        if (st.hover === s.id || selected) {
          ctx.strokeStyle = '#e4f3ff'
          ctx.lineWidth = 1.3
          ctx.globalAlpha = 0.9
          ctx.setLineDash([3, 4])
          ctx.beginPath(); ctx.arc(p.sx, p.sy, R * 2.3, 0, 6.29); ctx.stroke()
          ctx.setLineDash([])
        }

        // label pill: NAME · STATUS (always upright, always readable)
        const name = s.name.toUpperCase()
        ctx.font = '800 12px Orbitron, monospace'
        const nw = ctx.measureText(name).width
        ctx.font = '10px "Share Tech Mono", monospace'
        const sw = ctx.measureText(stt.label).width
        const gap = 8
        const totalW = nw + gap + sw + 20
        const ly = p.sy + Math.max(R * 2.4, 20) + 4
        ctx.globalAlpha = dim ? 0.18 : 0.92
        ctx.fillStyle = 'rgba(2,10,22,0.9)'
        rrPath(ctx, p.sx - totalW / 2, ly, totalW, 21, 10); ctx.fill()
        ctx.strokeStyle = ac
        ctx.globalAlpha = dim ? 0.08 : 0.4
        ctx.lineWidth = 1
        rrPath(ctx, p.sx - totalW / 2, ly, totalW, 21, 10); ctx.stroke()
        ctx.globalAlpha = dim ? 0.25 : 1
        ctx.textBaseline = 'middle'
        ctx.textAlign = 'left'
        ctx.font = '800 12px Orbitron, monospace'
        ctx.fillStyle = '#e4f3ff'
        ctx.fillText(name, p.sx - totalW / 2 + 10, ly + 11.5)
        ctx.font = '10px "Share Tech Mono", monospace'
        ctx.fillStyle = stt.color
        ctx.fillText(stt.label, p.sx - totalW / 2 + 10 + nw + gap, ly + 11.5)

        // live activity line while working
        if (working && !dim) {
          const last = s.feed && s.feed.length ? s.feed[s.feed.length - 1] : undefined
          let txt = s.current || 'working…'
          if (last) { const fv = feedView(last, pp.nameOf); txt = `${fv.icon} ${fv.text}` }
          if (txt.length > 38) txt = txt.slice(0, 37) + '…'
          ctx.font = '10px "Share Tech Mono", monospace'
          const tw = ctx.measureText(txt).width + 16
          ctx.globalAlpha = 0.88
          ctx.fillStyle = 'rgba(0,28,48,0.9)'
          rrPath(ctx, p.sx - tw / 2, ly + 25, tw, 17, 8); ctx.fill()
          ctx.fillStyle = '#7fe3ff'
          ctx.fillText(txt, p.sx - tw / 2 + 8, ly + 33.5)
        }
        ctx.globalAlpha = 1
        hits.current.unshift({ sid: s.id, sx: p.sx, sy: p.sy, r: Math.max(R * 2.3, 18) })
      })
    }

    const loop = () => {
      const st = V.current
      if (!P.current.reduced) {
        st.t += 0.016
        if (!st.dragging) st.yaw += 0.0014
        draw()
      } else if (st.dirty) {
        draw()
      }
      st.dirty = false
      raf = requestAnimationFrame(loop)
    }
    raf = requestAnimationFrame(loop)
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(() => { V.current.dirty = true }).catch(() => { /* noop */ })
    }
    return () => cancelAnimationFrame(raf)
  }, [])

  const hitAt = (clientX: number, clientY: number) => {
    const c = canvasRef.current
    if (!c) return undefined
    const rect = c.getBoundingClientRect()
    const x = clientX - rect.left, y = clientY - rect.top
    return hits.current.find(h => Math.hypot(h.sx - x, h.sy - y) <= h.r)
  }
  const onPointerDown = (e: ReactPointerEvent<HTMLCanvasElement>) => {
    const c = canvasRef.current
    if (c) { try { c.setPointerCapture(e.pointerId) } catch { /* noop */ } }
    const st = V.current
    st.dragging = true
    st.lastX = e.clientX; st.lastY = e.clientY
    st.downX = e.clientX; st.downY = e.clientY
  }
  const onPointerMove = (e: ReactPointerEvent<HTMLCanvasElement>) => {
    const st = V.current
    if (st.dragging) {
      st.yaw += (e.clientX - st.lastX) * 0.006
      st.pitch = Math.max(-0.9, Math.min(0.9, st.pitch + (e.clientY - st.lastY) * 0.004))
      st.lastX = e.clientX; st.lastY = e.clientY
      st.dirty = true
    } else {
      const found = hitAt(e.clientX, e.clientY)
      st.hover = found ? found.sid : ''
      const c = canvasRef.current
      if (c) c.style.cursor = found ? 'pointer' : 'grab'
      st.dirty = true
    }
  }
  const onPointerUp = (e: ReactPointerEvent<HTMLCanvasElement>) => {
    const st = V.current
    st.dragging = false
    if (Math.hypot(e.clientX - st.downX, e.clientY - st.downY) < 6) {
      const found = hitAt(e.clientX, e.clientY)
      if (found) P.current.onActivate(found.sid)
    }
  }
  const onKeyDown = (e: ReactKeyboardEvent<HTMLCanvasElement>) => {
    const st = V.current
    const list = P.current.ordered
    if (!list.length) return
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') { st.sel = (st.sel + 1) % list.length; e.preventDefault() }
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') { st.sel = (st.sel - 1 + list.length) % list.length; e.preventDefault() }
    else if ((e.key === 'Enter' || e.key === ' ') && st.sel >= 0 && list[st.sel]) {
      e.preventDefault()
      P.current.onActivate(list[st.sel].id)
      return
    } else return
    const s = list[st.sel]
    if (s) setSelName(`${s.name} — ${statusOf(s.status).label}. Press Enter to ${P.current.connectFrom ? 'link' : 'open'}.`)
    st.dirty = true
  }

  return (
    <div ref={wrapRef} style={{ position: 'relative', zIndex: 1, height: 'clamp(460px, calc(100vh - 330px), 760px)',
      borderRadius: 10, overflow: 'hidden' }}>
      <canvas ref={canvasRef} tabIndex={0} role="application"
        aria-label={`3D space view: ${ordered.length} sessions, ${connections.length} links. Drag to rotate. Arrow keys pick a session, Enter ${connectFrom ? 'links' : 'opens'} it.`}
        onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp}
        onPointerLeave={() => { V.current.hover = ''; V.current.dirty = true }} onKeyDown={onKeyDown}
        style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', cursor: 'grab',
          touchAction: 'pan-y', outlineOffset: -2 }} />
      <div aria-live="polite" className="visually-hidden">{selName}</div>
    </div>
  )
}

// ─── PROJECT MEMORY — one shared ledger PER connected cluster ─────────────
function LedgerRow({ e, sessions, nameOf }: {
  e: LedgerEntry; sessions: Record<string, Session>; nameOf: (sid?: string) => string
}) {
  const author = sessions[e.author]
  const ac = author ? colorOf(author) : 'var(--text-lo)'
  return (
    <div className="pp-row-in"
      style={{ display: 'flex', gap: 9, alignItems: 'baseline', padding: '4.5px 0', minWidth: 0,
        borderBottom: '1px solid rgba(0,212,255,0.05)' }}>
      <span style={{ ...mono, fontSize: 9.5, color: 'var(--text-lo)', flexShrink: 0, width: 56 }}>{fmtT(e.t)}</span>
      <span style={{ fontFamily: 'var(--font-b)', fontSize: 12, lineHeight: 1.5, color: 'var(--text)', minWidth: 0 }}>
        <b style={{ ...mono, fontSize: 10.5, fontWeight: 700, color: ac, letterSpacing: '0.06em' }}>
          {e.author_name || nameOf(e.author)}
        </b>
        <span style={{ color: 'var(--text-lo)' }}> · </span>
        {e.line}
      </span>
    </div>
  )
}

// One cluster's ledger: its own panel, accent, scroll and auto-follow.
function GroupMemoryPanel({ label, accent, members, entries, sessions, nameOf, emptyNote }: {
  label: string; accent: string; members: string[]; entries: LedgerEntry[]
  sessions: Record<string, Session>; nameOf: (sid?: string) => string; emptyNote: string
}) {
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const [following, setFollowing] = useState(true)
  useEffect(() => {
    const el = scrollRef.current
    if (el && following) el.scrollTop = el.scrollHeight
  }, [entries.length, following])
  const onScroll = () => {
    const el = scrollRef.current
    if (!el) return
    setFollowing(el.scrollHeight - el.scrollTop - el.clientHeight < 48)
  }
  return (
    <div style={{ position: 'relative', flexShrink: 0, borderRadius: 10, overflow: 'hidden',
      border: `1px solid ${accent}30`, borderTop: `2px solid ${accent}`,
      background: 'rgba(3,13,30,0.88)' }}>
      <header style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap',
        padding: '8px 12px', borderBottom: '1px solid var(--border)',
        background: `linear-gradient(90deg, ${accent}12, transparent 65%)` }}>
        <span style={{ ...mono, fontSize: 9.5, fontWeight: 700, letterSpacing: '0.2em', color: accent }}>
          ◈ {label}
        </span>
        {members.map(sid => (
          <span key={sid} style={{ ...mono, fontSize: 9.5, fontWeight: 700, color: colorOf(sessions[sid]),
            border: `1px solid ${colorOf(sessions[sid])}44`, borderRadius: 4, padding: '1px 6px' }}>
            {nameOf(sid)}
          </span>
        ))}
        <span style={{ flex: 1 }} />
        <span style={{ ...mono, fontSize: 9, color: 'var(--text-lo)' }}>{entries.length}</span>
      </header>
      <div ref={scrollRef} onScroll={onScroll} aria-label={`${label} shared ledger`}
        style={{ maxHeight: 250, overflowY: 'auto', padding: '2px 12px 10px', overscrollBehavior: 'contain' }}>
        {entries.length === 0
          ? <div style={{ ...mono, fontSize: 10.5, color: 'var(--text-lo)', padding: '10px 0' }}>{emptyNote}</div>
          : entries.map((e, i) => <LedgerRow key={`${e.t}-${i}`} e={e} sessions={sessions} nameOf={nameOf} />)}
      </div>
      {!following && entries.length > 0 && (
        <button type="button" onClick={() => setFollowing(true)} aria-label={`jump to the latest ${label} entry`}
          style={{ ...mono, position: 'absolute', bottom: 8, left: '50%', marginLeft: -52, width: 104,
            fontSize: 9.5, fontWeight: 700, letterSpacing: '0.1em', color: '#02121c', background: accent,
            border: 'none', borderRadius: 20, padding: '5px 0', cursor: 'pointer' }}>
          ↓ LATEST
        </button>
      )}
    </div>
  )
}

function MemoryRail({ ledger, groups, sessions, nameOf }: {
  ledger: LedgerEntry[]; groups: string[][]; sessions: Record<string, Session>
  nameOf: (sid?: string) => string
}) {
  // bucket entries: by the connected cluster containing the author, else "earlier"
  const buckets = useMemo(() => {
    const idx = (e: LedgerEntry) => {
      for (let g = 0; g < groups.length; g++) {
        if (groups[g].indexOf(e.author) >= 0) return g
        if (e.audience && e.audience.some(sid => groups[g].indexOf(sid) >= 0)) return g
      }
      return -1
    }
    const map: Record<string, LedgerEntry[]> = {}
    ledger.forEach(e => { const k = String(idx(e)); (map[k] = map[k] || []).push(e) })
    return map
  }, [ledger, groups])

  return (
    <aside className="pp-mem" aria-label="Project memory — one shared ledger per connected group"
      style={{ position: 'sticky', top: 74, display: 'flex', flexDirection: 'column', gap: 12,
        maxHeight: 'calc(100vh - 128px)', overflowY: 'auto', overscrollBehavior: 'contain' }}>
      <header style={{ flexShrink: 0, display: 'flex', alignItems: 'center', gap: 9, padding: '11px 14px',
        borderRadius: 10, border: '1px solid var(--border-hi)', background: 'var(--bg-panel)',
        boxShadow: 'inset 0 1px 0 rgba(0,212,255,0.12)' }}>
        <span aria-hidden="true" style={{ color: 'var(--cyan)', fontSize: 13 }}>▤</span>
        <span style={{ ...disp, fontSize: 12, fontWeight: 800, letterSpacing: '0.2em', color: 'var(--cyan)',
          textShadow: 'var(--glow-sm)' }}>PROJECT MEMORY</span>
        <span style={{ flex: 1 }} />
        <span style={{ ...mono, fontSize: 9.5, color: 'var(--text-lo)' }}>
          {groups.length} GROUP{groups.length === 1 ? '' : 'S'} · {ledger.length} ENTRIES · LIVE
        </span>
      </header>

      {groups.length === 0 && (
        <div style={{ borderRadius: 10, border: '1px solid var(--border)', background: 'rgba(3,13,30,0.85)',
          padding: '18px 16px', fontFamily: 'var(--font-b)', fontSize: 12.5, lineHeight: 1.65,
          color: 'var(--text-lo)' }}>
          No groups yet. Link two sessions with <b style={{ color: 'var(--amber)' }}>⚡ LINK</b> — each
          connected cluster gets its <b style={{ color: 'var(--text-hi)' }}>own shared ledger</b> here:
          one line per turn, one line per hand-off.
        </div>
      )}

      {groups.map((members, g) => (
        <GroupMemoryPanel key={members.join('|')} label={groupLabel(g)}
          accent={GROUP_ACCENTS[g % GROUP_ACCENTS.length]} members={members}
          entries={buckets[String(g)] || []} sessions={sessions} nameOf={nameOf}
          emptyNote="linked — waiting for the first recorded turn…" />
      ))}

      {(buckets['-1'] || []).length > 0 && (
        <GroupMemoryPanel label="EARLIER · UNGROUPED" accent="#5d80a0" members={[]}
          entries={buckets['-1'] || []} sessions={sessions} nameOf={nameOf} emptyNote="" />
      )}
    </aside>
  )
}

// ─── OUTPUT rail — the deliverables the board has published ────────────────
function DeliverableCard({ d, accent }: { d: Deliverable; accent: string }) {
  const [open, setOpen] = useState(false)
  const [copied, setCopied] = useState(false)
  const download = () => {
    const blob = new Blob([`# ${d.title}\n\n${d.content}\n`], { type: 'text/markdown' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${d.title.replace(/[^\w\d-]+/g, '-').toLowerCase().slice(0, 60) || 'deliverable'}.md`
    a.click()
    URL.revokeObjectURL(url)
  }
  const copy = () => {
    navigator.clipboard?.writeText(d.content).then(
      () => { setCopied(true); window.setTimeout(() => setCopied(false), 1600) },
      () => { /* clipboard denied — download still works */ })
  }
  return (
    <div style={{ flexShrink: 0, borderRadius: 10, overflow: 'hidden',
      border: `1px solid ${accent}30`, borderTop: `2px solid ${accent}`,
      background: 'rgba(3,13,30,0.88)' }}>
      <button type="button" onClick={() => setOpen(o => !o)} aria-expanded={open}
        aria-label={`${open ? 'collapse' : 'expand'} deliverable: ${d.title}`}
        style={{ display: 'flex', alignItems: 'center', gap: 8, width: '100%', textAlign: 'left',
          padding: '10px 12px', background: 'transparent', border: 'none', cursor: 'pointer' }}>
        <span aria-hidden="true" style={{ color: accent, fontSize: 11 }}>{open ? '▾' : '▸'}</span>
        <span style={{ ...mono, fontSize: 11.5, fontWeight: 700, color: 'var(--text-hi)', flex: 1, minWidth: 0,
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {d.title}
        </span>
        <span style={{ ...mono, fontSize: 9, color: accent, flexShrink: 0 }}>{d.author_name}</span>
        <span style={{ ...mono, fontSize: 9, color: 'var(--text-lo)', flexShrink: 0 }}>{fmtT(d.t)}</span>
      </button>
      {open && (
        <div style={{ borderTop: '1px solid var(--border)' }}>
          {d.task ? (
            <div style={{ ...mono, fontSize: 9.5, color: 'var(--text-lo)', padding: '8px 12px 0' }}>
              task: “{d.task}”
            </div>
          ) : null}
          <div style={{ padding: '8px 12px 4px', maxHeight: 380, overflowY: 'auto',
            overscrollBehavior: 'contain', fontSize: 12.5 }}>
            <Markdown text={d.content} />
          </div>
          <div style={{ display: 'flex', gap: 6, padding: '8px 12px 10px' }}>
            <button type="button" onClick={download} aria-label={`download ${d.title} as markdown`}
              style={{ ...mono, fontSize: 9.5, fontWeight: 700, color: '#02121c', background: accent,
                border: 'none', borderRadius: 5, padding: '5px 12px', cursor: 'pointer' }}>
              ↓ .MD
            </button>
            <button type="button" onClick={copy} aria-label={`copy ${d.title} to clipboard`}
              style={{ ...mono, fontSize: 9.5, fontWeight: 700, color: copied ? '#00ff88' : 'var(--text)',
                background: 'transparent', border: '1px solid var(--border)', borderRadius: 5,
                padding: '5px 12px', cursor: 'pointer' }}>
              {copied ? '✓ COPIED' : 'COPY'}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

function OutputRail({ deliverables, sessions }: {
  deliverables: Deliverable[]; sessions: Record<string, Session>
}) {
  const newestFirst = useMemo(() => [...deliverables].reverse(), [deliverables])
  return (
    <aside className="pp-mem" aria-label="Deliverables the board has published"
      style={{ position: 'sticky', top: 74, display: 'flex', flexDirection: 'column', gap: 12,
        maxHeight: 'calc(100vh - 128px)', overflowY: 'auto', overscrollBehavior: 'contain' }}>
      <header style={{ flexShrink: 0, display: 'flex', alignItems: 'center', gap: 9, padding: '11px 14px',
        borderRadius: 10, border: '1px solid var(--border-hi)', background: 'var(--bg-panel)',
        boxShadow: 'inset 0 1px 0 rgba(0,212,255,0.12)' }}>
        <span aria-hidden="true" style={{ color: '#b07aff', fontSize: 13 }}>⬢</span>
        <span style={{ ...disp, fontSize: 12, fontWeight: 800, letterSpacing: '0.2em', color: '#b07aff' }}>
          OUTPUT
        </span>
        <span style={{ flex: 1 }} />
        <span style={{ ...mono, fontSize: 9.5, color: 'var(--text-lo)' }}>
          {deliverables.length} PUBLISHED
        </span>
      </header>
      {newestFirst.length === 0 ? (
        <div style={{ borderRadius: 10, border: '1px solid var(--border)', background: 'rgba(3,13,30,0.85)',
          padding: '18px 16px', fontFamily: 'var(--font-b)', fontSize: 12.5, lineHeight: 1.65,
          color: 'var(--text-lo)' }}>
          Nothing published yet. When a team finishes a <b style={{ color: '#b07aff' }}>⬢ CONSENSUS</b>{' '}
          task, its merger publishes the final answer here (agents call{' '}
          <b style={{ color: 'var(--text-hi)' }}>panel_deliver</b>) — expand a card to read it,
          download it as markdown, or copy it out.
        </div>
      ) : newestFirst.map(d => (
        <DeliverableCard key={d.id} d={d}
          accent={colorOf(Object.values(sessions).find(s => s.id === d.author))} />
      ))}
    </aside>
  )
}

// ─── Focused session view — full 2D chat, free scrolling ──────────────────
function FocusView({ s, sessions, connections, models, nameOf, reduced,
  onBack, onChat, onStop, onRemove, onModel, onDisconnect, onStartConnect, onPersona }: {
  s: Session; sessions: Record<string, Session>; connections: Conn[]; models: string[]
  nameOf: (sid?: string) => string; reduced: boolean
  onBack: () => void; onChat: (text: string) => void; onStop: () => void; onRemove: () => void
  onModel: (m: string) => void; onDisconnect: (to: string) => void; onStartConnect: () => void
  onPersona: (persona: string) => void
}) {
  const [draft, setDraft] = useState('')
  const [personaDraft, setPersonaDraft] = useState(s.persona || '')
  useEffect(() => { setPersonaDraft(s.persona || '') }, [s.id, s.persona])
  const logRef = useRef<HTMLDivElement | null>(null)
  const feedRef = useRef<HTMLDivElement | null>(null)
  const st = statusOf(s.status)
  const ac = colorOf(s)
  const chat = s.chat || []
  const feed = s.feed || []
  // Links are bidirectional — one merged peer list, whichever way the edge is stored.
  const linked = [...new Set(connections
    .filter(c => c.from === s.id || c.to === s.id)
    .map(c => (c.from === s.id ? c.to : c.from))
    .filter(sid => sessions[sid]))]

  useEffect(() => {
    const el = logRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [chat.length, s.status])
  useEffect(() => {
    const el = feedRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [feed.length])

  const send = () => {
    const text = draft.trim()
    if (!text) return
    onChat(text)
    setDraft('')
  }
  const lastFeed = feed.length ? feedView(feed[feed.length - 1], nameOf) : null

  // Portal to <body>: the page sits inside a framer-motion fade wrapper whose
  // transform creates a stacking context — without the portal the fixed nav
  // (z-index 90) paints OVER this dialog and cuts off its header.
  return createPortal(
    <div role="dialog" aria-modal="true" aria-label={`${s.name} session`} data-screen-label="Session Focus"
      className="pp-overlay"
      style={{ position: 'fixed', inset: 0, zIndex: 200, display: 'flex', alignItems: 'center',
        justifyContent: 'center', padding: '26px clamp(12px, 2.5vw, 40px)',
        background: 'rgba(1,5,14,0.78)', backdropFilter: 'blur(10px)', WebkitBackdropFilter: 'blur(10px)' }}>
      <div className="pp-panel-in"
        style={{ width: 'min(1560px, 100%)', height: '100%', maxHeight: 940, display: 'flex', flexDirection: 'column',
          minHeight: 0, borderRadius: 14, overflow: 'hidden', background: 'rgba(3,12,26,0.97)',
          border: '1px solid var(--border-hi)',
          boxShadow: `0 0 0 1px rgba(0,212,255,0.06), 0 30px 90px rgba(0,0,0,0.7), inset 0 1px 0 rgba(0,212,255,0.14)` }}>

        {/* ── header ── */}
        <header style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '12px 16px',
          borderBottom: '1px solid var(--border)', flexWrap: 'wrap',
          background: `linear-gradient(90deg, ${ac}14, transparent 55%)` }}>
          <HudButton onClick={onBack} ariaLabel="back to the workspace overview"
            style={{ fontSize: 11.5 }}>◀ WORKSPACE</HudButton>
          <span aria-hidden="true" style={{ width: 10, height: 10, borderRadius: '50%', background: ac,
            boxShadow: `0 0 10px ${ac}` }} />
          <span style={{ ...disp, fontSize: 19, fontWeight: 800, color: 'var(--text-hi)' }}>{s.name}</span>
          <StatusBadge status={s.status} reduced={reduced} />
          {s.ruflo_id ? (
            <span title={`RUFLO fabric: ${s.ruflo_id}`} style={{ ...mono, fontSize: 10, color: '#b07aff' }}>⬡ RUFLO</span>
          ) : null}
          <span style={{ flex: 1 }} />
          {s.status === 'working' && (
            <HudButton onClick={onStop} color="#ff8896" border="rgba(255,85,102,0.5)"
              ariaLabel={`stop ${s.name}'s current turn`}>■ STOP</HudButton>
          )}
          <label style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
            <span className="visually-hidden">model for {s.name}</span>
            <select value={s.model || ''} onChange={e => onModel(e.target.value)} aria-label={`model for ${s.name}`}
              style={{ ...mono, fontSize: 11, background: 'var(--bg-panel)', color: 'var(--cyan)',
                border: '1px solid var(--border)', borderRadius: 6, padding: '8px 9px', outline: 'none', maxWidth: 210 }}>
              <option value="">model: default</option>
              {models.map(m => <option key={m} value={m}>{m}</option>)}
            </select>
          </label>
          <HudButton onClick={onStartConnect} color="var(--amber)" border="rgba(255,149,0,0.4)"
            ariaLabel={`link ${s.name} to another session`}>⚡ LINK</HudButton>
          <ConfirmButton label="✕ REMOVE" ariaLabel={`remove session ${s.name}`} onConfirm={onRemove} />
        </header>

        {/* ── body: chat + side rail ── */}
        <div className="pp-focus-body" style={{ flex: 1 }}>
          {/* chat column */}
          <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0, minHeight: 0,
            borderRight: '1px solid var(--border)' }}>
            <div ref={logRef} style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '18px 22px',
              display: 'flex', flexDirection: 'column', gap: 12, overscrollBehavior: 'contain' }}>
              {chat.length === 0 && (
                <div style={{ margin: 'auto', textAlign: 'center', maxWidth: 380 }}>
                  <div aria-hidden="true" style={{ fontSize: 26, marginBottom: 10, color: ac }}>◈</div>
                  <div style={{ ...disp, fontSize: 13, fontWeight: 700, letterSpacing: '0.12em', color: 'var(--text-hi)' }}>
                    {s.name.toUpperCase()} IS LISTENING
                  </div>
                  <div style={{ fontFamily: 'var(--font-b)', fontSize: 12.5, color: 'var(--text)', marginTop: 8, lineHeight: 1.6 }}>
                    This is {s.name}'s own conversation — separate history, separate model.
                    Anything it learns can be handed to linked sessions.
                  </div>
                </div>
              )}
              {chat.map((c, i) => {
                const you = c.who === 'you'
                return (
                  <div key={i} className="pp-row-in" style={{ maxWidth: 'min(720px, 86%)',
                    alignSelf: you ? 'flex-end' : 'flex-start',
                    background: you ? 'rgba(255,159,10,0.07)' : 'rgba(0,212,255,0.05)',
                    border: `1px solid ${you ? 'rgba(255,159,10,0.3)' : 'var(--border)'}`,
                    borderLeft: you ? undefined : `3px solid ${ac}`,
                    borderRight: you ? '3px solid rgba(255,159,10,0.55)' : undefined,
                    borderRadius: 9, padding: '10px 14px' }}>
                    <div style={{ ...mono, fontSize: 9.5, marginBottom: 6, letterSpacing: '0.12em',
                      color: you ? 'var(--amber)' : ac }}>
                      {you ? 'YOU' : c.who.toUpperCase()} · {fmtT(c.t)}
                    </div>
                    <div style={{ fontSize: 13.5, color: 'var(--text-hi)', lineHeight: 1.55 }}>
                      <Markdown text={c.text} />
                    </div>
                  </div>
                )
              })}
              {s.status === 'working' && (
                <div className="pp-row-in" style={{ alignSelf: 'flex-start', display: 'flex', alignItems: 'center',
                  gap: 9, ...mono, fontSize: 11.5, color: 'var(--cyan)', padding: '8px 13px',
                  border: '1px dashed var(--border-hi)', borderRadius: 9, maxWidth: '86%' }}>
                  <span aria-hidden="true">{lastFeed ? lastFeed.icon : '✦'}</span>
                  <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}>
                    {lastFeed ? lastFeed.text : (s.current || 'working')}
                  </span>
                  <WorkingDots />
                </div>
              )}
            </div>
            {/* input */}
            <div style={{ display: 'flex', gap: 10, padding: '13px 16px', borderTop: '1px solid var(--border)' }}>
              <input value={draft} onChange={e => setDraft(e.target.value)} autoFocus
                onKeyDown={e => { if (e.key === 'Enter') send() }}
                placeholder={`Message ${s.name}…  (Enter to send)`} aria-label={`message ${s.name}`}
                style={{ flex: 1, fontSize: 14, background: 'rgba(0,212,255,0.04)', border: '1px solid var(--border)',
                  borderRadius: 8, padding: '12px 15px', color: 'var(--text-hi)', outline: 'none',
                  fontFamily: 'var(--font-b)' }} />
              <button type="button" onClick={send} disabled={!draft.trim()} aria-label={`send message to ${s.name}`}
                style={{ ...disp, fontSize: 11, fontWeight: 800, letterSpacing: '0.14em', cursor: draft.trim() ? 'pointer' : 'default',
                  background: draft.trim() ? 'var(--cyan)' : 'rgba(0,212,255,0.07)',
                  color: draft.trim() ? '#02121c' : 'var(--text-lo)', border: 'none', borderRadius: 8,
                  padding: '0 22px', boxShadow: draft.trim() ? 'var(--glow-sm)' : 'none' }}>
                SEND ➤
              </button>
            </div>
          </div>

          {/* side rail: wiring + live activity */}
          <div className="pp-focus-side" style={{ display: 'flex', flexDirection: 'column', minHeight: 0 }}>
            <div style={{ padding: '14px 16px', borderBottom: '1px solid var(--border)' }}>
              <div style={{ ...mono, fontSize: 9.5, letterSpacing: '0.24em', color: 'var(--text-lo)', marginBottom: 9 }}>
                ROLE
              </div>
              <select value="" aria-label="pick a preset role"
                onChange={e => { if (e.target.value) setPersonaDraft(e.target.value); e.target.value = '' }}
                style={{ ...mono, width: '100%', fontSize: 10.5, background: 'var(--bg-panel)',
                  color: 'var(--text)', border: '1px solid var(--border)', borderRadius: 6,
                  padding: '7px 9px', outline: 'none', marginBottom: 7 }}>
                <option value="">preset roles…</option>
                {PERSONA_PRESETS.map(p => <option key={p.label} value={p.text}>{p.label}</option>)}
              </select>
              <textarea value={personaDraft} onChange={e => setPersonaDraft(e.target.value)}
                rows={3} aria-label={`role for ${s.name} — injected into every turn`}
                placeholder="no role — write one, or pick a preset above"
                style={{ ...mono, width: '100%', fontSize: 11, lineHeight: 1.5, resize: 'vertical',
                  background: 'rgba(255,179,64,0.04)', border: '1px solid rgba(255,179,64,0.25)',
                  borderRadius: 6, padding: '8px 10px', color: 'var(--text-hi)', outline: 'none',
                  boxSizing: 'border-box' }} />
              {personaDraft.trim() !== (s.persona || '') && (
                <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
                  <button type="button" onClick={() => onPersona(personaDraft.trim())}
                    aria-label={`save ${s.name}'s role`}
                    style={{ ...mono, flex: 1, fontSize: 10, fontWeight: 700, color: '#02121c',
                      background: '#ffb340', border: 'none', borderRadius: 5, padding: '6px 0',
                      cursor: 'pointer' }}>
                    SAVE ROLE
                  </button>
                  <button type="button" onClick={() => setPersonaDraft(s.persona || '')}
                    aria-label="discard role edits"
                    style={{ ...mono, fontSize: 10, color: 'var(--text-lo)', background: 'transparent',
                      border: '1px solid var(--border)', borderRadius: 5, padding: '6px 10px',
                      cursor: 'pointer' }}>
                    ✕
                  </button>
                </div>
              )}
              <div style={{ ...mono, fontSize: 9.5, letterSpacing: '0.24em', color: 'var(--text-lo)',
                margin: '14px 0 9px' }}>
                LINKED WITH
              </div>
              {linked.length === 0 && (
                <div style={{ fontFamily: 'var(--font-b)', fontSize: 12, color: 'var(--text-lo)', lineHeight: 1.5 }}>
                  No links — ⚡ LINK wires {s.name} to a peer; context then flows both ways.
                </div>
              )}
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {linked.map(sid => (
                  <div key={sid} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span style={{ ...mono, fontSize: 11, fontWeight: 700, color: colorOf(sessions[sid]),
                      border: '1px solid rgba(0,212,255,0.2)', background: 'rgba(0,212,255,0.05)',
                      borderRadius: 5, padding: '4px 9px', flex: 1 }}>
                      ⇋ {nameOf(sid)}
                    </span>
                    <button type="button" onClick={() => onDisconnect(sid)}
                      aria-label={`unlink ${s.name} from ${nameOf(sid)}`} title="remove this link"
                      style={{ ...mono, fontSize: 10, color: 'var(--text-lo)', background: 'transparent',
                        border: '1px solid var(--border)', borderRadius: 5, padding: '4px 8px', cursor: 'pointer' }}>
                      ✕
                    </button>
                  </div>
                ))}
              </div>
            </div>
            <div style={{ ...mono, fontSize: 9.5, letterSpacing: '0.24em', color: 'var(--text-lo)',
              padding: '12px 16px 6px' }}>
              LIVE ACTIVITY
            </div>
            <div ref={feedRef} style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '0 16px 14px',
              overscrollBehavior: 'contain' }}>
              {feed.length === 0 && (
                <div style={{ fontFamily: 'var(--font-b)', fontSize: 12, color: 'var(--text-lo)' }}>quiet so far.</div>
              )}
              {feed.slice(-40).map((e, i) => {
                const fv = feedView(e, nameOf)
                return (
                  <div key={i} style={{ display: 'flex', gap: 8, alignItems: 'baseline', padding: '3.5px 0', minWidth: 0 }}>
                    <span aria-hidden="true" style={{ ...mono, fontSize: 10.5, color: fv.color, flexShrink: 0, width: 12 }}>{fv.icon}</span>
                    <span style={{ ...mono, fontSize: 10.5, color: fv.color, lineHeight: 1.5, minWidth: 0,
                      overflowWrap: 'anywhere' }}>{fv.text}</span>
                    <span style={{ ...mono, fontSize: 8.5, color: 'var(--text-lo)', marginLeft: 'auto', flexShrink: 0 }}>{fmtT(e.t)}</span>
                  </div>
                )
              })}
            </div>
          </div>
        </div>
      </div>
    </div>,
    document.body
  )
}

// ─── Toasts (backend reasons, network failures) ────────────────────────────
interface Toast { id: number; text: string }
function Toasts({ toasts, dismiss }: { toasts: Toast[]; dismiss: (id: number) => void }) {
  if (!toasts.length) return null
  return createPortal(
    <div style={{ position: 'fixed', right: 18, bottom: 18, zIndex: 400, display: 'flex',
      flexDirection: 'column', gap: 8, maxWidth: 420 }}>
      {toasts.map(t => (
        <div key={t.id} role="alert" className="pp-row-in"
          style={{ display: 'flex', alignItems: 'flex-start', gap: 10, padding: '11px 13px', borderRadius: 8,
            background: 'rgba(30,6,10,0.95)', border: '1px solid rgba(255,34,68,0.5)',
            boxShadow: '0 10px 30px rgba(0,0,0,0.6)' }}>
          <span aria-hidden="true" style={{ color: '#ff5566', fontSize: 13 }}>⚠</span>
          <span style={{ fontFamily: 'var(--font-b)', fontSize: 12.5, color: 'var(--text-hi)', lineHeight: 1.5, flex: 1 }}>
            {t.text}
          </span>
          <button type="button" onClick={() => dismiss(t.id)} aria-label="dismiss notification"
            style={{ background: 'none', border: 'none', color: 'var(--text-lo)', cursor: 'pointer',
              fontSize: 12, padding: 2 }}>✕</button>
        </div>
      ))}
    </div>,
    document.body
  )
}

// ─── Connected components over the (undirected) link graph ────────────────
function computeGroups(sessions: Record<string, Session>, connections: Conn[]): string[][] {
  const ids = Object.keys(sessions)
  const parent: Record<string, string> = {}
  ids.forEach(i => { parent[i] = i })
  const find = (x: string): string => {
    let r = x
    while (parent[r] !== r) r = parent[r]
    let c = x
    while (parent[c] !== r) { const n = parent[c]; parent[c] = r; c = n }
    return r
  }
  connections.forEach(c => {
    if (!(c.from in parent) || !(c.to in parent)) return
    const a = find(c.from), b = find(c.to)
    if (a !== b) parent[a] = b
  })
  const byRoot: Record<string, string[]> = {}
  ids.forEach(i => { const r = find(i); (byRoot[r] = byRoot[r] || []).push(i) })
  return Object.values(byRoot)
    .filter(g => g.length >= 2)
    .map(g => g.sort((a, b) => (sessions[a].seat || 0) - (sessions[b].seat || 0)))
    .sort((a, b) => b.length - a.length)
}

// ═══════════════════════════════════════════════════════════════════════════
// PAGE
// ═══════════════════════════════════════════════════════════════════════════
export default function PanelPage({ page, onNavigate }: { page?: Page; onNavigate?: (p: Page) => void }) {
  const [sessions, setSessions] = useState<Record<string, Session>>({})
  const [connections, setConnections] = useState<Conn[]>([])
  const [ledger, setLedger] = useState<LedgerEntry[]>([])
  const [models, setModels] = useState<string[]>([])
  const [rufloSwarm, setRufloSwarm] = useState('')
  const [wsStatus, setWsStatus] = useState<'connecting' | 'live' | 'down'>('connecting')
  const [hotEdges, setHotEdges] = useState<Record<string, string>>({})
  const [connectFrom, setConnectFrom] = useState<string | null>(null)
  const [focus, setFocus] = useState<string | null>(null)
  const [hoverSid, setHoverSid] = useState<string | null>(null)
  const [view, setView] = useState<'grid' | 'space'>(() => {
    try { return localStorage.getItem('cosmos.panel.view') === 'space' ? 'space' : 'grid' } catch { return 'grid' }
  })
  const switchView = (v: 'grid' | 'space') => {
    setView(v)
    try { localStorage.setItem('cosmos.panel.view', v) } catch { /* private mode */ }
  }
  const [toasts, setToasts] = useState<Toast[]>([])
  const [newName, setNewName] = useState('')
  const [newModel, setNewModel] = useState('')
  const [newPersona, setNewPersona] = useState('')
  const [mode, setModeState] = useState<'singular' | 'consensus'>('singular')
  const [groupDraft, setGroupDraft] = useState('')
  const [templates, setTemplates] = useState<Template[]>([])
  const [deliverables, setDeliverables] = useState<Deliverable[]>([])
  const [groupTask, setGroupTask] = useState<GroupTask | null>(null)
  const [railTab, setRailTab] = useState<'memory' | 'output'>('memory')
  const seenOutput = useRef(0)             // deliverable count when OUTPUT last opened
  const reduced = useReducedMotion()

  const wsRef = useRef<WebSocket | null>(null)
  const attemptsRef = useRef(0)
  const retryTimerRef = useRef(0)
  const disposedRef = useRef(false)
  const toastSeq = useRef(1)

  const nameOf = useCallback((sid?: string) => {
    if (!sid) return '?'
    const s = sessions[sid]
    return s ? s.name : sid.slice(0, 6)
  }, [sessions])

  const pushToast = useCallback((text: string) => {
    const id = toastSeq.current++
    setToasts(p => [...p.slice(-3), { id, text }])
    window.setTimeout(() => setToasts(p => p.filter(t => t.id !== id)), 7000)
  }, [])

  // ── incoming events (all handlers guard unknown session ids) ────────────
  const applyEvent = useCallback((m: any) => {
    if (!m || typeof m.type !== 'string') return
    switch (m.type) {
      case 'snapshot':
        setSessions(m.sessions || {})
        setConnections(m.connections || [])
        setLedger(Array.isArray(m.ledger) ? m.ledger.slice(-500) : [])
        setModels(m.models || [])
        setRufloSwarm(m.ruflo_swarm || '')
        setModeState(m.mode === 'consensus' ? 'consensus' : 'singular')
        setTemplates(Array.isArray(m.templates) ? m.templates : [])
        setDeliverables(Array.isArray(m.deliverables) ? m.deliverables : [])
        setGroupTask(m.group_task || null)
        return
      case 'panel_mode':
        setModeState(m.mode === 'consensus' ? 'consensus' : 'singular')
        return
      case 'session_persona':
        setSessions(p => p[m.session_id]
          ? { ...p, [m.session_id]: { ...p[m.session_id], persona: m.persona } } : p)
        return
      case 'group_task':
        setGroupTask(m.task || null)
        return
      case 'group_progress':
        setGroupTask(p => {
          if (!p || p.id !== m.task_id || typeof m.team !== 'number') return p
          const teams = p.teams.map((t, i) => i !== m.team ? t : {
            ...t, done: typeof m.done === 'number' ? m.done : t.done,
            merged: m.merged === true ? true : t.merged,
            merge_started: m.merged === true ? true : t.merge_started,
          })
          return { ...p, teams }
        })
        return
      case 'group_merge':
        setGroupTask(p => {
          if (!p || p.id !== m.task_id || typeof m.team !== 'number') return p
          return { ...p, teams: p.teams.map((t, i) => i === m.team ? { ...t, merge_started: true } : t) }
        })
        return
      case 'group_done':
        // Leave the object; the strip hides itself once every team is merged.
        return
      case 'deliverable':
        if (!m.deliverable) return
        setDeliverables(p => [...p.filter(d => d.id !== m.deliverable.id), m.deliverable].slice(-40))
        return
      case 'session_add':
        if (!m.session || !m.session.id) return
        setSessions(p => ({ ...p, [m.session.id]: m.session }))
        return
      case 'session_remove':
        setSessions(p => { const n = { ...p }; delete n[m.session_id]; return n })
        setConnections(p => p.filter(c => c.from !== m.session_id && c.to !== m.session_id))
        setConnectFrom(f => (f === m.session_id ? null : f))
        setFocus(f => (f === m.session_id ? null : f))
        return
      case 'session_status':
        setSessions(p => p[m.session_id]
          ? { ...p, [m.session_id]: { ...p[m.session_id], status: m.status, current: m.status === 'working' ? p[m.session_id].current : '' } } : p)
        return
      case 'session_model':
        setSessions(p => p[m.session_id] ? { ...p, [m.session_id]: { ...p[m.session_id], model: m.model } } : p)
        return
      case 'session_chat':
        setSessions(p => {
          const s = p[m.session_id]; if (!s || !m.entry) return p
          return { ...p, [m.session_id]: { ...s, chat: [...(s.chat || []), m.entry].slice(-200) } }
        })
        return
      case 'session_event':
        setSessions(p => {
          const s = p[m.session_id]; if (!s || !m.event) return p
          const next: Session = { ...s, feed: [...(s.feed || []), m.event].slice(-120) }
          if (m.event && typeof m.event.text === 'string' && m.event.kind === 'thought' && s.status === 'working' && !s.current) {
            next.current = m.event.text
          }
          return { ...p, [m.session_id]: next }
        })
        return
      case 'connection_add':
        setConnections(p => p.some(c => c.from === m.from && c.to === m.to) ? p : [...p, { from: m.from, to: m.to }])
        return
      case 'connection_remove':
        // undirected: the stored edge may be the reverse of the removal request
        setConnections(p => p.filter(c =>
          !((c.from === m.from && c.to === m.to) || (c.from === m.to && c.to === m.from))))
        return
      case 'connections':
        setConnections(m.connections || [])
        return
      case 'edge': {
        const key = `${m.from}>${m.to}`
        const kind = m.kind === 'fetch' ? 'fetch' : 'peer'
        setHotEdges(p => ({ ...p, [key]: kind }))
        window.setTimeout(() => setHotEdges(p => { const n = { ...p }; delete n[key]; return n }), 2500)
        return
      }
      case 'ledger':
        if (!m.entry) return
        setLedger(p => [...p, m.entry].slice(-500))
        return
      case 'ruflo':
        if (m.swarm_id) setRufloSwarm(m.swarm_id)
        setSessions(p => p[m.session_id] ? { ...p, [m.session_id]: { ...p[m.session_id], ruflo_id: m.swarm_id || 'attached' } } : p)
        return
    }
  }, [])

  // ── websocket with reconnect ────────────────────────────────────────────
  const connectWs = useCallback(() => {
    if (disposedRef.current) return
    try { wsRef.current?.close() } catch { /* noop */ }
    setWsStatus(attemptsRef.current === 0 ? 'connecting' : 'down')
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${location.host}/ws/panel`)
    wsRef.current = ws
    ws.onopen = () => { attemptsRef.current = 0; setWsStatus('live') }
    ws.onmessage = ev => { try { applyEvent(JSON.parse(ev.data)) } catch (e) { console.warn('[panel-ws] malformed frame:', e) } }
    ws.onclose = () => {
      if (disposedRef.current || wsRef.current !== ws) return
      wsRef.current = null
      setWsStatus('down')
      attemptsRef.current += 1
      const delay = Math.min(900 * Math.pow(1.7, attemptsRef.current - 1), 8000)
      retryTimerRef.current = window.setTimeout(connectWs, delay)
    }
    ws.onerror = () => { try { ws.close() } catch { /* noop */ } }
  }, [applyEvent])

  useEffect(() => {
    disposedRef.current = false
    connectWs()
    // one REST snapshot too, so the page fills even if the socket is slow
    fetch('/api/panel').then(r => (r.ok ? r.json() : null))
      .then(j => { if (j) applyEvent({ ...j, type: 'snapshot' }) })
      .catch(e => console.warn('[panel] REST snapshot failed:', e))
    return () => {
      disposedRef.current = true
      window.clearTimeout(retryTimerRef.current)
      try { wsRef.current?.close() } catch { /* noop */ }
      wsRef.current = null
    }
  }, [connectWs, applyEvent])

  // ── REST helper: surfaces backend reason strings verbatim ───────────────
  const api = useCallback(async (path: string, body?: unknown, method = 'POST'): Promise<boolean> => {
    try {
      const res = await fetch(`/api/panel${path}`, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
      })
      if (!res.ok) {
        let msg = ''
        try { msg = await res.text() } catch { /* noop */ }
        try { const j = JSON.parse(msg); msg = j.detail || j.error || j.message || msg } catch { /* raw text is fine */ }
        pushToast(msg || `Request failed (${res.status})`)
        return false
      }
      return true
    } catch {
      pushToast('Backend unreachable — is the Cosmos server running?')
      return false
    }
  }, [pushToast])

  const addSession = async () => {
    const body: Record<string, string> = {}
    if (newName.trim()) body.name = newName.trim()
    if (newModel) body.model = newModel
    if (newPersona) body.persona = newPersona
    const ok = await api('/sessions', body)
    if (ok) { setNewName(''); setNewPersona('') }
  }
  const spawnSquad = async (tid: string) => {
    if (!tid) return
    const t = templates.find(x => x.id === tid)
    const ok = await api(`/templates/${tid}`)
    if (ok) pushToast(`${t ? t.label : 'Squad'} spawned — pre-wired, personas set, mode: consensus.`)
  }
  const savePersona = (sid: string, persona: string) => { api(`/sessions/${sid}/persona`, { persona }) }
  const sendChat = (sid: string, text: string) => { api(`/sessions/${sid}/chat`, { text }) }
  const removeSession = (sid: string) => { api(`/sessions/${sid}`, undefined, 'DELETE') }
  const setModel = (sid: string, model: string) => { api(`/sessions/${sid}/model`, { model }) }
  const stopTurn = (sid: string) => { api(`/sessions/${sid}/stop`) }
  const disconnect = (from: string, to: string) => { api('/connections/remove', { from, to }) }
  const makeLink = async (from: string, to: string) => {
    if (from === to) { pushToast('A session cannot link to itself.'); return }
    // links are bidirectional — a reverse edge is the same link
    if (connections.some(c => (c.from === from && c.to === to) || (c.from === to && c.to === from))) {
      pushToast(`${nameOf(from)} and ${nameOf(to)} are already linked.`)
      setConnectFrom(null)
      return
    }
    const ok = await api('/connections', { from, to })
    if (ok) setConnectFrom(null)
  }
  const setPanelMode = async (m: 'singular' | 'consensus') => {
    setModeState(m)                        // optimistic; snapshot/event confirms
    api('/mode', { mode: m })
  }
  const launchGroup = async () => {
    const text = groupDraft.trim()
    if (!text) return
    const ok = await api('/broadcast', { text })
    if (ok) { setGroupDraft(''); pushToast('Task sent to the whole board — teams are dividing the work.') }
  }

  // ── esc: cancel link mode first, then close focus ───────────────────────
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      setConnectFrom(f => {
        if (f) return null
        setFocus(null)
        return f
      })
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  // ── card position measurement for the edge layer ────────────────────────
  const boardRef = useRef<HTMLDivElement | null>(null)
  const cardEls = useRef<Map<string, HTMLDivElement>>(new Map())
  const [rects, setRects] = useState<Record<string, Rect>>({})
  const [boardSize, setBoardSize] = useState({ w: 0, h: 0 })

  const measure = useCallback(() => {
    const board = boardRef.current
    if (!board) return
    const next: Record<string, Rect> = {}
    cardEls.current.forEach((el, sid) => {
      if (!el || !el.isConnected) return
      next[sid] = { x: el.offsetLeft, y: el.offsetTop, w: el.offsetWidth, h: el.offsetHeight }
    })
    setBoardSize(p => (p.w === board.clientWidth && p.h === board.clientHeight)
      ? p : { w: board.clientWidth, h: board.clientHeight })
    setRects(prev => {
      const pk = Object.keys(prev), nk = Object.keys(next)
      if (pk.length === nk.length && nk.every(k => {
        const a = prev[k], b = next[k]
        return a && a.x === b.x && a.y === b.y && a.w === b.w && a.h === b.h
      })) return prev
      return next
    })
  }, [])

  const sessionKey = useMemo(() => Object.keys(sessions).sort().join(','), [sessions])
  useLayoutEffect(() => { measure() })
  useEffect(() => {
    const ro = new ResizeObserver(() => measure())
    if (boardRef.current) ro.observe(boardRef.current)
    cardEls.current.forEach(el => { if (el) ro.observe(el) })
    window.addEventListener('resize', measure)
    return () => { ro.disconnect(); window.removeEventListener('resize', measure) }
  }, [sessionKey, measure])

  // ── derived ──────────────────────────────────────────────────────────────
  const groups = useMemo(() => computeGroups(sessions, connections), [sessions, connections])
  const groupIndexOf = useMemo(() => {
    const m: Record<string, number> = {}
    groups.forEach((g, i) => g.forEach(sid => { m[sid] = i }))
    return m
  }, [groups])
  // cluster-mates sit next to each other — links stay short and readable
  const ordered = useMemo(() =>
    Object.values(sessions).sort((a, b) => {
      const ga = groupIndexOf[a.id] !== undefined ? groupIndexOf[a.id] : 99
      const gb = groupIndexOf[b.id] !== undefined ? groupIndexOf[b.id] : 99
      return ga !== gb ? ga - gb : (a.seat || 0) - (b.seat || 0)
    }), [sessions, groupIndexOf])
  // 3D positions for the space view — cluster-mates orbit a shared center
  const basePos = useMemo(() => {
    const map: Record<string, SpacePos> = {}
    groups.forEach((members, g) => {
      const ga = g * 2.399963
      const gr = groups.length > 1 ? 165 : 0
      const gx = Math.cos(ga) * gr, gz = Math.sin(ga) * gr
      const gy = groups.length > 1 ? (g % 2 ? 40 : -40) : 0
      const rr = 78 + members.length * 12
      members.forEach((sid, i) => {
        const a = (i / members.length) * Math.PI * 2 + g * 1.15
        map[sid] = { x: gx + Math.cos(a) * rr, y: gy + ((i % 3) - 1) * 34, z: gz + Math.sin(a) * rr }
      })
    })
    let k = 0
    ordered.forEach(s => {
      if (map[s.id]) return
      const a = k * 2.399963 + 0.8
      const r = 265 + (k % 2) * 34
      map[s.id] = { x: Math.cos(a) * r, y: ((k % 5) - 2) * 44, z: Math.sin(a) * r }
      k++
    })
    return map
  }, [groups, ordered])
  const n = ordered.length
  const focused = focus ? sessions[focus] : undefined
  const fromSession = connectFrom ? sessions[connectFrom] : undefined
  const workingN = ordered.filter(s => s.status === 'working').length
  const backendLost = wsStatus === 'down' && attemptsRef.current >= 2

  const wsChip = wsStatus === 'live'
    ? <span className="status-chip online" style={{ fontSize: 9.5 }}><span className="status-dot" />LIVE</span>
    : wsStatus === 'connecting'
      ? <span className="status-chip" style={{ fontSize: 9.5 }}><span className="status-dot" />CONNECTING</span>
      : <span className="status-chip reconnecting" style={{ fontSize: 9.5 }}><span className="status-dot" />RECONNECTING</span>

  // ═════════════════════════════════════════════════════════════════════════
  return (
    <PageShell title="Panel" page={page} onNavigate={onNavigate}
      subtitle="INDEPENDENT AGENT SESSIONS · BIDIRECTIONAL LINKS · SINGULAR / CONSENSUS PROMPTS"
      right={<div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>{wsChip}</div>}>

      <style>{PP_CSS}</style>

      {/* Full-bleed: escape PageShell's centered 1080px column and use the
          whole viewport width (margin trick — no transform, so the focus
          overlay's position:fixed still means the viewport). */}
      <div data-screen-label="Panel Workspace"
        style={{ width: 'calc(100vw - 36px)', marginLeft: 'calc(50% - 50vw + 18px)' }}>

        {/* ── command bar: counts · legend · add-session · link banner ── */}
        <div style={{ position: 'sticky', top: 64, zIndex: 30, marginBottom: 16, borderRadius: 10,
          border: `1px solid ${connectFrom ? 'rgba(255,149,0,0.55)' : 'var(--border)'}`,
          background: connectFrom ? 'rgba(26,15,2,0.92)' : 'rgba(2,10,22,0.88)',
          backdropFilter: 'blur(14px)', WebkitBackdropFilter: 'blur(14px)',
          boxShadow: connectFrom ? '0 0 26px rgba(255,149,0,0.18)' : '0 8px 30px rgba(0,0,0,0.45)' }}>
          {connectFrom ? (
            <div role="status" aria-live="assertive"
              style={{ display: 'flex', alignItems: 'center', gap: 14, padding: '12px 16px', flexWrap: 'wrap' }}>
              <span aria-hidden="true" style={{ fontSize: 16, color: 'var(--amber)' }}>⚡</span>
              <span style={{ ...mono, fontSize: 12.5, color: 'var(--amber)', letterSpacing: '0.06em' }}>
                LINKING&nbsp;
                <b style={{ color: 'var(--text-hi)' }}>{fromSession ? fromSession.name : '?'}</b>
                &nbsp;⇋&nbsp;<b style={{ color: 'var(--text-hi)' }}>click a target session</b>
                &nbsp;— linked agents share context BOTH ways
              </span>
              <span style={{ flex: 1 }} />
              <span style={{ ...mono, fontSize: 10, color: 'var(--text-lo)' }}>ESC also cancels</span>
              <HudButton onClick={() => setConnectFrom(null)} color="var(--amber)" border="rgba(255,149,0,0.5)"
                ariaLabel="cancel linking">CANCEL</HudButton>
            </div>
          ) : (
            <div style={{ display: 'flex', alignItems: 'center', gap: 14, padding: '10px 16px', flexWrap: 'wrap' }}>
              <span style={{ ...mono, fontSize: 11, color: 'var(--text)', letterSpacing: '0.1em' }}>
                <b style={{ color: 'var(--text-hi)' }}>{n}</b> SESSION{n === 1 ? '' : 'S'}
                <span style={{ color: 'var(--text-lo)' }}> · </span>
                <b style={{ color: 'var(--text-hi)' }}>{connections.length}</b> LINK{connections.length === 1 ? '' : 'S'}
                <span style={{ color: 'var(--text-lo)' }}> · </span>
                <b style={{ color: 'var(--text-hi)' }}>{groups.length}</b> GROUP{groups.length === 1 ? '' : 'S'}
                {workingN > 0 && <span style={{ color: 'var(--cyan)' }}> · {workingN} WORKING</span>}
              </span>
              {rufloSwarm ? (
                <span title={`RUFLO coordination fabric: ${rufloSwarm}`}
                  style={{ ...mono, fontSize: 9.5, color: '#b07aff', border: '1px solid rgba(153,68,255,0.4)',
                    background: 'rgba(153,68,255,0.07)', borderRadius: 4, padding: '3px 8px' }}>
                  ⬡ RUFLO · {rufloSwarm}
                </span>
              ) : null}
              <span aria-hidden="true" style={{ display: 'inline-flex', gap: 12, marginLeft: 4 }}>
                {Object.keys(STATUS).map(k => (
                  <span key={k} style={{ ...mono, fontSize: 9, color: 'var(--text-lo)', display: 'inline-flex',
                    alignItems: 'center', gap: 5 }}>
                    <span style={{ width: 6, height: 6, borderRadius: '50%', background: STATUS[k].color }} />
                    {STATUS[k].label}
                  </span>
                ))}
              </span>
              <div role="group" aria-label="workspace view mode"
                style={{ display: 'flex', marginLeft: 6, border: '1px solid var(--border-hi)', borderRadius: 6, overflow: 'hidden' }}>
                {(['grid', 'space'] as const).map(v => (
                  <button key={v} type="button" onClick={() => switchView(v)} aria-pressed={view === v}
                    aria-label={v === 'grid' ? 'grid view' : '3D space view'}
                    style={{ ...disp, fontSize: 9.5, fontWeight: 800, letterSpacing: '0.14em', padding: '8px 12px',
                      cursor: 'pointer', border: 'none',
                      background: view === v ? 'var(--cyan)' : 'transparent',
                      color: view === v ? '#02121c' : 'var(--cyan)',
                      boxShadow: view === v ? 'var(--glow-sm)' : 'none' }}>
                    {v === 'grid' ? '▦ GRID' : '✴ SPACE'}
                  </button>
                ))}
              </div>
              <div role="group" aria-label="prompt mode: singular gives each session its own task, consensus sends one task to the whole board"
                style={{ display: 'flex', border: '1px solid rgba(153,68,255,0.45)', borderRadius: 6, overflow: 'hidden' }}>
                {(['singular', 'consensus'] as const).map(m => (
                  <button key={m} type="button" onClick={() => setPanelMode(m)} aria-pressed={mode === m}
                    title={m === 'singular'
                      ? 'SINGULAR: chat with each session individually — its own task'
                      : 'CONSENSUS: one prompt to the whole board — linked teams split the work and merge findings'}
                    style={{ ...disp, fontSize: 9.5, fontWeight: 800, letterSpacing: '0.14em', padding: '8px 12px',
                      cursor: 'pointer', border: 'none',
                      background: mode === m ? '#b07aff' : 'transparent',
                      color: mode === m ? '#120224' : '#b07aff',
                      boxShadow: mode === m ? '0 0 14px rgba(153,68,255,0.45)' : 'none' }}>
                    {m === 'singular' ? '◎ SINGULAR' : '⬢ CONSENSUS'}
                  </button>
                ))}
              </div>
              <span style={{ flex: 1 }} />
              <select value="" onChange={e => { spawnSquad(e.target.value); e.target.value = '' }}
                aria-label="spawn a pre-wired squad template"
                title="one click → a pre-wired team: sessions + roles + links + consensus mode"
                style={{ ...mono, fontSize: 10.5, background: 'rgba(153,68,255,0.08)', color: '#b07aff',
                  border: '1px solid rgba(153,68,255,0.45)', borderRadius: 6, padding: '8px 9px',
                  outline: 'none', cursor: 'pointer', maxWidth: 170 }}>
                <option value="">⚡ spawn squad…</option>
                {templates.map(t => (
                  <option key={t.id} value={t.id} title={t.desc}>{t.label} ({t.size})</option>
                ))}
              </select>
              <input value={newName} onChange={e => setNewName(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') addSession() }}
                placeholder="session name (optional)" aria-label="new session name"
                style={{ ...mono, fontSize: 11, background: 'rgba(0,212,255,0.04)', border: '1px solid var(--border)',
                  borderRadius: 6, padding: '8px 11px', color: 'var(--text-hi)', outline: 'none', width: 150 }} />
              <select value={newPersona} onChange={e => setNewPersona(e.target.value)}
                aria-label="role for the new session"
                title="the role is injected into every one of this agent's turns"
                style={{ ...mono, fontSize: 10.5, background: 'var(--bg-panel)', color: 'var(--text)',
                  border: '1px solid var(--border)', borderRadius: 6, padding: '8px 9px', outline: 'none', maxWidth: 150 }}>
                <option value="">role: none</option>
                {PERSONA_PRESETS.map(p => <option key={p.label} value={p.text}>{p.label}</option>)}
              </select>
              <select value={newModel} onChange={e => setNewModel(e.target.value)} aria-label="model for the new session"
                style={{ ...mono, fontSize: 10.5, background: 'var(--bg-panel)', color: 'var(--cyan)',
                  border: '1px solid var(--border)', borderRadius: 6, padding: '8px 9px', outline: 'none', maxWidth: 160 }}>
                <option value="">model: default</option>
                {models.map(m => <option key={m} value={m}>{m}</option>)}
              </select>
              <button type="button" onClick={addSession} aria-label="add a new session"
                style={{ ...disp, fontSize: 11, fontWeight: 800, letterSpacing: '0.14em', color: '#02121c',
                  background: 'var(--cyan)', border: 'none', borderRadius: 6, padding: '9px 16px',
                  cursor: 'pointer', boxShadow: 'var(--glow-sm)' }}>
                ＋ ADD SESSION
              </button>
            </div>
          )}
          {/* consensus prompt: ONE task → the whole board; linked teams split it */}
          {!connectFrom && mode === 'consensus' && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 16px',
              borderTop: '1px solid rgba(153,68,255,0.3)', background: 'rgba(20,8,40,0.35)' }}>
              <span aria-hidden="true" style={{ fontSize: 14, color: '#b07aff' }}>⬢</span>
              <input value={groupDraft} onChange={e => setGroupDraft(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); launchGroup() } }}
                placeholder={n === 0 ? 'add sessions first — then one task for the whole board…'
                  : `one task for all ${n} session${n === 1 ? '' : 's'} — linked teams split the work, seat #1 merges…`}
                aria-label="consensus task for the whole board" disabled={n === 0}
                style={{ ...mono, flex: 1, fontSize: 12, background: 'rgba(153,68,255,0.06)',
                  border: '1px solid rgba(153,68,255,0.35)', borderRadius: 6, padding: '9px 12px',
                  color: 'var(--text-hi)', outline: 'none', opacity: n === 0 ? 0.5 : 1 }} />
              <button type="button" onClick={launchGroup} disabled={n === 0 || !groupDraft.trim()}
                aria-label={`send this task to all ${n} sessions`}
                style={{ ...disp, fontSize: 10.5, fontWeight: 800, letterSpacing: '0.14em',
                  color: n === 0 || !groupDraft.trim() ? '#b07aff' : '#120224',
                  background: n === 0 || !groupDraft.trim() ? 'transparent' : '#b07aff',
                  border: '1px solid rgba(153,68,255,0.5)', borderRadius: 6, padding: '9px 16px',
                  cursor: n === 0 || !groupDraft.trim() ? 'default' : 'pointer',
                  boxShadow: n === 0 || !groupDraft.trim() ? 'none' : '0 0 16px rgba(153,68,255,0.4)' }}>
                ⬢ LAUNCH → ALL {n}
              </button>
            </div>
          )}
          {/* live consensus progress: one segment per team, hides when all merged */}
          {groupTask && groupTask.teams.some(t => !t.merged) && (
            <div role="status" aria-label="consensus task progress"
              style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '8px 16px', flexWrap: 'wrap',
                borderTop: '1px solid rgba(153,68,255,0.25)', background: 'rgba(12,5,26,0.5)' }}>
              <span style={{ ...mono, fontSize: 9.5, color: '#b07aff', letterSpacing: '0.14em' }}>
                ⬢ IN FLIGHT · “{groupTask.text.slice(0, 60)}{groupTask.text.length > 60 ? '…' : ''}”
              </span>
              {groupTask.teams.map((t, i) => {
                const label = t.names.length > 1 ? t.names.join('+') : (t.names[0] || `team ${i + 1}`)
                const state = t.merged ? '✓ merged'
                  : t.merge_started ? `merging (${nameOf(t.merger)})…`
                  : `${t.done}/${t.total} slices`
                return (
                  <span key={i} style={{ ...mono, fontSize: 9.5, borderRadius: 4, padding: '3px 9px',
                    color: t.merged ? '#00ff88' : t.merge_started ? '#ffe14d' : 'var(--cyan)',
                    border: `1px solid ${t.merged ? 'rgba(0,255,136,0.35)' : t.merge_started ? 'rgba(255,225,77,0.35)' : 'rgba(0,212,255,0.3)'}`,
                    background: 'rgba(0,212,255,0.04)' }}
                    title={`${label} — merger: ${nameOf(t.merger)}`}>
                    {label.slice(0, 34)} · {state}
                  </span>
                )
              })}
            </div>
          )}
        </div>

        {backendLost && n === 0 && (
          <div style={{ marginBottom: 16 }}>
            <OfflineBanner onRetry={() => {
              window.clearTimeout(retryTimerRef.current)
              attemptsRef.current = 0
              connectWs()
              fetch('/api/panel').then(r => (r.ok ? r.json() : null))
                .then(j => { if (j) applyEvent({ ...j, type: 'snapshot' }) }).catch(() => { /* noop */ })
            }} />
          </div>
        )}

        {/* ── main: workspace grid + memory rail ── */}
        <div className="pp-main">
          <div ref={boardRef}
            style={{ position: 'relative', borderRadius: 14, border: '1px solid var(--border)',
              background: 'linear-gradient(180deg, rgba(4,16,32,0.5), rgba(2,8,18,0.35))',
              padding: 18, minHeight: 380 }}>
            {view === 'grid' && (
              <EdgeLayer size={boardSize} rects={rects} connections={connections} sessions={sessions}
                hot={hotEdges} reduced={reduced} nameOf={nameOf} hoverSid={hoverSid} />
            )}

            {n === 0 ? (
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center',
                justifyContent: 'center', minHeight: 340, textAlign: 'center', gap: 6 }}>
                <div aria-hidden="true" style={{ fontSize: 30, color: 'var(--cyan)', textShadow: 'var(--glow-sm)' }}>⧉</div>
                <div style={{ ...disp, fontSize: 16, fontWeight: 800, letterSpacing: '0.12em', color: 'var(--text-hi)' }}>
                  NO SESSIONS YET
                </div>
                <div style={{ fontFamily: 'var(--font-b)', fontSize: 13, color: 'var(--text)', maxWidth: 460, lineHeight: 1.65 }}>
                  Each session is an independent agent with its own chat and model.{' '}
                  <b style={{ color: 'var(--text-hi)' }}>＋ Add</b> a few, then wire them with{' '}
                  <b style={{ color: 'var(--amber)' }}>⚡ LINK</b> — linked sessions share context both ways
                  and one Project Memory. Flip to <b style={{ color: '#b07aff' }}>⬢ CONSENSUS</b> to give
                  the whole board ONE task they split among themselves.
                </div>
                <button type="button" onClick={addSession}
                  style={{ ...disp, marginTop: 14, fontSize: 11.5, fontWeight: 800, letterSpacing: '0.14em',
                    color: '#02121c', background: 'var(--cyan)', border: 'none', borderRadius: 6,
                    padding: '11px 22px', cursor: 'pointer', boxShadow: 'var(--glow-sm)' }}>
                  ＋ ADD FIRST SESSION
                </button>
              </div>
            ) : view === 'space' ? (
              <SpaceView sessions={sessions} ordered={ordered} connections={connections} hot={hotEdges}
                basePos={basePos} groupIndexOf={groupIndexOf} connectFrom={connectFrom} reduced={reduced}
                nameOf={nameOf}
                onActivate={sid => {
                  if (connectFrom === sid) setConnectFrom(null)
                  else if (connectFrom) makeLink(connectFrom, sid)
                  else setFocus(sid)
                }} />
            ) : (
              <div style={{ position: 'relative', zIndex: 1, display: 'grid', gap: 16,
                gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))' }}>
                {ordered.map(s => (
                  <SessionCard key={s.id} s={s} sessions={sessions} connections={connections}
                    connectFrom={connectFrom} reduced={reduced} nameOf={nameOf}
                    groupIdx={groupIndexOf[s.id] !== undefined ? groupIndexOf[s.id] : -1}
                    groupAccent={GROUP_ACCENTS[(groupIndexOf[s.id] || 0) % GROUP_ACCENTS.length]}
                    onHover={setHoverSid}
                    refCb={el => { if (el) cardEls.current.set(s.id, el); else cardEls.current.delete(s.id) }}
                    onOpen={() => setFocus(s.id)}
                    onPickTarget={() => { if (connectFrom) makeLink(connectFrom, s.id) }}
                    onCancelConnect={() => setConnectFrom(null)}
                    onStartConnect={() => setConnectFrom(s.id)}
                    onStop={() => stopTurn(s.id)} />
                ))}
              </div>
            )}

            {n > 0 && (
              <div style={{ ...mono, position: 'relative', zIndex: 1, fontSize: 9.5, color: 'var(--text-lo)',
                marginTop: 16, letterSpacing: '0.08em' }}>
                {view === 'space'
                  ? 'drag to rotate the space · hover a star to isolate its links · click a star to open it (or pick it as a ⚡ LINK target) · links are bidirectional — energy streams show each live transfer'
                  : 'click a card to open its chat · ⚡ LINK then click a target to wire A ⇋ B (both ways) · linked agents message each other and share one project memory · hover a card to isolate its links · a bright pulse = context transferring right now'}
              </div>
            )}
          </div>

          <div style={{ position: 'sticky', top: 64, display: 'flex', flexDirection: 'column',
            gap: 10, minWidth: 0 }}>
            {/* rail tabs: shared memory vs published output */}
            <div role="tablist" aria-label="right rail content"
              style={{ display: 'flex', border: '1px solid var(--border-hi)', borderRadius: 8,
                overflow: 'hidden', flexShrink: 0 }}>
              {(['memory', 'output'] as const).map(t => {
                const unread = t === 'output' ? Math.max(0, deliverables.length - seenOutput.current) : 0
                return (
                  <button key={t} type="button" role="tab" aria-selected={railTab === t}
                    onClick={() => { setRailTab(t); if (t === 'output') seenOutput.current = deliverables.length }}
                    style={{ ...disp, flex: 1, fontSize: 10, fontWeight: 800, letterSpacing: '0.16em',
                      padding: '9px 0', cursor: 'pointer', border: 'none',
                      background: railTab === t ? 'var(--cyan)' : 'transparent',
                      color: railTab === t ? '#02121c' : 'var(--cyan)' }}>
                    {t === 'memory' ? '▤ MEMORY' : `⬢ OUTPUT (${deliverables.length})`}
                    {railTab !== t && unread > 0 ? ' ●' : ''}
                  </button>
                )
              })}
            </div>
            {railTab === 'memory'
              ? <MemoryRail ledger={ledger} groups={groups} sessions={sessions} nameOf={nameOf} />
              : <OutputRail deliverables={deliverables} sessions={sessions} />}
          </div>
        </div>
      </div>

      {focused && (
        <FocusView s={focused} sessions={sessions} connections={connections} models={models}
          nameOf={nameOf} reduced={reduced}
          onBack={() => setFocus(null)}
          onChat={text => sendChat(focused.id, text)}
          onStop={() => stopTurn(focused.id)}
          onRemove={() => { removeSession(focused.id); setFocus(null) }}
          onModel={m => setModel(focused.id, m)}
          onDisconnect={to => disconnect(focused.id, to)}
          onStartConnect={() => { setConnectFrom(focused.id); setFocus(null) }}
          onPersona={p => savePersona(focused.id, p)} />
      )}

      <Toasts toasts={toasts} dismiss={id => setToasts(p => p.filter(t => t.id !== id))} />
    </PageShell>
  )
}
