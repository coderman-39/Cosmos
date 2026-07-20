import { useEffect, useState, useCallback } from 'react'
import { motion } from 'framer-motion'
import PageShell, { OfflineBanner } from './PageShell'
import Markdown from './Markdown'
import type { Page } from '../store'

interface SkillMeta { name: string; title: string; protected: boolean; chars: number; preview: string }
interface SkillFull { name: string; title: string; content: string; protected: boolean }

const btn: React.CSSProperties = {
  fontFamily: 'var(--font-d)', fontSize: 10, fontWeight: 700, letterSpacing: '0.12em',
  padding: '8px 16px', borderRadius: 4, cursor: 'pointer', border: '1px solid var(--border-hi)',
  background: 'var(--cyan-10)', color: 'var(--cyan)',
}
const btnGhost: React.CSSProperties = { ...btn, background: 'transparent', color: 'var(--text)' }

export default function SkillsPage({ page, onNavigate }: {
  page?: Page; onNavigate?: (p: Page) => void
}) {
  const [list, setList] = useState<SkillMeta[]>([])
  const [sel, setSel] = useState<SkillFull | null>(null)
  const [content, setContent] = useState('')
  const [dirty, setDirty] = useState(false)
  const [preview, setPreview] = useState(false)
  const [aiPrompt, setAiPrompt] = useState('')
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState<{ msg: string; ok: boolean } | null>(null)
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [offline, setOffline] = useState(false)

  const loadList = useCallback(() => {
    fetch('/api/skills').then(r => r.ok ? r.json() : Promise.reject())
      .then(d => { setList(d.skills || []); setOffline(false) })
      .catch(() => setOffline(true))
  }, [])
  useEffect(() => { loadList() }, [loadList])

  const open = async (name: string) => {
    setStatus(null); setPreview(false); setAiPrompt('')
    const r = await fetch(`/api/skills/${name}`)
    if (!r.ok) { setStatus({ msg: 'Could not load skill', ok: false }); return }
    const s: SkillFull = await r.json()
    setSel(s); setContent(s.content); setDirty(false)
  }

  const startNew = () => { setCreating(true); setNewName('') }
  const confirmNew = () => {
    const n = newName.trim().toLowerCase()
    if (!/^[a-z0-9][a-z0-9-]{1,40}$/.test(n)) {
      setStatus({ msg: 'Name must be kebab-case (a-z, 0-9, dashes).', ok: false }); return
    }
    setCreating(false)
    setSel({ name: n, title: n, content: '', protected: false })
    setContent(`# ${n.replace(/-/g, ' ')}\n\n`); setDirty(true); setPreview(false); setStatus(null)
  }

  const save = async () => {
    if (!sel) return
    setBusy(true); setStatus(null)
    const r = await fetch(`/api/skills/${sel.name}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    }).then(x => x.json()).catch(() => ({ ok: false, message: 'Network error' }))
    setBusy(false)
    setStatus({ msg: r.message || (r.ok ? 'Saved.' : 'Failed.'), ok: !!r.ok })
    if (r.ok) { setDirty(false); loadList() }
  }

  const aiEdit = async () => {
    if (!sel || !aiPrompt.trim()) return
    setBusy(true); setStatus({ msg: 'Asking the model to revise…', ok: true })
    const r = await fetch(`/api/skills/${sel.name}/edit`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt: aiPrompt }),
    }).then(x => x.json()).catch(() => ({ ok: false, error: 'Network error' }))
    setBusy(false)
    if (r.ok && r.content) {
      setContent(r.content); setDirty(true); setAiPrompt(''); setPreview(true)
      setStatus({ msg: 'Proposed changes below — review, then Save to apply.', ok: true })
    } else {
      setStatus({ msg: r.error || 'Edit failed.', ok: false })
    }
  }

  const del = async () => {
    if (!sel || !confirm(`Delete skill "${sel.name}"? This removes it from Cosmos's instructions.`)) return
    const r = await fetch(`/api/skills/${sel.name}`, { method: 'DELETE' })
      .then(x => x.json()).catch(() => ({ ok: false, message: 'Network error' }))
    if (r.ok) { setSel(null); loadList() }
    else setStatus({ msg: r.message, ok: false })
  }

  const delFromList = async (name: string) => {
    if (!confirm(`Delete skill "${name}"?`)) return
    const r = await fetch(`/api/skills/${name}`, { method: 'DELETE' })
      .then(x => x.json()).catch(() => ({ ok: false, message: 'Network error' }))
    if (r.ok) loadList()
    else setStatus({ msg: r.message, ok: false })
  }

  // ── Detail / editor view ──────────────────────────────────────────────────
  if (sel) {
    return (
      <PageShell title={sel.title || sel.name} page={page} onNavigate={onNavigate}
        subtitle={`skills/${sel.name}.md${sel.protected ? '  ·  BUILT-IN' : ''}`}
        right={<button style={btnGhost} onClick={() => setSel(null)}>← ALL SKILLS</button>}>
        {sel.protected && (
          <div style={noteBox('var(--amber)')}>
            Built-in skill. You can edit it, but a future update may overwrite your changes.
          </div>
        )}

        <div style={{ display: 'flex', gap: 8, marginBottom: 10 }}>
          <button style={preview ? btnGhost : btn} onClick={() => setPreview(false)}>EDIT</button>
          <button style={preview ? btn : btnGhost} onClick={() => setPreview(true)}>PREVIEW</button>
          <div style={{ flex: 1 }} />
          {!sel.protected && <button style={{ ...btnGhost, color: 'var(--red)',
            borderColor: 'rgba(255,34,68,0.4)' }} onClick={del}>DELETE</button>}
          <button style={{ ...btn, opacity: dirty ? 1 : 0.5 }} disabled={!dirty || busy}
            onClick={save}>{busy ? 'SAVING…' : dirty ? 'SAVE' : 'SAVED'}</button>
        </div>

        {preview ? (
          <div style={{ background: 'var(--bg-card)', border: '1px solid var(--border)',
            borderRadius: 8, padding: 18, minHeight: 240,
            fontFamily: 'var(--font-b)', fontSize: 13, color: 'var(--text-hi)' }}>
            <Markdown text={content} />
          </div>
        ) : (
          <textarea value={content} onChange={e => { setContent(e.target.value); setDirty(true) }}
            spellCheck={false}
            style={{ width: '100%', minHeight: 320, resize: 'vertical', boxSizing: 'border-box',
              background: 'var(--bg-deep)', border: '1px solid var(--border)', borderRadius: 8,
              padding: 16, color: 'var(--text-hi)', fontFamily: 'var(--font-m)', fontSize: 12.5,
              lineHeight: 1.6, outline: 'none' }} />
        )}

        {/* AI edit box */}
        <div style={{ marginTop: 18, background: 'var(--bg-card)',
          border: '1px solid var(--border-hi)', borderRadius: 8, padding: 16 }}>
          <div style={{ fontFamily: 'var(--font-d)', fontSize: 10, fontWeight: 700,
            letterSpacing: '0.16em', color: 'var(--cyan)', marginBottom: 10 }}>
            ✦ EDIT WITH AI
          </div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
            <textarea value={aiPrompt} onChange={e => setAiPrompt(e.target.value)}
              placeholder="Describe the change — e.g. “add a step to notify #it after wiping a device”, or “tighten the wording and add trigger phrases”."
              onKeyDown={e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) aiEdit() }}
              style={{ flex: 1, minHeight: 56, resize: 'vertical', boxSizing: 'border-box',
                background: 'var(--bg-deep)', border: '1px solid var(--border)', borderRadius: 5,
                padding: '10px 12px', color: 'var(--text-hi)', fontFamily: 'var(--font-b)',
                fontSize: 13, outline: 'none' }} />
            <button style={{ ...btn, opacity: aiPrompt.trim() && !busy ? 1 : 0.5, whiteSpace: 'nowrap' }}
              disabled={!aiPrompt.trim() || busy} onClick={aiEdit}>
              {busy ? '…' : 'PROPOSE'}
            </button>
          </div>
          <div style={{ fontFamily: 'var(--font-m)', fontSize: 8.5, color: 'var(--text-lo)',
            marginTop: 8, letterSpacing: '0.08em' }}>
            The model rewrites the file; you review the result above and click Save to apply. ⌘⏎ to propose.
          </div>
        </div>

        {status && <div style={{ ...noteBox(status.ok ? 'var(--cyan)' : 'var(--red)'), marginTop: 14 }}>
          {status.msg}</div>}
      </PageShell>
    )
  }

  // ── List view ───────────────────────────────────────────────────────────────
  return (
    <PageShell title="Skills" page={page} onNavigate={onNavigate}
      subtitle={`${list.length} playbooks in Cosmos's instructions`}
      right={<button style={btn} onClick={startNew}>+ NEW SKILL</button>}>
      {offline && <div style={{ marginBottom: 16 }}><OfflineBanner onRetry={loadList} /></div>}
      {creating && (
        <div style={{ ...noteBox('var(--cyan)'), display: 'flex', gap: 8, alignItems: 'center' }}>
          <input autoFocus value={newName} onChange={e => setNewName(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') confirmNew(); if (e.key === 'Escape') setCreating(false) }}
            placeholder="kebab-case-name"
            style={{ flex: 1, background: 'var(--bg-deep)', border: '1px solid var(--border)',
              borderRadius: 4, padding: '8px 10px', color: 'var(--text-hi)',
              fontFamily: 'var(--font-m)', fontSize: 13, outline: 'none' }} />
          <button style={btn} onClick={confirmNew}>CREATE</button>
          <button style={btnGhost} onClick={() => setCreating(false)}>CANCEL</button>
        </div>
      )}
      {status && !creating && <div style={noteBox(status.ok ? 'var(--cyan)' : 'var(--red)')}>{status.msg}</div>}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: 16 }}>
        {list.map((s, i) => (
          <motion.div key={s.name} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}
            transition={{ delay: i * 0.03 }} whileHover={{ y: -4 }} onClick={() => open(s.name)}
            className="skill-card v2-card v2-glow"
            style={{ textAlign: 'left', padding: 20, cursor: 'pointer' }}>
            <div style={{ position: 'relative', zIndex: 1, display: 'flex', alignItems: 'center', gap: 9, marginBottom: 8 }}>
              <span style={{ color: 'var(--cyan)', fontSize: 14, textShadow: 'var(--glow-xs)', lineHeight: 1 }}>▤</span>
              <span style={{ fontFamily: 'var(--font-d)', fontSize: 13, fontWeight: 700,
                color: 'var(--text-hi)', flex: 1 }}>{s.title}</span>
              {s.protected
                ? <span style={tag}>BUILT-IN</span>
                : <button title="Delete skill"
                    onClick={e => { e.stopPropagation(); delFromList(s.name) }}
                    style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0,
                      color: 'var(--text-lo)', fontSize: 14, lineHeight: 1 }}
                    onMouseEnter={e => (e.currentTarget.style.color = 'var(--red)')}
                    onMouseLeave={e => (e.currentTarget.style.color = 'var(--text-lo)')}>✕</button>}
            </div>
            <div style={{ position: 'relative', zIndex: 1, fontFamily: 'var(--font-b)', fontSize: 12, lineHeight: 1.5,
              color: 'var(--text)', marginBottom: 10, minHeight: 36 }}>{s.preview}…</div>
            <div style={{ position: 'relative', zIndex: 1, fontFamily: 'var(--font-m)', fontSize: 8.5, color: 'var(--text-lo)',
              letterSpacing: '0.08em' }}>{s.name}.md · {s.chars.toLocaleString()} chars</div>
          </motion.div>
        ))}
      </div>
    </PageShell>
  )
}

const tag: React.CSSProperties = {
  fontFamily: 'var(--font-m)', fontSize: 7.5, letterSpacing: '0.14em', color: 'var(--amber)',
  border: '1px solid rgba(255,149,0,0.4)', borderRadius: 3, padding: '2px 5px',
}
function noteBox(color: string): React.CSSProperties {
  return { fontFamily: 'var(--font-b)', fontSize: 12, color, background: 'rgba(0,212,255,0.05)',
    border: `1px solid ${color === 'var(--cyan)' ? 'var(--border)' : color}`, borderRadius: 6,
    padding: '10px 14px', marginBottom: 14 }
}
