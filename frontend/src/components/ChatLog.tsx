import { memo, useCallback, useEffect, useRef, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useShallow } from 'zustand/react/shallow'
import { Message, CosmosState, Conversation, useCosmosStore } from '../store'
import Markdown from './Markdown'

const T = (d: Date | string) =>
  new Date(d).toLocaleTimeString('en-US', { hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false })

const DATE = (d: Date | string) =>
  new Date(d).toLocaleDateString('en-US', { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit' })

interface Props {
  messages: Message[]
  partialTranscript: string
  currentToolLabel?: string   // live label of the tool Cosmos is running right now
  state: CosmosState
  onCommand: (text: string) => void
  onStop: () => void
  conversations: Conversation[]
  activeConversationId: string
  onNewChat: () => void
  onSwitchConversation: (id: string) => void
  onDeleteConversation?: (id: string) => void
}

// Small hover-revealed icon button used on message bubbles.
function BubbleBtn({ label, onClick, children }: {
  label: string; onClick: () => void; children: React.ReactNode
}) {
  return (
    <button aria-label={label} title={label} onClick={onClick}
      className="bubble-btn"
      style={{ background:'rgba(0,20,40,0.85)', border:'1px solid var(--border)',
        color:'var(--text)', borderRadius:2, cursor:'pointer',
        padding:'2px 6px', fontSize:'var(--fs-cap)', fontFamily:'var(--font-m)',
        display:'inline-flex', alignItems:'center', gap:4 }}>
      {children}
    </button>
  )
}

// ── Completed message row — memoized so streaming ticks / input keystrokes /
// state transitions never rebuild archived bubbles (or re-parse markdown).
const MessageRow = memo(function MessageRow({ msg, copied, busy, onCopy, onCommand }: {
  msg: Message; copied: boolean; busy: boolean
  onCopy: (id: string, text: string) => void
  onCommand: (text: string) => void
}) {
  return (
    <motion.div
      initial={{ opacity:0, y:4 }} animate={{ opacity:1, y:0 }}
      transition={{ duration:0.15 }}
      className="msg-row"
      style={{ display:'flex', flexDirection:'column', gap:3 }}>
      <div style={{ display:'flex', alignItems:'center', gap:8 }}>
        <span style={{
          fontFamily:'var(--font-d)', fontSize:'var(--fs-cap)', fontWeight:700,
          letterSpacing:'0.18em',
          color: msg.role==='cosmos' ? 'var(--cyan)' : 'rgba(200,220,240,0.7)',
          textShadow: msg.role==='cosmos' ? '0 0 8px rgba(0,212,255,0.6)' : 'none',
        }}>
          {msg.role==='cosmos' ? 'COSMOS' : 'SIR'}
        </span>
        <span style={{ fontFamily:'var(--font-m)', fontSize:'var(--fs-cap)', color:'var(--text-lo)' }}>
          {T(msg.timestamp)}
        </span>
        <span className="msg-actions" style={{ marginLeft:'auto', display:'flex', gap:4 }}>
          {msg.role==='cosmos' ? (
            <BubbleBtn label="Copy reply" onClick={() => onCopy(msg.id, msg.text)}>
              {copied ? '✓ copied' : '⧉ copy'}
            </BubbleBtn>
          ) : (
            <BubbleBtn label="Run again" onClick={() => !busy && onCommand(msg.text)}>
              ↻ re-run
            </BubbleBtn>
          )}
        </span>
      </div>
      <div className={msg.role==='cosmos' ? 'msg-cosmos' : 'msg-user'}
        style={{ padding:'6px 10px', borderRadius:'0 4px 4px 0',
          fontFamily:'var(--font-b)', fontSize:'var(--fs-body)', lineHeight:1.55,
          color: msg.role==='cosmos' ? 'var(--text-hi)' : 'var(--text)',
          ...(msg.kind==='briefing' ? {
            border:'1px solid rgba(0,212,255,0.25)', borderRadius:4,
            background:'rgba(0,212,255,0.04)' } : {}) }}>
        {msg.kind==='briefing' && (
          <div style={{ fontFamily:'var(--font-d)', fontSize:'var(--fs-cap)',
            fontWeight:700, letterSpacing:'0.2em', color:'var(--cyan)',
            textShadow:'0 0 8px rgba(0,212,255,0.5)',
            borderBottom:'1px solid rgba(0,212,255,0.2)',
            paddingBottom:4, marginBottom:6, textTransform:'uppercase' }}>
            ☀ {msg.title || 'briefing'}
          </div>
        )}
        {msg.role==='cosmos'
          ? <Markdown text={msg.text} />
          : msg.text}
      </div>
    </motion.div>
  )
})

// ── Live streaming reply — a LEAF subscriber to streamingText, so per-token
// store updates re-render only this bubble, not ChatLog / the app tree.
// Rendered as MARKDOWN so it doesn't visibly re-format when the final
// message lands. onGrow keeps the log scrolled (or shows the NEW RESPONSE
// pill) exactly as the old parent-level effect did.
const StreamingBubble = memo(function StreamingBubble({ onGrow }: { onGrow: () => void }) {
  const streamingText = useCosmosStore(s => s.streamingText)
  useEffect(() => { if (streamingText) onGrow() }, [streamingText, onGrow])
  if (!streamingText) return null
  return (
    <motion.div aria-hidden initial={{ opacity:0 }} animate={{ opacity:1 }}
      style={{ display:'flex', flexDirection:'column', gap:3 }}>
      <span style={{ fontFamily:'var(--font-d)', fontSize:'var(--fs-cap)', fontWeight:700,
        letterSpacing:'0.18em', color:'var(--cyan)',
        textShadow:'0 0 8px rgba(0,212,255,0.6)' }}>
        COSMOS
      </span>
      <div className="msg-cosmos" style={{ padding:'6px 10px', borderRadius:'0 4px 4px 0',
        fontFamily:'var(--font-b)', fontSize:'var(--fs-body)', lineHeight:1.55, color:'var(--text-hi)' }}>
        <Markdown text={streamingText} />
        <span style={{ display:'inline-block', width:7, height:12, background:'var(--cyan)',
          marginLeft:2, verticalAlign:'middle', animation:'blink 1s step-end infinite' }}/>
      </div>
    </motion.div>
  )
})

export default function ChatLog({
  messages, partialTranscript, currentToolLabel, state, onCommand, onStop,
  conversations, activeConversationId, onNewChat, onSwitchConversation, onDeleteConversation,
}: Props) {
  const scrollRef  = useRef<HTMLDivElement>(null)
  const bottomRef  = useRef<HTMLDivElement>(null)
  const atBottomRef = useRef(true)
  const inputRef   = useRef<HTMLInputElement>(null)
  const [input, setInput]             = useState('')
  const [showHistory, setShowHistory] = useState(false)
  const [newPill, setNewPill]         = useState(false)
  const [renaming, setRenaming]       = useState<string | null>(null)
  const [renameText, setRenameText]   = useState('')
  const [copied, setCopied]           = useState<string | null>(null)
  const busy = state === 'thinking' || state === 'speaking' || state === 'executing'

  const { queuedCommand, setQueuedCommand, renameConversation, togglePinConversation } =
    useCosmosStore(useShallow(s => ({
      queuedCommand: s.queuedCommand, setQueuedCommand: s.setQueuedCommand,
      renameConversation: s.renameConversation, togglePinConversation: s.togglePinConversation,
    })))
  // Boolean-only subscription: ChatLog re-renders when streaming STARTS/ENDS
  // (the thinking indicator + scroll effect need that), never per token —
  // the token-level subscription lives in the StreamingBubble leaf.
  const hasStreaming = useCosmosStore(s => s.streamingText !== '')

  // Scroll anchoring: only follow new content while the user is AT the bottom.
  // Detached readers get a NEW RESPONSE pill instead of losing their place.
  // Mirror showHistory into a ref so followOutput stays referentially stable
  // (it's a prop of the memoized StreamingBubble).
  const showHistoryRef = useRef(false)
  useEffect(() => { showHistoryRef.current = showHistory }, [showHistory])
  const followOutput = useCallback(() => {
    if (showHistoryRef.current) return
    if (atBottomRef.current) {
      bottomRef.current?.scrollIntoView({ behavior:'smooth' })
      setNewPill(false)
    } else {
      setNewPill(true)
    }
  }, [])
  useEffect(() => {
    followOutput()
  }, [messages, partialTranscript, hasStreaming, showHistory, followOutput])

  const onScroll = () => {
    const el = scrollRef.current
    if (!el) return
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 48
    atBottomRef.current = atBottom
    if (atBottom) setNewPill(false)
  }

  const submit = () => {
    const t = input.trim()
    if (!t) return
    setInput('')
    if (busy) {
      // Don't reject mid-run typing — queue it for when the run finishes.
      setQueuedCommand(t)
    } else {
      onCommand(t)
    }
    inputRef.current?.focus()
  }

  // Stable identity — passed to the memoized MessageRow.
  const copyText = useCallback((id: string, text: string) => {
    navigator.clipboard?.writeText(text).then(() => {
      setCopied(id)
      setTimeout(() => setCopied(c => (c === id ? null : c)), 1200)
    }).catch(() => {})
  }, [])

  const sortedConvs = [...conversations].sort((a, b) =>
    Number(!!b.pinned) - Number(!!a.pinned))

  return (
    <div className="hud-panel" style={{ height:'100%', display:'flex', flexDirection:'column' }}>
      {/* Header */}
      <div className="hud-title" style={{ justifyContent:'space-between', gap:8 }}>
        <span>Command Interface</span>
        <div style={{ display:'flex', gap:6, alignItems:'center' }}>
          {conversations.length > 1 && (
            <button onClick={() => setShowHistory(h => !h)}
              aria-expanded={showHistory}
              style={{ fontFamily:'var(--font-d)', fontSize:'var(--fs-cap)', letterSpacing:'0.12em',
                color: showHistory ? 'var(--cyan)' : 'var(--text-lo)',
                background: showHistory ? 'rgba(0,212,255,0.1)' : 'transparent',
                border:`1px solid ${showHistory ? 'rgba(0,212,255,0.3)' : 'rgba(0,212,255,0.1)'}`,
                padding:'2px 8px', cursor:'pointer', borderRadius:2 }}>
              HISTORY
            </button>
          )}
          <button onClick={onNewChat}
            style={{ fontFamily:'var(--font-d)', fontSize:'var(--fs-cap)', letterSpacing:'0.12em',
              color:'var(--cyan)', background:'rgba(0,212,255,0.08)',
              border:'1px solid rgba(0,212,255,0.3)',
              padding:'2px 10px', cursor:'pointer', borderRadius:2,
              transition:'all 0.15s' }}>
            + NEW CHAT
          </button>
        </div>
      </div>

      {/* Conversation history panel — rename / pin / delete */}
      <AnimatePresence>
        {showHistory && (
          <motion.div initial={{ height:0, opacity:0 }} animate={{ height:'auto', opacity:1 }}
            exit={{ height:0, opacity:0 }} transition={{ duration:0.2 }}
            style={{ overflow:'hidden', borderBottom:'1px solid var(--border)' }}>
            <div style={{ padding:'6px 10px', maxHeight:180, overflowY:'auto' }}>
              <div style={{ fontFamily:'var(--font-m)', fontSize:'var(--fs-cap)', color:'var(--text-lo)',
                letterSpacing:'0.12em', marginBottom:6 }}>
                PAST CONVERSATIONS — pin ★ keeps them past the 10-cap
              </div>
              {sortedConvs.map(conv => (
                <div key={conv.id}
                  style={{
                    display:'flex', alignItems:'center', gap:6, padding:'4px 8px',
                    marginBottom:2, borderRadius:2,
                    background: conv.id === activeConversationId
                      ? 'rgba(0,212,255,0.1)' : 'transparent',
                    border: `1px solid ${conv.id === activeConversationId
                      ? 'rgba(0,212,255,0.3)' : 'transparent'}`,
                  }}>
                  {renaming === conv.id ? (
                    <input autoFocus className="hud-input" value={renameText}
                      onChange={e => setRenameText(e.target.value)}
                      onKeyDown={e => {
                        if (e.key === 'Enter') { renameConversation(conv.id, renameText); setRenaming(null) }
                        if (e.key === 'Escape') setRenaming(null)
                      }}
                      onBlur={() => setRenaming(null)}
                      style={{ flex:1, padding:'2px 6px', borderRadius:2 }} />
                  ) : (
                    <button
                      onClick={() => { onSwitchConversation(conv.id); setShowHistory(false) }}
                      onDoubleClick={() => { setRenaming(conv.id); setRenameText(conv.title) }}
                      aria-current={conv.id === activeConversationId ? 'true' : undefined}
                      title="Click to open · double-click to rename"
                      style={{ flex:1, minWidth:0, display:'flex', alignItems:'center', gap:8,
                        background:'none', border:'none', cursor:'pointer', textAlign:'left', padding:0 }}>
                      <span style={{ fontFamily:'var(--font-m)', fontSize:'var(--fs-label)', color:'var(--text)',
                        flex:1, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>
                        {conv.title}
                      </span>
                      <span style={{ fontFamily:'var(--font-m)', fontSize:'var(--fs-cap)', color:'var(--text-lo)',
                        flexShrink:0 }}>
                        {DATE(conv.startedAt)} · {conv.messages.length} msgs
                      </span>
                    </button>
                  )}
                  <button aria-label={conv.pinned ? 'Unpin conversation' : 'Pin conversation'}
                    aria-pressed={!!conv.pinned}
                    onClick={() => togglePinConversation(conv.id)}
                    style={{ background:'none', border:'none', cursor:'pointer', padding:'0 2px',
                      color: conv.pinned ? 'var(--amber)' : 'var(--text-lo)', fontSize:'var(--fs-label)' }}>
                    {conv.pinned ? '★' : '☆'}
                  </button>
                  {conv.id !== activeConversationId && (
                    <button aria-label="Delete conversation"
                      onClick={() => onDeleteConversation?.(conv.id)}
                      style={{ background:'none', border:'none', cursor:'pointer', padding:'0 2px',
                        color:'rgba(255,34,68,0.6)', fontSize:'var(--fs-label)' }}>
                      ✕
                    </button>
                  )}
                </div>
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Messages */}
      <div ref={scrollRef} onScroll={onScroll} role="log" aria-label="Conversation with Cosmos"
        style={{ flex:1, overflowY:'auto', padding:'10px 12px',
        display:'flex', flexDirection:'column', gap:8, minHeight:0, position:'relative' }}>

        {messages.length === 0 && (
          <div style={{ fontFamily:'var(--font-m)', fontSize:'var(--fs-label)', color:'var(--text-lo)',
            textAlign:'center', marginTop:24, lineHeight:2.2 }}>
            Say <span style={{ color:'rgba(0,212,255,0.4)' }}>"cosmos open chrome"</span> or type below · ⌘K for the palette
          </div>
        )}

        <AnimatePresence initial={false}>
          {messages.map(msg => (
            <MessageRow key={msg.id} msg={msg}
              copied={copied === msg.id} busy={busy}
              onCopy={copyText} onCommand={onCommand} />
          ))}
        </AnimatePresence>

        {/* Live transcript */}
        {partialTranscript && state==='listening' && (
          <motion.div aria-hidden initial={{ opacity:0 }} animate={{ opacity:1 }}
            style={{ display:'flex', flexDirection:'column', gap:3 }}>
            <span style={{ fontFamily:'var(--font-d)', fontSize:'var(--fs-cap)', color:'rgba(200,220,240,0.6)',
              letterSpacing:'0.18em' }}>SIR</span>
            <div className="msg-user" style={{ padding:'6px 10px',
              fontFamily:'var(--font-b)', fontSize:'var(--fs-body)', color:'rgba(176,196,222,0.55)',
              fontStyle:'italic', borderRadius:'0 4px 4px 0' }}>
              {partialTranscript}
              <span style={{ display:'inline-block', width:7, height:12, background:'var(--cyan)',
                marginLeft:3, verticalAlign:'middle', animation:'blink 1s step-end infinite' }}/>
            </div>
          </motion.div>
        )}

        {/* Live streaming reply — leaf subscriber, re-renders per token
            WITHOUT re-rendering ChatLog or the app tree */}
        <StreamingBubble onGrow={followOutput} />

        {/* Thinking / executing indicator */}
        {(state==='thinking'||state==='executing') && !hasStreaming && (
          <motion.div initial={{ opacity:0 }} animate={{ opacity:1 }}
            style={{ display:'flex', alignItems:'center', gap:8, padding:'2px 0' }}>
            {[0,180,360].map(d => (
              <div key={d} style={{ width:6, height:6, borderRadius:'50%',
                background: state==='executing' ? 'var(--purple)' : 'var(--amber)',
                boxShadow: `0 0 6px ${state==='executing' ? 'var(--purple)' : 'var(--amber)'}`,
                animation:`softPulse 0.9s ease-in-out ${d}ms infinite` }}/>
            ))}
            <span style={{ fontFamily:'var(--font-m)', fontSize:'var(--fs-label)', letterSpacing:'0.08em',
              color: state==='executing' ? 'rgba(153,68,255,0.7)' : 'rgba(255,149,0,0.6)',
              overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap', maxWidth:280 }}>
              {state==='executing' && currentToolLabel ? currentToolLabel.toUpperCase() :
               state==='executing' ? 'EXECUTING' : 'PROCESSING'}
            </span>
          </motion.div>
        )}

        <div ref={bottomRef}/>
      </div>

      {/* NEW RESPONSE pill — shown when content arrived while scrolled up */}
      {newPill && (
        <button onClick={() => { atBottomRef.current = true; setNewPill(false)
            bottomRef.current?.scrollIntoView({ behavior:'smooth' }) }}
          style={{ position:'absolute', bottom:64, left:'50%', transform:'translateX(-50%)',
            zIndex:5, fontFamily:'var(--font-d)', fontSize:'var(--fs-cap)', letterSpacing:'0.14em',
            color:'var(--cyan)', background:'rgba(0,20,40,0.95)',
            border:'1px solid var(--cyan-50)', borderRadius:10, padding:'4px 12px',
            cursor:'pointer', boxShadow:'0 0 14px rgba(0,212,255,0.25)' }}>
          ↓ NEW RESPONSE
        </button>
      )}

      {/* Queued command chip */}
      {queuedCommand && (
        <div style={{ display:'flex', alignItems:'center', gap:8, padding:'4px 12px',
          borderTop:'1px solid var(--border)', background:'rgba(0,212,255,0.03)' }}>
          <span style={{ fontFamily:'var(--font-d)', fontSize:'var(--fs-cap)',
            letterSpacing:'0.14em', color:'var(--amber)' }}>QUEUED</span>
          <span style={{ fontFamily:'var(--font-m)', fontSize:'var(--fs-label)', color:'var(--text)',
            flex:1, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>
            {queuedCommand}
          </span>
          <button aria-label="Dismiss queued command" onClick={() => setQueuedCommand(null)}
            style={{ background:'none', border:'none', cursor:'pointer',
              color:'var(--text-lo)', fontSize:'var(--fs-label)' }}>✕</button>
        </div>
      )}

      {/* Input — stays ENABLED during runs (submits queue) */}
      <div style={{ borderTop:'1px solid var(--border)', padding:'9px 12px',
        display:'flex', gap:8, alignItems:'center' }}>
        <div style={{ width:6, height:6, borderRadius:'50%', flexShrink:0,
          background: state==='listening' ? 'var(--cyan)'
            : state==='speaking'   ? 'var(--green)'
            : busy                 ? 'var(--amber)'
            : 'rgba(0,212,255,0.25)',
          boxShadow: state==='listening' ? '0 0 8px var(--cyan)' : 'none',
          animation: state==='listening' ? 'softPulse 1s ease-in-out infinite' : 'none' }}/>
        <input
          ref={inputRef}
          className="hud-input" value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if(e.key==='Enter' && !e.shiftKey) submit() }}
          placeholder={
            state==='listening' ? 'Listening…' :
            busy                ? 'Working — Enter queues your next command…' :
            'Type a command or say "cosmos …"  ·  ⌘K palette'
          }
          aria-label="Command input"
          style={{ flex:1, padding:'7px 12px', borderRadius:2 }}
        />
        {busy ? (
          <button
            onClick={onStop}
            style={{
              fontFamily:'var(--font-d)', fontSize:'var(--fs-cap)', letterSpacing:'0.2em',
              color:'var(--red)', background:'rgba(255,34,68,0.1)',
              border:'1px solid rgba(255,34,68,0.5)',
              padding:'7px 16px', cursor:'pointer', borderRadius:2,
              boxShadow:'0 0 10px rgba(255,34,68,0.2)',
              whiteSpace:'nowrap', flexShrink:0,
            }}>
            ■ STOP
          </button>
        ) : (
          <button className="hud-btn active" onClick={submit}
            disabled={!input.trim()}
            style={{ padding:'7px 16px', borderRadius:2 }}>
            SEND
          </button>
        )}
      </div>
    </div>
  )
}
