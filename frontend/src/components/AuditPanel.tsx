import { useEffect, useState, useCallback } from 'react'

// Security audit trail viewer — GET /api/audit (append-only ~/.friday/audit.jsonl).
// Every executed tool, which ones tripped the risk gate, and what the user decided.

interface AuditEntry {
  ts: string
  tool: string
  ok: boolean
  summary: string
  danger?: string
  confirmed?: boolean
}

export default function AuditPanel() {
  const [entries, setEntries] = useState<AuditEntry[]>([])
  const [error, setError] = useState(false)

  const load = useCallback(() => {
    fetch('/api/audit?limit=120').then(r => r.json())
      .then(d => { setEntries(d.entries ?? []); setError(false) })
      .catch(() => setError(true))
  }, [])
  useEffect(() => { load() }, [load])

  return (
    <div style={{ flex: 1, overflowY: 'auto', padding: '10px 12px', minHeight: 0 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        marginBottom: 6 }}>
        <span style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-cap, 8px)',
          color: 'var(--text-lo)', letterSpacing: '0.12em' }}>
          NEWEST FIRST · GATED ACTIONS FLAGGED
        </span>
        <button className="hud-btn" onClick={load} style={{ padding: '3px 10px', borderRadius: 2 }}>
          REFRESH
        </button>
      </div>

      {error && (
        <div style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-label, 9.5px)',
          color: 'var(--amber)' }}>Backend unreachable — audit unavailable.</div>
      )}
      {!error && entries.length === 0 && (
        <div style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-label, 9.5px)',
          color: 'var(--text-lo)', textAlign: 'center', marginTop: 24 }}>
          No audited actions yet.
        </div>
      )}

      {entries.map((e, i) => (
        <div key={`${e.ts}-${i}`} style={{ padding: '5px 8px', marginBottom: 3, borderRadius: 2,
          background: e.danger ? 'rgba(255,149,0,0.04)' : 'rgba(0,212,255,0.03)',
          border: `1px solid ${e.danger ? 'rgba(255,149,0,0.18)' : 'rgba(0,212,255,0.07)'}`,
          borderLeft: `2px solid ${!e.ok ? 'rgba(255,34,68,0.5)'
            : e.danger ? 'rgba(255,149,0,0.55)' : 'rgba(0,255,136,0.35)'}` }}>
          <div style={{ display: 'flex', gap: 8, alignItems: 'baseline' }}>
            <span style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-cap, 8px)',
              color: 'var(--text-lo)', flexShrink: 0 }}>
              {e.ts?.slice(5, 16).replace('T', ' ')}
            </span>
            <span style={{ fontFamily: 'var(--font-d)', fontSize: 'var(--fs-cap, 8px)',
              letterSpacing: '0.1em', color: e.ok ? 'var(--cyan)' : 'var(--red)', flexShrink: 0 }}>
              {e.tool}
            </span>
            {e.danger && (
              <span style={{ fontFamily: 'var(--font-d)', fontSize: 'var(--fs-cap, 7.5px)',
                letterSpacing: '0.1em', color: 'var(--amber)',
                border: '1px solid rgba(255,149,0,0.4)', borderRadius: 2,
                padding: '0 4px', flexShrink: 0 }}>
                {e.confirmed === false ? 'DECLINED' : 'APPROVED'}
              </span>
            )}
          </div>
          <div style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-label, 9px)',
            color: 'var(--text)', marginTop: 2, lineHeight: 1.5,
            wordBreak: 'break-word' }}>
            {e.summary}
          </div>
          {e.danger && (
            <div style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-cap, 8px)',
              color: 'rgba(255,149,0,0.7)', marginTop: 2 }}>
              ⚠ {e.danger}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}
