import { ReactNode, useRef } from 'react'

// Generic tabbed HUD panel — WAI-ARIA tablist with arrow-key navigation,
// styled on the existing .hud-panel ornaments. Tabs replace the hud-title bar.

export interface HudTab {
  id: string
  label: string
  badge?: string | number
  content: ReactNode
}

interface Props {
  tabs: HudTab[]
  active: string
  onChange: (id: string) => void
  ariaLabel: string
}

export default function HudTabs({ tabs, active, onChange, ariaLabel }: Props) {
  const stripRef = useRef<HTMLDivElement>(null)
  const activeTab = tabs.find(t => t.id === active) ?? tabs[0]

  const onKeyDown = (e: React.KeyboardEvent) => {
    const idx = tabs.findIndex(t => t.id === activeTab.id)
    let next = -1
    if (e.key === 'ArrowRight') next = (idx + 1) % tabs.length
    else if (e.key === 'ArrowLeft') next = (idx - 1 + tabs.length) % tabs.length
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = tabs.length - 1
    if (next >= 0) {
      e.preventDefault()
      onChange(tabs[next].id)
      const btn = stripRef.current?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]
      btn?.focus()
    }
  }

  return (
    <div className="hud-panel" style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <div ref={stripRef} role="tablist" aria-label={ariaLabel} onKeyDown={onKeyDown}
        style={{ display: 'flex', borderBottom: '1px solid var(--border)',
          position: 'relative', zIndex: 2, flexShrink: 0 }}>
        {tabs.map(tab => {
          const isActive = tab.id === activeTab.id
          return (
            <button key={tab.id}
              role="tab"
              id={`tab-${ariaLabel}-${tab.id}`}
              aria-selected={isActive}
              aria-controls={`panel-${ariaLabel}-${tab.id}`}
              tabIndex={isActive ? 0 : -1}
              onClick={() => onChange(tab.id)}
              className="hud-tab"
              style={{
                flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
                gap: 6, padding: '9px 6px 7px',
                fontFamily: 'var(--font-d)', fontSize: 'var(--fs-cap, 8.5px)', fontWeight: 700,
                letterSpacing: '0.18em', textTransform: 'uppercase',
                background: isActive ? 'rgba(0,212,255,0.07)' : 'transparent',
                border: 'none', cursor: 'pointer',
                color: isActive ? 'var(--cyan)' : 'var(--text)',
                borderBottom: isActive ? '2px solid var(--cyan)' : '2px solid transparent',
                transition: 'color 0.15s, background 0.15s',
              }}>
              {tab.label}
              {tab.badge !== undefined && tab.badge !== 0 && tab.badge !== '' && (
                <span style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-cap, 8px)',
                  color: isActive ? 'var(--cyan)' : 'var(--text-lo)',
                  border: '1px solid currentColor', borderRadius: 2,
                  padding: '0 4px', lineHeight: '13px' }}>
                  {tab.badge}
                </span>
              )}
            </button>
          )
        })}
      </div>

      <div role="tabpanel"
        id={`panel-${ariaLabel}-${activeTab.id}`}
        aria-labelledby={`tab-${ariaLabel}-${activeTab.id}`}
        style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
        {activeTab.content}
      </div>
    </div>
  )
}
