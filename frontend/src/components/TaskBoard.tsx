import { motion, AnimatePresence } from 'framer-motion'
import type { Todo } from '../types/protocol'

// ── Progress ring — SVG circle showing completed/total ─────────
function ProgressRing({ done, total }: { done: number; total: number }) {
  const R = 8, C = 2 * Math.PI * R
  const frac = total > 0 ? done / total : 0
  return (
    <svg width="22" height="22" viewBox="0 0 22 22" style={{ flexShrink: 0 }}>
      <circle cx="11" cy="11" r={R} fill="none" stroke="rgba(0,212,255,0.14)" strokeWidth="2" />
      <circle cx="11" cy="11" r={R} fill="none"
        stroke={frac >= 1 ? 'var(--green)' : 'var(--cyan)'} strokeWidth="2"
        strokeLinecap="round"
        strokeDasharray={C}
        strokeDashoffset={C * (1 - frac)}
        transform="rotate(-90 11 11)"
        style={{ transition: 'stroke-dashoffset 0.6s cubic-bezier(0.4,0,0.2,1), stroke 0.4s',
          filter: 'drop-shadow(0 0 3px rgba(0,212,255,0.6))' }} />
    </svg>
  )
}

const GLYPH: Record<Todo['status'], { icon: string; color: string; glow: string }> = {
  pending:     { icon: '◇', color: 'var(--text-lo)', glow: 'none' },
  in_progress: { icon: '▸', color: 'var(--cyan)',    glow: '0 0 8px rgba(0,212,255,0.7)' },
  completed:   { icon: '✓', color: 'var(--green)',   glow: '0 0 6px rgba(0,255,136,0.5)' },
}

function TodoItem({ todo }: { todo: Todo }) {
  const g = GLYPH[todo.status]
  const cls = todo.status === 'in_progress' ? 'todo-active'
            : todo.status === 'completed'   ? 'todo-done' : undefined
  return (
    <motion.div layout
      initial={{ opacity: 0, x: -8 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: 8 }}
      transition={{ duration: 0.18, layout: { type: 'spring', stiffness: 320, damping: 30 } }}
      className={cls}
      style={{ display: 'flex', alignItems: 'flex-start', gap: 8, padding: '5px 6px',
        borderRadius: 2, borderBottom: '1px solid rgba(0,212,255,0.05)' }}>
      <span style={{ fontSize: 'var(--fs-label)', color: g.color, flexShrink: 0, marginTop: 1, textShadow: g.glow,
        animation: todo.status === 'in_progress' ? 'softPulse 0.9s ease-in-out infinite' : 'none' }}>
        {g.icon}
      </span>
      <span style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-label)', lineHeight: 1.5, minWidth: 0,
        color: todo.status === 'completed'   ? 'rgba(0,255,136,0.65)'
             : todo.status === 'in_progress' ? 'var(--text-hi)' : 'var(--text)',
        textDecoration: todo.status === 'completed' ? 'line-through' : 'none',
        textDecorationColor: 'rgba(0,255,136,0.35)' }}>
        {todo.text}
      </span>
    </motion.div>
  )
}

// Rendered INSIDE a HudTabs panel — the tab strip is the chrome, so no
// hud-panel wrapper or title bar here.
export default function TaskBoard({ todos }: { todos: Todo[] }) {
  const done  = todos.filter(t => t.status === 'completed').length
  const total = todos.length

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      {total > 0 && (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end',
          gap: 6, padding: '6px 12px 0' }}>
          <span style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-cap, 8px)',
            letterSpacing: '0.1em',
            color: done === total ? 'var(--green)' : 'var(--text)' }}>
            {done}/{total}
          </span>
          <ProgressRing done={done} total={total} />
        </div>
      )}

      <div style={{ flex: 1, overflowY: 'auto', padding: '6px 8px', minHeight: 0 }}>
        {total === 0 ? (
          <div style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-label, 9px)',
            color: 'var(--text-lo)', letterSpacing: '0.14em', textAlign: 'center', marginTop: 20 }}>
            NO ACTIVE MISSION
          </div>
        ) : (
          <AnimatePresence initial={false}>
            {todos.map(todo => <TodoItem key={todo.id} todo={todo} />)}
          </AnimatePresence>
        )}
      </div>
    </div>
  )
}
