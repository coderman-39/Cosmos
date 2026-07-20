import { ReactNode } from 'react'
import { motion } from 'framer-motion'
import type { Page } from '../store'
import TopNav from './TopNav'

// Consistent immersive backdrop + scrollable content column for the non-agent
// pages (Skills / Connectors), carrying the Home-v2 chrome: fixed top nav,
// drifting aurora, vignettes and a moving scanline overlay.
export default function PageShell({ title, subtitle, right, children, page, onNavigate }: {
  title: string; subtitle?: string; right?: ReactNode; children: ReactNode
  page?: Page; onNavigate?: (p: Page) => void
}) {
  return (
    <div style={{ position: 'fixed', inset: 0, background: 'var(--bg)', overflow: 'hidden' }}>
      {/* Immersive background stack */}
      <div className="v2-aurora" />
      <div className="v2-vignette" />
      <div className="hex-grid" style={{ zIndex: 0 }} />
      <div className="v2-scan" />
      <div className="v2-vignette-inner" />

      {page && onNavigate && <TopNav page={page} onNavigate={onNavigate} />}

      <div style={{ position: 'absolute', inset: 0, zIndex: 3, overflowY: 'auto' }}>
        <div style={{ maxWidth: 1080, margin: '0 auto', padding: '112px 30px 72px' }}>
          <motion.header initial={{ opacity: 0, y: -12 }} animate={{ opacity: 1, y: 0 }}
            transition={{ type: 'spring', stiffness: 260, damping: 26 }}
            style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between',
              gap: 16, marginBottom: 30, paddingBottom: 20,
              borderBottom: '1px solid var(--border)' }}>
            <div>
              <div style={{ fontFamily: 'var(--font-m)', fontSize: 12, letterSpacing: '0.4em',
                textTransform: 'uppercase', color: 'var(--cyan)', marginBottom: 12 }}>
                ◈ {title}
              </div>
              <h1 style={{ fontFamily: 'var(--font-d)', fontSize: 44, fontWeight: 800,
                lineHeight: 1, letterSpacing: '0.01em', color: 'var(--text-hi)', margin: 0,
                textShadow: '0 0 30px rgba(0,150,210,0.35)' }}>{title}</h1>
              {subtitle && (
                <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.15 }}
                  style={{ fontFamily: 'var(--font-m)', fontSize: 11,
                    color: 'var(--text-lo)', letterSpacing: '0.16em', marginTop: 12 }}>
                  {subtitle}
                </motion.div>
              )}
            </div>
            {right}
          </motion.header>
          {children}
        </div>
      </div>
    </div>
  )
}

// Shared loud "backend offline" banner for pages that need the API — the most
// common cause is the backend running old code (restart to load new routes).
export function OfflineBanner({ onRetry }: { onRetry: () => void }) {
  return (
    <motion.div initial={{ opacity: 0, scale: 0.98 }} animate={{ opacity: 1, scale: 1 }}
      style={{ background: 'rgba(255,149,0,0.06)', border: '1px solid rgba(255,149,0,0.4)',
        borderRadius: 8, padding: '18px 20px', display: 'flex', alignItems: 'center', gap: 16 }}>
      <span style={{ fontSize: 22 }}>⚠</span>
      <div style={{ flex: 1 }}>
        <div style={{ fontFamily: 'var(--font-d)', fontSize: 12, fontWeight: 700,
          letterSpacing: '0.1em', color: 'var(--amber)' }}>BACKEND NOT REACHABLE</div>
        <div style={{ fontFamily: 'var(--font-b)', fontSize: 12.5, color: 'var(--text)', marginTop: 6 }}>
          This page needs the backend’s new API routes. If Cosmos’s backend is running an
          older build, <b>restart it</b> (these routes were just added), then retry.
        </div>
      </div>
      <button onClick={onRetry} style={{ fontFamily: 'var(--font-d)', fontSize: 10, fontWeight: 700,
        letterSpacing: '0.12em', padding: '8px 16px', borderRadius: 4, cursor: 'pointer',
        border: '1px solid var(--border-hi)', background: 'var(--cyan-10)', color: 'var(--cyan)' }}>
        ↻ Retry
      </button>
    </motion.div>
  )
}
