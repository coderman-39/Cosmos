import { useEffect, useMemo, useRef, useState } from 'react'
import { useCosmosStore } from '../store'

// Cmd+K command palette — fuzzy subsequence match over static actions,
// conversations, and the user's actual frequent tasks (from /api/memory).
// No dependencies; focus-trapped dialog on the existing HUD styling.

export interface PaletteAction {
  id: string
  label: string
  hint?: string
  run: () => void
}

interface Props {
  open: boolean
  onClose: () => void
  onCommand: (text: string) => void
  onNewChat: () => void
  onStop: () => void
  onToggleMode: () => void
  onSwitchConversation: (id: string) => void
}

// Subsequence fuzzy match: every query char must appear in order.
function fuzzy(query: string, target: string): boolean {
  const q = query.toLowerCase(), t = target.toLowerCase()
  let i = 0
  for (const ch of t) {
    if (ch === q[i]) i++
    if (i === q.length) return true
  }
  return q.length === 0
}

export default function CommandPalette({
  open, onClose, onCommand, onNewChat, onStop, onToggleMode, onSwitchConversation,
}: Props) {
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState(0)
  const [frequent, setFrequent] = useState<string[]>([])
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    setQuery('')
    setSelected(0)
    setTimeout(() => inputRef.current?.focus(), 60)
    // The user's real habits become palette entries.
    fetch('/api/memory').then(r => r.json())
      .then(d => setFrequent((d.frequent_tasks ?? []).slice(0, 8).map((t: any) => t.task)))
      .catch(() => setFrequent([]))
  }, [open])

  const actions: PaletteAction[] = useMemo(() => {
    const st = useCosmosStore.getState()
    const base: PaletteAction[] = [
      { id: 'new-chat', label: 'New conversation', hint: '⌘N',
        run: () => { onNewChat() } },
      { id: 'stop', label: 'Stop current task', hint: 'ESC',
        run: () => { onStop() } },
      { id: 'toggle-mode',
        label: st.permissionMode === 'full' ? 'Switch to GUARDED mode' : 'Switch to FULL ACCESS mode',
        run: () => { onToggleMode() } },
      { id: 'tab-settings', label: 'Open Settings tab',
        run: () => { st.setLeftTab('settings') } },
      { id: 'tab-memory', label: 'Open Memory tab',
        run: () => { st.setLeftTab('memory') } },
      { id: 'tab-audit', label: 'Open Audit tab',
        run: () => { st.setRightTab('audit') } },
    ]
    const convs: PaletteAction[] = st.conversations
      .filter(c => c.id !== st.activeConversationId)
      .slice(0, 6)
      .map(c => ({
        id: `conv-${c.id}`, label: `Switch to: ${c.title}`, hint: 'conversation',
        run: () => onSwitchConversation(c.id),
      }))
    const tasks: PaletteAction[] = frequent.map((t, i) => ({
      id: `task-${i}`, label: `Run: ${t}`, hint: 'frequent',
      run: () => onCommand(t),
    }))
    return [...base, ...convs, ...tasks]
  }, [frequent, onCommand, onNewChat, onStop, onSwitchConversation, onToggleMode, open])

  const visible = useMemo(() => {
    const typed = query.trim()
    // Free-text escape hatch: anything typed can be sent as a raw command.
    const matches = actions.filter(a => fuzzy(typed, a.label))
    if (typed && !matches.length) {
      return [{ id: '__raw__', label: `Ask Cosmos: "${typed}"`, hint: '↵',
                run: () => onCommand(typed) } as PaletteAction]
    }
    if (typed) {
      matches.push({ id: '__raw__', label: `Ask Cosmos: "${typed}"`, hint: '↵',
                     run: () => onCommand(typed) })
    }
    return matches
  }, [actions, query, onCommand])

  useEffect(() => { setSelected(0) }, [query])

  useEffect(() => {
    if (!open) return
    const el = listRef.current?.children[selected] as HTMLElement | undefined
    el?.scrollIntoView({ block: 'nearest' })
  }, [selected, open])

  if (!open) return null

  const choose = (a: PaletteAction) => { onClose(); a.run() }

  return (
    <div role="dialog" aria-modal="true" aria-label="Command palette"
      onClick={onClose}
      style={{ position: 'fixed', inset: 0, zIndex: 60,
        background: 'rgba(1,6,16,0.65)', backdropFilter: 'blur(3px)',
        display: 'flex', alignItems: 'flex-start', justifyContent: 'center',
        paddingTop: '18vh' }}>
      <div className="hud-panel" onClick={e => e.stopPropagation()}
        style={{ width: 'min(520px, calc(100vw - 48px))', borderRadius: 3 }}>
        <input
          ref={inputRef}
          className="hud-input"
          value={query}
          onChange={e => setQuery(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Escape') { e.preventDefault(); onClose() }
            else if (e.key === 'ArrowDown') { e.preventDefault(); setSelected(s => Math.min(s + 1, visible.length - 1)) }
            else if (e.key === 'ArrowUp') { e.preventDefault(); setSelected(s => Math.max(s - 1, 0)) }
            else if (e.key === 'Enter' && visible[selected]) { e.preventDefault(); choose(visible[selected]) }
          }}
          placeholder="Type a command, action, or conversation…"
          aria-label="Command palette input"
          style={{ width: '100%', padding: '11px 14px', border: 'none',
            borderBottom: '1px solid var(--border)', background: 'transparent' }}
        />
        <div ref={listRef} role="listbox" aria-label="Palette results"
          style={{ maxHeight: 300, overflowY: 'auto', padding: '6px' }}>
          {visible.length === 0 && (
            <div style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-label)',
              color: 'var(--text-lo)', padding: '10px 12px' }}>
              No matches.
            </div>
          )}
          {visible.map((a, i) => (
            <div key={a.id} role="option" aria-selected={i === selected}
              onMouseEnter={() => setSelected(i)}
              onClick={() => choose(a)}
              style={{ display: 'flex', alignItems: 'center', gap: 8,
                padding: '7px 10px', borderRadius: 2, cursor: 'pointer',
                background: i === selected ? 'rgba(0,212,255,0.1)' : 'transparent',
                border: `1px solid ${i === selected ? 'rgba(0,212,255,0.3)' : 'transparent'}` }}>
              <span style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-label)',
                color: i === selected ? 'var(--text-hi)' : 'var(--text)',
                flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis',
                whiteSpace: 'nowrap' }}>
                {a.label}
              </span>
              {a.hint && (
                <span style={{ fontFamily: 'var(--font-d)', fontSize: 'var(--fs-cap)',
                  letterSpacing: '0.1em', color: 'var(--text-lo)', flexShrink: 0 }}>
                  {a.hint}
                </span>
              )}
            </div>
          ))}
        </div>
        <div style={{ borderTop: '1px solid var(--border)', padding: '5px 12px',
          fontFamily: 'var(--font-m)', fontSize: 'var(--fs-cap)', color: 'var(--text-lo)',
          letterSpacing: '0.1em' }}>
          ↑↓ NAVIGATE · ↵ RUN · ESC CLOSE
        </div>
      </div>
    </div>
  )
}
