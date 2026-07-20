import { useEffect, useState, useCallback } from 'react'

// Read-only viewer over GET /api/memory — what Cosmos has learned: speech
// corrections, preferences, people/project facts, frequent tasks.

interface MemoryData {
  corrections?: Record<string, string>
  preferences?: Record<string, string>
  people?: Record<string, string>
  projects?: Record<string, string>
  learned_apps?: Record<string, string>
  frequent_tasks?: { task: string; count: number }[]
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ fontFamily: 'var(--font-d)', fontSize: 'var(--fs-cap, 8px)',
        letterSpacing: '0.2em', color: 'rgba(0,212,255,0.55)', marginBottom: 4 }}>
        {title}
      </div>
      {children}
    </div>
  )
}

function KvRows({ data }: { data: Record<string, string> }) {
  const entries = Object.entries(data)
  if (!entries.length) return null
  return (
    <div>
      {entries.map(([k, v]) => (
        <div key={k} style={{ display: 'flex', gap: 8, padding: '3px 0',
          borderBottom: '1px solid rgba(0,212,255,0.05)', alignItems: 'baseline' }}>
          <span style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-label, 9.5px)',
            color: 'var(--text-hi)', flexShrink: 0, maxWidth: '45%',
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{k}</span>
          <span style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-label, 9.5px)',
            color: 'var(--text)', minWidth: 0, lineHeight: 1.5 }}>{v}</span>
        </div>
      ))}
    </div>
  )
}

export default function MemoryPanel() {
  const [mem, setMem] = useState<MemoryData | null>(null)
  const [error, setError] = useState(false)

  const load = useCallback(() => {
    fetch('/api/memory').then(r => r.json()).then(d => { setMem(d); setError(false) })
      .catch(() => setError(true))
  }, [])
  useEffect(() => { load() }, [load])

  const empty = mem && !Object.keys(mem.preferences ?? {}).length &&
    !Object.keys(mem.people ?? {}).length && !Object.keys(mem.projects ?? {}).length &&
    !Object.keys(mem.corrections ?? {}).length && !(mem.frequent_tasks ?? []).length

  return (
    <div style={{ flex: 1, overflowY: 'auto', padding: '10px 12px', minHeight: 0 }}>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 6 }}>
        <button className="hud-btn" onClick={load} style={{ padding: '3px 10px', borderRadius: 2 }}>
          REFRESH
        </button>
      </div>

      {error && (
        <div style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-label, 9.5px)',
          color: 'var(--amber)' }}>Backend unreachable — memory unavailable.</div>
      )}
      {empty && (
        <div style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-label, 9.5px)',
          color: 'var(--text-lo)', textAlign: 'center', marginTop: 24, lineHeight: 1.8 }}>
          Nothing learned yet, sir.<br />
          Say “remember that I prefer…” and it lands here.
        </div>
      )}

      {mem && Object.keys(mem.preferences ?? {}).length > 0 && (
        <Section title="Preferences"><KvRows data={mem.preferences!} /></Section>
      )}
      {mem && Object.keys(mem.people ?? {}).length > 0 && (
        <Section title="People"><KvRows data={mem.people!} /></Section>
      )}
      {mem && Object.keys(mem.projects ?? {}).length > 0 && (
        <Section title="Projects"><KvRows data={mem.projects!} /></Section>
      )}
      {mem && Object.keys(mem.learned_apps ?? {}).length > 0 && (
        <Section title="App quirks"><KvRows data={mem.learned_apps!} /></Section>
      )}
      {mem && Object.keys(mem.corrections ?? {}).length > 0 && (
        <Section title="Speech corrections"><KvRows data={mem.corrections!} /></Section>
      )}
      {mem && (mem.frequent_tasks ?? []).length > 0 && (
        <Section title="Frequent tasks">
          {(mem.frequent_tasks ?? []).slice(0, 10).map(t => (
            <div key={t.task} style={{ display: 'flex', gap: 8, padding: '3px 0',
              borderBottom: '1px solid rgba(0,212,255,0.05)', alignItems: 'baseline' }}>
              <span style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-label, 9.5px)',
                color: 'var(--text)', flex: 1, minWidth: 0, overflow: 'hidden',
                textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.task}</span>
              <span style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-cap, 8px)',
                color: 'var(--text-lo)', flexShrink: 0 }}>×{t.count}</span>
            </div>
          ))}
        </Section>
      )}
    </div>
  )
}
