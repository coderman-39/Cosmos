import { useEffect, useRef, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import PageShell, { OfflineBanner } from './PageShell'
import Markdown from './Markdown'
import type { Page } from '../store'

// ── Slack command bridge: live status + a timeline of every task Cosmos ran
// from the Slack channel — what was asked, how it worked the problem (tool
// timeline), what it delivered, and the final reply. Polls the backend. ──

interface BridgeStatus {
  enabled: boolean; connected: boolean; channel: string; owner: string
  last_event: string; last_ignored?: string; runs: number; note: string
  running: boolean; queued: number; active_thread?: string
}
interface TaskEvent {
  t: string; kind: string; tool?: string; label?: string
  ok?: boolean; detail?: string; text?: string
}
interface TaskRec {
  thread: string; text: string; ts: string; started: string; status: string
  reply: string; duration_s: number; events: TaskEvent[]; files: string[]
}

const STATUS_META: Record<string, { color: string; label: string; pulse?: boolean }> = {
  'queued':            { color: 'var(--text-lo)', label: 'QUEUED' },
  'running':           { color: 'var(--cyan)',    label: 'RUNNING', pulse: true },
  'awaiting approval': { color: 'var(--amber)',   label: 'AWAITING YES/NO', pulse: true },
  'awaiting reply':    { color: 'var(--amber)',   label: 'AWAITING REPLY', pulse: true },
  'done':              { color: 'var(--green, #2ecc71)', label: 'DONE' },
  'stopped':           { color: 'var(--text-lo)', label: 'STOPPED' },
  'error':             { color: 'var(--red, #ff5566)', label: 'ERROR' },
}

const mono: React.CSSProperties = { fontFamily: 'var(--font-m)', letterSpacing: '0.06em' }

function StatusOrb({ s }: { s: BridgeStatus | null }) {
  const ok = !!s?.connected
  const color = !s?.enabled ? 'var(--text-lo)' : ok ? 'var(--green, #2ecc71)' : 'var(--amber)'
  return (
    <span style={{ position: 'relative', width: 10, height: 10, display: 'inline-block' }}>
      <motion.span
        animate={ok ? { scale: [1, 1.9], opacity: [0.55, 0] } : {}}
        transition={{ repeat: Infinity, duration: 1.8, ease: 'easeOut' }}
        style={{ position: 'absolute', inset: 0, borderRadius: '50%', background: color }} />
      <span style={{ position: 'absolute', inset: 0, borderRadius: '50%',
        background: color, boxShadow: `0 0 10px ${color}` }} />
    </span>
  )
}

function EventRow({ e }: { e: TaskEvent }) {
  const time = (e.t || '').slice(11, 19)
  if (e.kind === 'tool') return (
    <div style={{ ...mono, fontSize: 10.5, color: 'var(--text)', display: 'flex', gap: 10 }}>
      <span style={{ color: 'var(--text-lo)' }}>{time}</span>
      <span style={{ color: 'var(--cyan)' }}>→ {e.tool}</span>
      <span style={{ color: 'var(--text-lo)', overflow: 'hidden', textOverflow: 'ellipsis',
        whiteSpace: 'nowrap' }}>{e.label}</span>
    </div>)
  if (e.kind === 'tool_done') return (
    <div style={{ ...mono, fontSize: 10.5, display: 'flex', gap: 10, paddingLeft: 66 }}>
      <span style={{ color: e.ok ? 'var(--green, #2ecc71)' : 'var(--red, #ff5566)' }}>
        {e.ok ? '✓' : '✗'}</span>
      <span style={{ color: 'var(--text-lo)', overflow: 'hidden', textOverflow: 'ellipsis',
        whiteSpace: 'nowrap' }}>{e.detail}</span>
    </div>)
  const tone = e.kind === 'confirm' || e.kind === 'timeout' ? 'var(--amber)'
    : e.kind === 'ask' ? 'var(--amber)'
    : e.kind === 'answer' ? 'var(--green, #2ecc71)' : 'var(--text-lo)'
  const glyph = { confirm: '⚠', ask: '❓', answer: '↩', say: '💬', thought: '·', timeout: '⏰' }[e.kind] || '·'
  return (
    <div style={{ ...mono, fontSize: 10.5, display: 'flex', gap: 10 }}>
      <span style={{ color: 'var(--text-lo)' }}>{time}</span>
      <span style={{ color: tone }}>{glyph} {e.text || e.kind}</span>
    </div>)
}

function TaskCard({ rec, live }: { rec: TaskRec; live: boolean }) {
  const [open, setOpen] = useState(live)
  useEffect(() => { if (live) setOpen(true) }, [live])
  const meta = STATUS_META[rec.status] || STATUS_META.queued
  return (
    <motion.div layout initial={{ opacity: 0, y: 14 }} animate={{ opacity: 1, y: 0 }}
      style={{ background: 'var(--bg-panel)', border: `1px solid ${live ? 'var(--border-hi)' : 'var(--border)'}`,
        borderRadius: 8, overflow: 'hidden',
        boxShadow: live ? 'var(--glow-sm)' : 'none' }}>
      {/* Header */}
      <button onClick={() => setOpen(o => !o)}
        style={{ width: '100%', display: 'flex', alignItems: 'center', gap: 14,
          padding: '14px 18px', background: 'transparent', border: 'none',
          cursor: 'pointer', textAlign: 'left' }}>
        <motion.span
          animate={meta.pulse ? { opacity: [1, 0.35, 1] } : {}}
          transition={{ repeat: Infinity, duration: 1.4 }}
          style={{ ...mono, fontSize: 9, fontWeight: 700, color: meta.color,
            border: `1px solid ${meta.color}`, borderRadius: 3, padding: '3px 8px',
            letterSpacing: '0.14em', flexShrink: 0 }}>
          {meta.label}
        </motion.span>
        <span style={{ fontFamily: 'var(--font-b)', fontSize: 14, color: 'var(--text-hi)',
          flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {rec.text}
        </span>
        <span style={{ ...mono, fontSize: 10, color: 'var(--text-lo)', flexShrink: 0 }}>
          {(rec.started || '').slice(11, 16)}
          {rec.duration_s > 0 && ` · ${rec.duration_s}s`}
          {rec.files.length > 0 && ` · 📎${rec.files.length}`}
        </span>
        <span style={{ color: 'var(--text-lo)', fontSize: 11, flexShrink: 0 }}>
          {open ? '▾' : '▸'}</span>
      </button>

      <AnimatePresence>
        {open && (
          <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.22 }}
            style={{ overflow: 'hidden' }}>
            <div style={{ padding: '2px 18px 16px', borderTop: '1px solid var(--border)' }}>
              {rec.events.length > 0 && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 5,
                  padding: '12px 0', maxHeight: 220, overflowY: 'auto' }}>
                  {rec.events.map((e, i) => <EventRow key={i} e={e} />)}
                </div>
              )}
              {rec.files.length > 0 && (
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', padding: '4px 0 12px' }}>
                  {rec.files.map((f, i) => (
                    <span key={i} style={{ ...mono, fontSize: 10, color: 'var(--cyan)',
                      border: '1px solid var(--border-hi)', background: 'var(--cyan-5)',
                      borderRadius: 4, padding: '4px 10px' }}>📎 {f}</span>
                  ))}
                </div>
              )}
              {rec.reply && (
                <div style={{ background: 'var(--cyan-5)', border: '1px solid var(--border)',
                  borderRadius: 6, padding: '12px 16px', fontSize: 13.5 }}>
                  <div style={{ ...mono, fontSize: 8.5, color: 'var(--text-lo)',
                    letterSpacing: '0.3em', marginBottom: 8 }}>COSMOS · REPLY</div>
                  <Markdown text={rec.reply} />
                </div>
              )}
              {!rec.reply && rec.events.length === 0 && (
                <div style={{ ...mono, fontSize: 11, color: 'var(--text-lo)', padding: '12px 0' }}>
                  Waiting for the run to produce activity…
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}

const LIVE_STATUSES = ['running', 'awaiting approval', 'awaiting reply', 'queued']
const isLive = (s: string) => LIVE_STATUSES.includes(s)

interface Convo { thread: string; title: string; turns: TaskRec[] }

// One Slack thread = one conversation. Group the flat run history by thread,
// preserving newest-first order (threads[] arrives newest-first).
function groupByThread(recs: TaskRec[]): Convo[] {
  const order: string[] = []
  const byThread: Record<string, TaskRec[]> = {}
  for (const r of recs) {
    const k = r.thread || r.ts
    if (!byThread[k]) { byThread[k] = []; order.push(k) }
    byThread[k].push(r)
  }
  return order.map(k => {
    const turns = byThread[k]
    return { thread: k, turns, title: turns[turns.length - 1]?.text || '(conversation)' }
  })
}

// A conversation: its root prompt as a title, a WORKING pulse when the thread
// is the one Cosmos is actively running, and every turn nested inside.
function ConversationGroup({ c, working }: { c: Convo; working: boolean }) {
  const turns = c.turns.length
  const last = c.turns[0]                       // newest turn (list is newest-first)
  const color = working ? 'var(--cyan)' : 'var(--border)'
  return (
    <motion.div layout initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }}
      style={{ border: `1px solid ${working ? 'var(--border-hi)' : 'var(--border)'}`,
        borderLeft: `2px solid ${color}`, borderRadius: 8, padding: '12px 12px 12px 14px',
        background: 'var(--bg-panel)', boxShadow: working ? 'var(--glow-sm)' : 'none' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: turns ? 10 : 0 }}>
        {working ? (
          <motion.span animate={{ opacity: [1, 0.35, 1] }}
            transition={{ repeat: Infinity, duration: 1.4 }}
            style={{ ...mono, fontSize: 8.5, fontWeight: 700, color: 'var(--cyan)',
              border: '1px solid var(--cyan)', borderRadius: 3, padding: '3px 8px',
              letterSpacing: '0.16em', flexShrink: 0 }}>● WORKING</motion.span>
        ) : (
          <span style={{ ...mono, fontSize: 8.5, color: 'var(--text-lo)',
            letterSpacing: '0.24em', flexShrink: 0 }}>THREAD</span>
        )}
        <span style={{ fontFamily: 'var(--font-d)', fontSize: 13, fontWeight: 700,
          color: 'var(--text-hi)', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis',
          whiteSpace: 'nowrap' }}>{c.title}</span>
        <span style={{ ...mono, fontSize: 9.5, color: 'var(--text-lo)', flexShrink: 0 }}>
          {turns} turn{turns === 1 ? '' : 's'}
          {last?.started ? ` · ${last.started.slice(11, 16)}` : ''}
        </span>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {c.turns.map(t => (
          <TaskCard key={t.ts} rec={t} live={isLive(t.status)} />
        ))}
      </div>
    </motion.div>
  )
}

export default function SlackBridgePage({ page, onNavigate }: {
  page?: Page; onNavigate?: (p: Page) => void
}) {
  const [status, setStatus] = useState<BridgeStatus | null>(null)
  const [threads, setThreads] = useState<TaskRec[]>([])
  const [offline, setOffline] = useState(false)
  const timer = useRef<number>()

  const poll = () => {
    fetch('/api/slack-bridge/activity').then(r => r.ok ? r.json() : Promise.reject())
      .then(d => { setStatus(d.status); setThreads(d.threads || []); setOffline(false) })
      .catch(() => setOffline(true))
  }
  useEffect(() => {
    poll()
    timer.current = window.setInterval(poll, 2500)
    return () => window.clearInterval(timer.current)
  }, [])

  const chips = [
    { k: 'CHANNEL', v: status?.channel || '—' },
    { k: 'TASKS RUN', v: String(status?.runs ?? 0) },
    { k: 'QUEUED', v: String(status?.queued ?? 0) },
    { k: 'LAST EVENT', v: status?.last_event ? status.last_event.slice(11, 19) : '—' },
  ]

  return (
    <PageShell title="Slack Bridge" page={page} onNavigate={onNavigate}
      subtitle="COMMAND COSMOS FROM SLACK · REPLIES, FILES & APPROVALS IN-THREAD"
      right={
        <div style={{ display: 'flex', alignItems: 'center', gap: 10,
          border: '1px solid var(--border)', borderRadius: 6, padding: '10px 16px',
          background: 'var(--bg-panel)' }}>
          <StatusOrb s={status} />
          <span style={{ ...mono, fontSize: 10, letterSpacing: '0.18em',
            color: status?.connected ? 'var(--green, #2ecc71)'
              : status?.enabled ? 'var(--amber)' : 'var(--text-lo)' }}>
            {status?.connected ? 'SOCKET LIVE' : status?.enabled ? 'RECONNECTING' : 'OFFLINE'}
          </span>
        </div>
      }>

      {offline && <div style={{ marginBottom: 22 }}><OfflineBanner onRetry={poll} /></div>}

      {/* Status strip */}
      {status && (
        <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}
          style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12,
            marginBottom: 14 }}>
          {chips.map(c => (
            <div key={c.k} style={{ background: 'var(--bg-panel)', borderRadius: 6,
              border: '1px solid var(--border)', padding: '12px 16px' }}>
              <div style={{ ...mono, fontSize: 8.5, color: 'var(--text-lo)',
                letterSpacing: '0.3em' }}>{c.k}</div>
              <div style={{ fontFamily: 'var(--font-d)', fontSize: 16, fontWeight: 700,
                color: 'var(--text-hi)', marginTop: 6 }}>{c.v}</div>
            </div>
          ))}
        </motion.div>
      )}

      {/* Why the last unprocessed message was skipped — the "it's ignoring
          me" debugger. Cleared implicitly when a newer message runs. */}
      {status?.last_ignored && (
        <div style={{ ...mono, fontSize: 10.5, color: 'var(--text-lo)',
          padding: '2px 4px 12px' }}>
          ⃠ last ignored message: {status.last_ignored}
        </div>
      )}

      {/* Misconfiguration note straight from the backend */}
      {status?.note && (
        <div style={{ background: 'rgba(255,149,0,0.06)', border: '1px solid rgba(255,149,0,0.4)',
          borderRadius: 6, padding: '12px 16px', marginBottom: 14,
          ...mono, fontSize: 11.5, color: 'var(--amber)' }}>
          ⚠ {status.note}
        </div>
      )}

      {/* Task timeline */}
      {threads.length === 0 && !offline ? (
        <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }}
          style={{ border: '1px dashed var(--border)', borderRadius: 8,
            padding: '46px 20px', textAlign: 'center' }}>
          <div style={{ fontSize: 26, marginBottom: 12 }}>✦</div>
          <div style={{ fontFamily: 'var(--font-d)', fontSize: 14, fontWeight: 700,
            letterSpacing: '0.1em', color: 'var(--text-hi)' }}>NO TASKS YET</div>
          <div style={{ fontFamily: 'var(--font-b)', fontSize: 13, color: 'var(--text)',
            marginTop: 10, lineHeight: 1.7 }}>
            Message the bridge channel — <span style={{ ...mono, color: 'var(--cyan)' }}>
            cosmos take a screenshot of &lt;url&gt; and send it here</span><br />
            Cosmos picks it up, works it, and answers in the thread. Follow-ups,
            yes/no approvals and <span style={{ ...mono }}>stop</span> all work in-thread.
          </div>
        </motion.div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {groupByThread(threads).map(c => (
            <ConversationGroup key={c.thread} c={c}
              working={status?.active_thread === c.thread
                || c.turns.some(t => isLive(t.status))} />
          ))}
        </div>
      )}
    </PageShell>
  )
}
