import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import type { Page } from '../store'

// ── Hamburger + slide-in navigation drawer, fixed top-left on every page. ──
const ITEMS: { id: Page; label: string; glyph: string; sub: string }[] = [
  { id: 'home',   label: 'Home',       glyph: '⌂', sub: 'Overview' },
  { id: 'agent',  label: 'Agent',      glyph: '◉', sub: 'Live assistant' },
  { id: 'nexus',  label: 'Nexus',      glyph: '◈', sub: 'Mind-map cortex' },
  { id: 'dossier',label: 'Dossier',    glyph: '❖', sub: 'People · promises · tasks' },
  { id: 'vision', label: 'Vision',     glyph: '◉', sub: 'Screen watchers · alerts' },
  { id: 'kinesis',label: 'Kinesis',    glyph: '◎', sub: 'Record · replay macros' },
  { id: 'slack',  label: 'Slack',      glyph: '✦', sub: 'Remote command bridge' },
  { id: 'panel',  label: 'Panel',      glyph: '⧉', sub: 'Multi-agent swarm board' },
  { id: 'skills', label: 'Skills',     glyph: '▤', sub: 'Playbooks · AI-edit' },
  { id: 'mutate', label: 'Mutate',     glyph: 'Δ', sub: 'Self-healing · evolution' },
  { id: 'mcps',   label: 'Connectors', glyph: '⬡', sub: 'Integrations · MCP' },
]

export default function NavMenu({ page, onNavigate }: {
  page: Page; onNavigate: (p: Page) => void
}) {
  const [open, setOpen] = useState(false)
  const go = (p: Page) => { setOpen(false); onNavigate(p) }

  return (
    <>
      {/* Hamburger button */}
      <button aria-label="Menu" onClick={() => setOpen(o => !o)}
        style={{
          position: 'fixed', top: 14, left: 16, zIndex: 120,
          width: 40, height: 40, display: 'flex', flexDirection: 'column',
          alignItems: 'center', justifyContent: 'center', gap: 4,
          background: 'rgba(3,13,30,0.75)', border: '1px solid var(--border-hi)',
          borderRadius: 6, cursor: 'pointer', backdropFilter: 'blur(6px)',
          boxShadow: open ? 'var(--glow-sm)' : 'none', transition: 'box-shadow .2s',
        }}>
        {[0, 1, 2].map(i => (
          <motion.span key={i} animate={{
            rotate: open ? (i === 0 ? 45 : i === 2 ? -45 : 0) : 0,
            y: open ? (i === 0 ? 6 : i === 2 ? -6 : 0) : 0,
            opacity: open && i === 1 ? 0 : 1,
          }} style={{ display: 'block', width: 20, height: 2, borderRadius: 2,
            background: 'var(--cyan)', boxShadow: 'var(--glow-xs)' }} />
        ))}
      </button>

      <AnimatePresence>
        {open && (
          <>
            <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
              onClick={() => setOpen(false)}
              style={{ position: 'fixed', inset: 0, zIndex: 110,
                background: 'rgba(1,6,16,0.55)', backdropFilter: 'blur(2px)' }} />
            <motion.nav
              initial={{ x: -280 }} animate={{ x: 0 }} exit={{ x: -280 }}
              transition={{ type: 'spring', stiffness: 320, damping: 32 }}
              style={{ position: 'fixed', top: 0, left: 0, bottom: 0, width: 264, zIndex: 115,
                background: 'var(--bg-panel)', borderRight: '1px solid var(--border-hi)',
                boxShadow: 'var(--glow-md)', padding: '72px 14px 20px',
                display: 'flex', flexDirection: 'column', gap: 6 }}>
              <div style={{ fontFamily: 'var(--font-d)', fontSize: 20, fontWeight: 900,
                color: 'var(--cyan)', letterSpacing: '0.22em', padding: '0 10px 14px',
                textShadow: 'var(--glow-sm)' }}>
                COSMOS
                <div style={{ fontFamily: 'var(--font-m)', fontSize: 8,
                  color: 'var(--text-lo)', letterSpacing: '0.4em', marginTop: 4 }}>
                  AI INTERFACE · v3.0
                </div>
              </div>
              {ITEMS.map(it => {
                const active = page === it.id
                return (
                  <button key={it.id} onClick={() => go(it.id)}
                    style={{
                      display: 'flex', alignItems: 'center', gap: 12, textAlign: 'left',
                      padding: '11px 12px', borderRadius: 5, cursor: 'pointer',
                      background: active ? 'var(--cyan-10)' : 'transparent',
                      border: `1px solid ${active ? 'var(--border-hi)' : 'transparent'}`,
                      transition: 'background .15s, border .15s',
                    }}
                    onMouseEnter={e => { if (!active) e.currentTarget.style.background = 'var(--cyan-5)' }}
                    onMouseLeave={e => { if (!active) e.currentTarget.style.background = 'transparent' }}>
                    <span style={{ fontSize: 18, width: 22, textAlign: 'center',
                      color: active ? 'var(--cyan)' : 'var(--text)',
                      textShadow: active ? 'var(--glow-xs)' : 'none' }}>{it.glyph}</span>
                    <span>
                      <div style={{ fontFamily: 'var(--font-d)', fontSize: 12, fontWeight: 700,
                        letterSpacing: '0.12em',
                        color: active ? 'var(--text-hi)' : 'var(--text)' }}>{it.label}</div>
                      <div style={{ fontFamily: 'var(--font-m)', fontSize: 8.5,
                        color: 'var(--text-lo)', letterSpacing: '0.08em', marginTop: 2 }}>
                        {it.sub}
                      </div>
                    </span>
                    {active && <span style={{ marginLeft: 'auto', width: 6, height: 6,
                      borderRadius: '50%', background: 'var(--cyan)',
                      boxShadow: 'var(--glow-xs)' }} />}
                  </button>
                )
              })}
            </motion.nav>
          </>
        )}
      </AnimatePresence>
    </>
  )
}
