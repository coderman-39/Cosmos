import { useRef, useEffect, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { ToolCall, AgentThought, ActionRun } from '../store'

// ── Per-tool SVG glyphs (stroke = currentColor) ─────────────────

type GlyphKind = 'terminal' | 'search' | 'globe' | 'eye' | 'file' | 'keyboard' | 'message' | 'gear'

const TOOL_GLYPH: Record<string, GlyphKind> = {
  bash: 'terminal', applescript: 'terminal', system_state: 'terminal', set_volume: 'terminal',
  web_search: 'search',
  open_url: 'globe', fetch_url: 'globe', read_browser: 'globe', browser_js: 'globe',
  see_screen: 'eye', screenshot: 'eye', read_app: 'eye',
  read_file: 'file', write_file: 'file', open_path: 'file',
  type_text: 'keyboard', keystroke: 'keyboard', click_ui: 'keyboard', mouse: 'keyboard',
  slack_dm: 'message', say: 'message', ask_user: 'message',
  set_todos: 'gear', open_app: 'gear',
}

function ToolGlyph({ tool, size = 12 }: { tool: string; size?: number }) {
  const kind = TOOL_GLYPH[tool] ?? 'gear'
  const p = { width: size, height: size, viewBox: '0 0 16 16', fill: 'none',
    stroke: 'currentColor', strokeWidth: 1.4, strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const, style: { flexShrink: 0, display: 'block' } }
  switch (kind) {
    case 'terminal': return (
      <svg {...p}><rect x="1.5" y="2.5" width="13" height="11" rx="1"/><path d="M4.5 6l2.5 2-2.5 2M8.5 10.5h3"/></svg>)
    case 'search': return (
      <svg {...p}><circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5L14 14"/></svg>)
    case 'globe': return (
      <svg {...p}><circle cx="8" cy="8" r="6"/><path d="M2 8h12M8 2c-4 4-4 8 0 12M8 2c4 4 4 8 0 12"/></svg>)
    case 'eye': return (
      <svg {...p}><path d="M1.5 8s2.5-4.5 6.5-4.5S14.5 8 14.5 8s-2.5 4.5-6.5 4.5S1.5 8 1.5 8z"/><circle cx="8" cy="8" r="2"/></svg>)
    case 'file': return (
      <svg {...p}><path d="M4 1.5h5.5L13 5v9.5H4z"/><path d="M9.5 1.5V5H13"/></svg>)
    case 'keyboard': return (
      <svg {...p}><rect x="1.5" y="4.5" width="13" height="7" rx="1"/><path d="M4 7h.01M7 7h.01M10 7h.01M12 7h.01M5 9.5h6"/></svg>)
    case 'message': return (
      <svg {...p}><path d="M1.5 3.5h13v8h-7L4 14.5v-3H1.5z"/></svg>)
    case 'gear': return (
      <svg {...p}><circle cx="8" cy="8" r="2.5"/><path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M12.6 3.4l-1.4 1.4M4.8 11.2l-1.4 1.4"/></svg>)
  }
}

// ── Formatting ──────────────────────────────────────────────────

const fmtDuration = (ms?: number) =>
  ms === undefined ? '' : ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`

const STATUS_STYLE: Record<ToolCall['status'], { color: string; edge: string }> = {
  running: { color: 'var(--cyan)',  edge: 'rgba(0,212,255,0.55)' },
  done:    { color: 'var(--green)', edge: 'rgba(0,255,136,0.45)' },
  error:   { color: 'var(--red)',   edge: 'rgba(255,34,68,0.5)' },
}

// ── Tool-call card ──────────────────────────────────────────────

function ToolCard({ call }: { call: ToolCall }) {
  const s = STATUS_STYLE[call.status]
  const [expanded, setExpanded] = useState(false)
  const expandable = !!call.detail && call.detail.length > 40
  return (
    <div className={call.status === 'running' ? 'tool-running' : undefined}
      style={{ display: 'flex', alignItems: 'flex-start', gap: 8, padding: '5px 8px',
        marginBottom: 3, borderRadius: 2,
        background: 'rgba(0,212,255,0.03)',
        border: '1px solid rgba(0,212,255,0.07)',
        borderLeft: `2px solid ${s.edge}`,
        boxShadow: call.status === 'running' ? `inset 0 0 12px rgba(0,212,255,0.05)` : 'none',
        transition: 'border-color 0.3s' }}>
      <span style={{ color: s.color, marginTop: 2,
        animation: call.status === 'running' ? 'softPulse 0.9s ease-in-out infinite' : 'none' }}>
        <ToolGlyph tool={call.tool} />
      </span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
          <span style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-label)', flex: 1, minWidth: 0,
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            color: call.status === 'error' ? 'var(--red)' : 'var(--text-hi)' }}>
            {call.label}
          </span>
          {call.status !== 'running' && call.durationMs !== undefined && (
            <span style={{ fontFamily: 'var(--font-d)', fontSize: 'var(--fs-cap)', letterSpacing: '0.1em',
              color: s.color, opacity: 0.85, flexShrink: 0 }}>
              {fmtDuration(call.durationMs)}
            </span>
          )}
          {expandable && (
            <button aria-label={expanded ? 'Collapse output' : 'Expand output'}
              aria-expanded={expanded}
              onClick={() => setExpanded(x => !x)}
              style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0,
                color: 'var(--text-lo)', fontSize: 'var(--fs-label)', flexShrink: 0,
                transform: expanded ? 'rotate(90deg)' : 'none', transition: 'transform 0.15s' }}>
              ›
            </button>
          )}
        </div>
        {call.detail && (expanded ? (
          <pre style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-cap)', color: 'var(--text)',
            marginTop: 3, maxHeight: 180, overflow: 'auto', whiteSpace: 'pre-wrap',
            wordBreak: 'break-word', background: 'rgba(0,0,0,0.3)',
            border: '1px solid rgba(0,212,255,0.08)', borderRadius: 2, padding: '5px 7px' }}>
            {call.detail}
          </pre>
        ) : (
          <div style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-cap)', color: 'rgba(0,212,255,0.5)',
            marginTop: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {call.detail}
          </div>
        ))}
      </div>
    </div>
  )
}

function ThoughtLine({ thought }: { thought: AgentThought }) {
  return (
    <div style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-cap)', fontStyle: 'italic',
      color: 'rgba(104,152,184,0.55)', padding: '2px 8px 4px 22px', lineHeight: 1.5 }}>
      …{thought.text}
    </div>
  )
}

// ── Run history — collapsible archive of past runs ──────────────
// Exported: the Flight Recorder tab renders these.

export function HistoryRun({ run, defaultOpen = false }: { run: ActionRun; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  const okCount  = run.toolCalls.filter(t => t.status === 'done').length
  const errCount = run.toolCalls.filter(t => t.status === 'error').length
  const ts = new Date(run.startedAt).toLocaleTimeString('en-US',
    { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })

  return (
    <div style={{ marginBottom: 4, borderRadius: 2, overflow: 'hidden',
      border: `1px solid ${run.ok ? 'rgba(0,255,136,0.12)' : 'rgba(255,34,68,0.12)'}`,
      background: run.ok ? 'rgba(0,255,136,0.03)' : 'rgba(255,34,68,0.03)' }}>
      {/* Header */}
      <div onClick={() => setOpen(o => !o)}
        style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 8px',
          cursor: 'pointer', userSelect: 'none' }}>
        <span style={{ color: run.ok ? 'var(--green)' : 'var(--red)', fontSize: 9 }}>
          {run.ok ? '✓' : '✗'}
        </span>
        <span style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-label)', color: 'var(--text)',
          flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {run.command.slice(0, 60)}
        </span>
        <span style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-cap)', color: 'var(--text-lo)',
          flexShrink: 0 }}>
          {ts}
        </span>
        <span style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-cap)',
          color: errCount ? 'var(--red)' : 'var(--text-lo)', flexShrink: 0 }}>
          {okCount}✓ {errCount > 0 ? `${errCount}✗` : ''}
        </span>
        <span style={{ color: 'var(--text-lo)', fontSize: 'var(--fs-label)', transition: 'transform 0.15s',
          transform: open ? 'rotate(90deg)' : 'rotate(0deg)' }}>›</span>
      </div>
      {/* Tool calls */}
      {open && (
        <div style={{ padding: '4px 8px 6px', borderTop: '1px solid rgba(0,212,255,0.06)' }}>
          {run.toolCalls.map(t => <ToolCard key={t.id} call={t} />)}
          {run.toolCalls.length === 0 && (
            <div style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-cap)', color: 'var(--text-lo)' }}>
              No tool calls recorded
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ── Main panel ──────────────────────────────────────────────────

type FeedItem =
  | { kind: 'tool'; ts: string; call: ToolCall }
  | { kind: 'thought'; ts: string; thought: AgentThought }

interface Props {
  toolCalls: ToolCall[]
  thoughts: AgentThought[]
  isExecuting: boolean
  currentCommand: string
}

// Rendered INSIDE a HudTabs panel — the tab strip is the chrome. Past runs
// live in the Flight Recorder tab (see FlightRecorder.tsx).
export default function AgentActivity({ toolCalls, thoughts, isExecuting, currentCommand }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [toolCalls, thoughts])

  // Interleave tool cards and dim thought lines chronologically
  const feed: FeedItem[] = [
    ...toolCalls.map(call => ({ kind: 'tool' as const, ts: call.ts, call })),
    ...thoughts.map(thought => ({ kind: 'thought' as const, ts: thought.ts, thought })),
  ].sort((a, b) => a.ts.localeCompare(b.ts))

  return (
    <div style={{ flex: 1, overflowY: 'auto', padding: '8px 10px',
      display: 'flex', flexDirection: 'column', minHeight: 0 }}>

      {isExecuting && (
        <div style={{ display: 'flex', justifyContent: 'flex-end', flexShrink: 0 }}>
          <span style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-cap, 7.5px)',
            color: 'var(--amber)', letterSpacing: '0.12em',
            animation: 'softPulse 0.8s ease-in-out infinite' }}>
            ● RUNNING
          </span>
        </div>
      )}

      {/* Current command */}
      {(isExecuting || feed.length > 0) && currentCommand && (
        <div style={{ fontFamily: 'var(--font-d)', fontSize: 'var(--fs-cap, 8px)',
          color: 'rgba(0,212,255,0.5)',
          letterSpacing: '0.12em', marginBottom: 6, overflow: 'hidden',
          textOverflow: 'ellipsis', whiteSpace: 'nowrap', flexShrink: 0 }}>
          ◈ {currentCommand.slice(0, 70)}
        </div>
      )}

      {feed.length === 0 && !isExecuting ? (
        <div style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-label, 9px)',
          color: 'var(--text-lo)', textAlign: 'center', marginTop: 24, lineHeight: 2 }}>
          Agent tool activity appears here<br/>
          <span style={{ fontSize: 'var(--fs-cap, 8px)', color: 'rgba(0,212,255,0.35)' }}>
            The RECORDER tab keeps past runs
          </span>
        </div>
      ) : (
        <AnimatePresence initial={false}>
          {feed.map(item => (
            <motion.div key={item.kind === 'tool' ? item.call.id : item.thought.id}
              initial={{ opacity: 0, x: -8 }} animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.16 }}>
              {item.kind === 'tool'
                ? <ToolCard call={item.call} />
                : <ThoughtLine thought={item.thought} />}
            </motion.div>
          ))}
        </AnimatePresence>
      )}

      {/* Running indicator */}
      {isExecuting && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '4px 0' }}>
          {[0, 150, 300].map(d => (
            <div key={d} style={{ width: 5, height: 5, borderRadius: '50%',
              background: 'var(--amber)', boxShadow: '0 0 6px var(--amber)',
              animation: `softPulse 0.8s ease-in-out ${d}ms infinite` }}/>
          ))}
        </div>
      )}

      <div ref={bottomRef} />
    </div>
  )
}
