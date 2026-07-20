import { useEffect, useState, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import PageShell, { OfflineBanner } from './PageShell'
import type { Page } from '../store'

interface Field { key: string; label: string; secret: boolean; optional: boolean; set: boolean }
interface Connector { id: string; label: string; blurb: string; note: string; via: string; configured: boolean; fields: Field[] }
interface Tool { name: string; description: string; gate: string }
interface Server { name: string; state: string; enabled: boolean; trusted: boolean; error: string; info: string; transport: string; dropped?: number; tools: Tool[] }

const btn: React.CSSProperties = {
  fontFamily: 'var(--font-d)', fontSize: 10, fontWeight: 700, letterSpacing: '0.12em',
  padding: '8px 16px', borderRadius: 4, cursor: 'pointer', border: '1px solid var(--border-hi)',
  background: 'var(--cyan-10)', color: 'var(--cyan)',
}
const GATE_UI: Record<string, { color: string; label: string }> = {
  open: { color: 'var(--green)', label: 'read · free' },
  confirm: { color: 'var(--cyan)', label: 'confirms' },
  destructive: { color: 'var(--red)', label: 'destructive' },
}
const STATE_UI: Record<string, { color: string; label: string }> = {
  connected: { color: 'var(--green)', label: 'CONNECTED' },
  error: { color: 'var(--red)', label: 'ERROR' },
  disabled: { color: 'var(--text-lo)', label: 'DISABLED' },
  'not connected': { color: 'var(--amber)', label: 'NOT CONNECTED' },
}

export default function ConnectorsPage({ page, onNavigate }: {
  page?: Page; onNavigate?: (p: Page) => void
}) {
  const [connectors, setConnectors] = useState<Connector[] | null>(null)
  const [servers, setServers] = useState<Server[]>([])
  const [cfgPath, setCfgPath] = useState('')
  const [offline, setOffline] = useState(false)
  const [open, setOpen] = useState<Record<string, boolean>>({})
  const [drafts, setDrafts] = useState<Record<string, Record<string, string>>>({})
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [busy, setBusy] = useState<string | null>(null)
  const [msg, setMsg] = useState<Record<string, { text: string; ok: boolean }>>({})

  const load = useCallback(() => {
    Promise.all([
      fetch('/api/connectors').then(r => r.ok ? r.json() : Promise.reject()),
      fetch('/api/mcp').then(r => r.ok ? r.json() : Promise.reject()),
    ]).then(([c, m]) => {
      setConnectors(c.connectors || []); setServers(m.servers || [])
      setCfgPath(m.config_path || ''); setOffline(false)
    }).catch(() => setOffline(true))
  }, [])
  useEffect(() => { load() }, [load])

  const setField = (cid: string, key: string, val: string) =>
    setDrafts(d => ({ ...d, [cid]: { ...(d[cid] || {}), [key]: val } }))

  const saveConnector = async (c: Connector) => {
    setBusy(c.id); setMsg(m => ({ ...m, [c.id]: { text: 'Saving…', ok: true } }))
    const r = await fetch(`/api/connectors/${c.id}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ values: drafts[c.id] || {} }),
    }).then(x => x.json()).catch(() => ({ ok: false, message: 'Network error' }))
    setBusy(null)
    setMsg(m => ({ ...m, [c.id]: { text: r.message, ok: !!r.ok } }))
    if (r.connectors) setConnectors(r.connectors)
    if (r.ok) setDrafts(d => ({ ...d, [c.id]: {} }))
  }

  const reloadMcp = async () => {
    setBusy('mcp'); setMsg(m => ({ ...m, mcp: { text: 'Reconnecting…', ok: true } }))
    const r = await fetch('/api/mcp/reload', { method: 'POST' })
      .then(x => x.json()).catch(() => ({ ok: false, message: 'Network error' }))
    setBusy(null)
    setMsg(m => ({ ...m, mcp: { text: r.message, ok: !!r.ok } }))
    if (r.servers) setServers(r.servers); else load()
  }

  const configured = connectors?.filter(c => c.configured).length ?? 0

  return (
    <PageShell title="Connectors" page={page} onNavigate={onNavigate}
      subtitle={connectors ? `${configured}/${connectors.length} integrations configured` : 'loading…'}>
      {offline && <div style={{ marginBottom: 20 }}><OfflineBanner onRetry={load} /></div>}

      {/* ── Native integrations ── */}
      <SectionTitle n="01" title="Integrations" hint="Credentials live in backend/.env — never shown here, only whether they’re set." />
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))', gap: 12, marginBottom: 40 }}>
        {(connectors || []).map((c, i) => {
          const isOpen = !!open[c.id]
          const m = msg[c.id]
          return (
            <motion.div key={c.id} initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }}
              transition={{ delay: i * 0.04 }} layout className="v2-card"
              style={{ padding: 18,
                borderColor: c.configured ? 'var(--border-hi)' : undefined }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer' }}
                onClick={() => setOpen(o => ({ ...o, [c.id]: !isOpen }))}>
                <span style={{ width: 9, height: 9, borderRadius: '50%', flexShrink: 0,
                  background: c.configured ? 'var(--green)' : 'var(--text-lo)',
                  boxShadow: c.configured ? '0 0 8px var(--green)' : 'none' }} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontFamily: 'var(--font-d)', fontSize: 13, fontWeight: 700,
                    color: 'var(--text-hi)' }}>{c.label}</div>
                  <div style={{ fontFamily: 'var(--font-b)', fontSize: 11.5, color: 'var(--text)', marginTop: 3 }}>
                    {c.blurb}</div>
                </div>
                <span style={{ fontFamily: 'var(--font-m)', fontSize: 9,
                  color: c.configured ? 'var(--green)' : 'var(--amber)', letterSpacing: '0.1em' }}>
                  {c.configured ? 'SET' : 'NOT SET'}</span>
                <motion.span animate={{ rotate: isOpen ? 90 : 0 }} style={{ color: 'var(--cyan)' }}>›</motion.span>
              </div>

              <AnimatePresence>
                {isOpen && (
                  <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }}
                    exit={{ height: 0, opacity: 0 }} style={{ overflow: 'hidden' }}>
                    <div style={{ paddingTop: 14, display: 'flex', flexDirection: 'column', gap: 10 }}>
                      {c.note && <div style={{ fontFamily: 'var(--font-b)', fontSize: 11,
                        color: 'var(--text-lo)', lineHeight: 1.5 }}>{c.note}</div>}
                      {c.fields.map(f => (
                        <label key={f.key} style={{ display: 'block' }}>
                          <div style={{ fontFamily: 'var(--font-m)', fontSize: 9, letterSpacing: '0.1em',
                            color: 'var(--text)', marginBottom: 4 }}>
                            {f.label}{f.optional ? '  (optional)' : ''}
                            {f.set && <span style={{ color: 'var(--green)', marginLeft: 6 }}>● set</span>}
                          </div>
                          <input type={f.secret ? 'password' : 'text'} autoComplete="off"
                            value={(drafts[c.id] || {})[f.key] || ''}
                            onChange={e => setField(c.id, f.key, e.target.value)}
                            placeholder={f.set ? '•••••••• (leave blank to keep)' : 'not set'}
                            style={{ width: '100%', boxSizing: 'border-box', background: 'var(--bg-deep)',
                              border: '1px solid var(--border)', borderRadius: 5, padding: '8px 10px',
                              color: 'var(--text-hi)', fontFamily: 'var(--font-m)', fontSize: 12, outline: 'none' }} />
                        </label>
                      ))}
                      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 4 }}>
                        <button style={{ ...btn, opacity: busy === c.id ? 0.5 : 1 }} disabled={busy === c.id}
                          onClick={() => saveConnector(c)}>{busy === c.id ? 'Saving…' : 'Save'}</button>
                        {m && <span style={{ fontFamily: 'var(--font-b)', fontSize: 11,
                          color: m.ok ? 'var(--green)' : 'var(--red)' }}>{m.text}</span>}
                      </div>
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </motion.div>
          )
        })}
      </div>

      {/* ── MCP servers ── */}
      <SectionTitle n="02" title="MCP servers" hint="External tool servers connected via the Model Context Protocol."
        right={<button style={{ ...btn, opacity: busy === 'mcp' ? 0.5 : 1 }} disabled={busy === 'mcp'}
          onClick={reloadMcp}>{busy === 'mcp' ? 'Reconnecting…' : '↻ Reconnect all'}</button>} />
      {msg.mcp && <div style={{ fontFamily: 'var(--font-b)', fontSize: 12, marginBottom: 12,
        color: msg.mcp.ok ? 'var(--green)' : 'var(--red)' }}>{msg.mcp.text}</div>}
      {servers.length === 0 && !offline && (
        <div style={{ fontFamily: 'var(--font-b)', fontSize: 12, color: 'var(--text)',
          border: '1px solid var(--border)', borderRadius: 8, padding: '12px 16px', marginBottom: 12 }}>
          No MCP servers configured. Add them to <code style={mono}>{cfgPath || '~/.friday/mcp.json'}</code>,
          put tokens in the integrations above, then Reconnect.
        </div>
      )}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        {servers.map((s, i) => {
          const ui = STATE_UI[s.state] || STATE_UI['not connected']
          const isOpen = !!expanded[s.name]
          return (
            <motion.div key={s.name} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}
              transition={{ delay: i * 0.04 }} className="v2-card">
              <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '14px 16px' }}>
                <span style={{ width: 9, height: 9, borderRadius: '50%', background: ui.color,
                  boxShadow: `0 0 8px ${ui.color}`, flexShrink: 0 }} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span style={{ fontFamily: 'var(--font-d)', fontSize: 14, fontWeight: 700, color: 'var(--text-hi)' }}>{s.name}</span>
                    {s.trusted && <span style={tag('var(--cyan)')}>TRUSTED</span>}
                    <span style={{ fontFamily: 'var(--font-m)', fontSize: 8, color: 'var(--text-lo)', letterSpacing: '0.1em' }}>{s.transport}</span>
                  </div>
                  <div style={{ fontFamily: 'var(--font-m)', fontSize: 9, color: ui.color, letterSpacing: '0.12em', marginTop: 3 }}>
                    {ui.label}{s.info ? ` · ${s.info}` : ''}{s.state === 'connected' ? ` · ${s.tools.length} tools` : ''}
                  </div>
                </div>
                {s.tools.length > 0 && (
                  <button onClick={() => setExpanded(e => ({ ...e, [s.name]: !isOpen }))}
                    style={{ ...btn, background: 'transparent', color: 'var(--text)', padding: '6px 12px' }}>
                    {isOpen ? 'Hide tools' : `Tools (${s.tools.length})`}
                  </button>
                )}
              </div>
              {s.error && <div style={{ padding: '0 16px 14px' }}>
                <div style={{ fontFamily: 'var(--font-b)', fontSize: 11.5, color: 'var(--red)',
                  background: 'rgba(255,34,68,0.06)', border: '1px solid var(--red)', borderRadius: 6, padding: '8px 12px' }}>
                  {/401|unauthor/i.test(s.error) ? 'Auth failed (401). Fix the token above, then Reconnect. ' : ''}{s.error}
                </div></div>}
              <AnimatePresence>
                {isOpen && (
                  <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }} exit={{ height: 0, opacity: 0 }} style={{ overflow: 'hidden' }}>
                    <div style={{ borderTop: '1px solid var(--border)', padding: '10px 16px 14px', display: 'flex', flexDirection: 'column', gap: 6 }}>
                      {s.tools.map(t => {
                        const g = GATE_UI[t.gate] || GATE_UI.confirm
                        return (
                          <div key={t.name} style={{ display: 'flex', gap: 10, alignItems: 'baseline' }}>
                            <span style={{ fontFamily: 'var(--font-m)', fontSize: 11.5, color: 'var(--cyan)', minWidth: 150 }}>{t.name}</span>
                            <span style={{ fontFamily: 'var(--font-m)', fontSize: 8, color: g.color, letterSpacing: '0.08em', minWidth: 78 }}>{g.label}</span>
                            <span style={{ fontFamily: 'var(--font-b)', fontSize: 11.5, color: 'var(--text)', flex: 1 }}>{t.description}</span>
                          </div>
                        )
                      })}
                      {!!s.dropped && <div style={{ fontFamily: 'var(--font-m)', fontSize: 9, color: 'var(--text-lo)' }}>+{s.dropped} more hidden by the per-server cap.</div>}
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </motion.div>
          )
        })}
      </div>
    </PageShell>
  )
}

function SectionTitle({ n, title, hint, right }: { n: string; title: string; hint: string; right?: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', alignItems: 'flex-end', gap: 12, marginBottom: 14 }}>
      <div style={{ flex: 1 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
          <span style={{ fontFamily: 'var(--font-d)', fontSize: 10, color: 'var(--cyan)', letterSpacing: '0.3em', opacity: 0.6 }}>{n}</span>
          <h2 style={{ fontFamily: 'var(--font-d)', fontSize: 16, fontWeight: 800, letterSpacing: '0.1em', color: 'var(--text-hi)', margin: 0 }}>{title}</h2>
        </div>
        <div style={{ fontFamily: 'var(--font-b)', fontSize: 11.5, color: 'var(--text-lo)', marginLeft: 32, marginTop: 3 }}>{hint}</div>
      </div>
      {right}
    </div>
  )
}

const mono: React.CSSProperties = { fontFamily: 'var(--font-m)', fontSize: 11, color: 'var(--cyan)' }
function tag(color: string): React.CSSProperties {
  return { fontFamily: 'var(--font-m)', fontSize: 7.5, letterSpacing: '0.14em', color, border: `1px solid ${color}`, borderRadius: 3, padding: '2px 5px', opacity: 0.8 }
}
