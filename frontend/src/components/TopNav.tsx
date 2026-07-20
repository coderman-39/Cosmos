import type { Page } from '../store'

// ── Home-v2 style fixed top nav: diamond logo + wordmark, horizontal section
// links with an active state. Every page must be reachable from here — keep
// this list in sync with NavMenu/HomePage when a new page ships. ──
const LINKS: { id: Page; label: string }[] = [
  { id: 'home', label: 'Home' },
  { id: 'agent', label: 'Agent' },
  { id: 'nexus', label: 'Nexus' },
  { id: 'dossier', label: 'Dossier' },
  { id: 'vision', label: 'Vision' },
  { id: 'kinesis', label: 'Kinesis' },
  { id: 'slack', label: 'Slack' },
  { id: 'panel', label: 'Panel' },
  { id: 'skills', label: 'Skills' },
  { id: 'mutate', label: 'Mutate' },
  { id: 'mcps', label: 'Connectors' },
]

export default function TopNav({ page, onNavigate }: {
  page: Page; onNavigate: (p: Page) => void
}) {
  return (
    <nav className="v2-nav">
      <button onClick={() => onNavigate('home')}
        style={{ display: 'flex', alignItems: 'center', gap: 13,
          background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}>
        <span className="v2-nav-logo" />
        <span style={{ fontFamily: 'var(--font-d)', fontWeight: 800, letterSpacing: '0.42em',
          fontSize: 15, color: 'var(--text-hi)', textShadow: '0 0 18px rgba(0,212,255,0.55)',
          paddingLeft: '0.42em' }}>COSMOS</span>
      </button>

      <div style={{ display: 'flex', alignItems: 'center', gap: 22 }}>
        {LINKS.map(l => (
          <button key={l.id} onClick={() => onNavigate(l.id)}
            className={`v2-nav-link${page === l.id ? ' active' : ''}`}>
            {l.label}
          </button>
        ))}
      </div>
    </nav>
  )
}
