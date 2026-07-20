import { useState, useEffect, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { ConfirmRequest, AskRequest } from '../store'

// ── Warning glyph (SVG, not emoji) ─────────────────────────────
function WarningGlyph() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none"
      stroke="var(--amber)" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"
      style={{ flexShrink: 0, filter: 'drop-shadow(0 0 5px rgba(255,149,0,0.7))' }}>
      <path d="M9 2L16.5 15.5H1.5L9 2z" />
      <path d="M9 7v4M9 13.4v.1" />
    </svg>
  )
}

interface Props {
  confirmRequest: ConfirmRequest | null
  askRequest: AskRequest | null
  onConfirm: (response: 'yes' | 'no') => void
  onAnswer: (text: string) => void
}

export default function ConfirmBar({ confirmRequest, askRequest, onConfirm, onAnswer }: Props) {
  const [answer, setAnswer] = useState('')
  const [showDetail, setShowDetail] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const abortRef = useRef<HTMLButtonElement>(null)
  const executeRef = useRef<HTMLButtonElement>(null)
  const active = confirmRequest ?? askRequest

  useEffect(() => {
    if (askRequest) {
      setAnswer('')
      setTimeout(() => inputRef.current?.focus(), 250)
    }
  }, [askRequest])

  // Keyboard-first authorization: focus lands on ABORT (the safe default);
  // Y approves, N/Esc declines, Tab cycles between the two buttons.
  useEffect(() => {
    if (!confirmRequest) { setShowDetail(false); return }
    setTimeout(() => abortRef.current?.focus(), 150)
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA')) return
      if (e.key === 'y' || e.key === 'Y') { e.preventDefault(); onConfirm('yes') }
      else if (e.key === 'n' || e.key === 'N' || e.key === 'Escape') {
        e.preventDefault(); onConfirm('no')
      } else if (e.key === 'Tab') {
        // Trap focus between the two decision buttons.
        e.preventDefault()
        const isAbort = document.activeElement === abortRef.current
        ;(isAbort ? executeRef.current : abortRef.current)?.focus()
      }
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [confirmRequest, onConfirm])

  const submitAnswer = () => {
    const t = answer.trim()
    if (!t) return
    setAnswer('')
    onAnswer(t)
  }

  return (
    <AnimatePresence>
      {active && (
        <motion.div
          initial={{ opacity: 0, y: 48 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 48 }}
          transition={{ type: 'spring', stiffness: 320, damping: 28 }}
          className="confirm-bar"
          role="alertdialog" aria-modal="true"
          aria-label={confirmRequest ? 'Authorization required' : 'Input required'}
          style={{ position: 'fixed', left: 0, right: 0, margin: '0 auto',
            bottom: 'calc(42.5% + 14px)', zIndex: 40,
            width: 'min(560px, calc(100vw - 48px))',
            borderRadius: 3, padding: '12px 16px',
            display: 'flex', flexDirection: 'column', gap: 10 }}>

          {/* Header row */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <WarningGlyph />
            <span style={{ fontFamily: 'var(--font-d)', fontSize: 'var(--fs-title)', fontWeight: 700,
              letterSpacing: '0.24em', color: 'var(--amber)',
              textShadow: '0 0 10px rgba(255,149,0,0.6)' }}>
              {confirmRequest ? 'AUTHORIZATION REQUIRED' : 'INPUT REQUIRED'}
            </span>
            {confirmRequest && (
              <span style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-cap)', letterSpacing: '0.1em',
                color: 'rgba(255,149,0,0.7)', marginLeft: 'auto', flexShrink: 0 }}>
                {confirmRequest.danger.toUpperCase().slice(0, 60)}
              </span>
            )}
          </div>

          {/* Body text */}
          <div style={{ fontFamily: 'var(--font-b)', fontSize: 'var(--fs-body)', lineHeight: 1.55,
            color: 'var(--text-hi)', wordBreak: 'break-word' }}>
            {confirmRequest ? confirmRequest.summary : askRequest?.question}
          </div>

          {/* Plan preview: one banner authorizes EVERY listed step at once */}
          {confirmRequest?.steps && confirmRequest.steps.length > 0 && (
            <ol style={{ margin: 0, paddingLeft: 22, maxHeight: 180, overflow: 'auto',
              display: 'flex', flexDirection: 'column', gap: 4 }}>
              {confirmRequest.steps.map((s, i) => (
                <li key={i} style={{ fontFamily: 'var(--font-b)', fontSize: 'var(--fs-cap)',
                  lineHeight: 1.5, color: 'var(--text)', wordBreak: 'break-word' }}>
                  {s.summary}
                  {s.danger && (
                    <span style={{ fontFamily: 'var(--font-m)', color: 'rgba(255,149,0,0.75)',
                      marginLeft: 6 }}>
                      [{s.danger.slice(0, 70)}]
                    </span>
                  )}
                </li>
              ))}
            </ol>
          )}

          {/* Exact command expander — verify the LITERAL call before approving */}
          {confirmRequest?.detail && (
            <div>
              <button onClick={() => setShowDetail(d => !d)}
                aria-expanded={showDetail}
                style={{ fontFamily: 'var(--font-d)', fontSize: 'var(--fs-cap)',
                  letterSpacing: '0.14em', color: 'rgba(255,149,0,0.8)',
                  background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}>
                {showDetail ? '▾ HIDE EXACT COMMAND' : '▸ SHOW EXACT COMMAND'}
              </button>
              {showDetail && (
                <pre style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-cap)',
                  color: 'var(--text)', background: 'rgba(0,0,0,0.4)',
                  border: '1px solid rgba(255,149,0,0.2)', borderRadius: 2,
                  padding: '8px 10px', marginTop: 6, maxHeight: 180, overflow: 'auto',
                  whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                  {confirmRequest.detail}
                </pre>
              )}
            </div>
          )}

          {/* Actions */}
          {confirmRequest ? (
            <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
              <button ref={executeRef} className="confirm-btn execute" onClick={() => onConfirm('yes')}>
                EXECUTE (Y)
              </button>
              <button ref={abortRef} className="confirm-btn abort" onClick={() => onConfirm('no')}>
                ABORT (N / ESC)
              </button>
              <span style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-cap)', color: 'var(--text-lo)',
                letterSpacing: '0.1em', marginLeft: 'auto' }}>
                or say "cosmos yes" / "cosmos no"
              </span>
            </div>
          ) : (
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <input
                ref={inputRef}
                className="hud-input"
                value={answer}
                onChange={e => setAnswer(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') submitAnswer() }}
                placeholder="Type your answer…"
                style={{ flex: 1, padding: '6px 10px', borderRadius: 2 }}
              />
              <button className="confirm-btn execute" onClick={submitAnswer} disabled={!answer.trim()}
                style={{ opacity: answer.trim() ? 1 : 0.4 }}>
                SEND
              </button>
              <span style={{ fontFamily: 'var(--font-m)', fontSize: 'var(--fs-cap)', color: 'var(--text-lo)',
                letterSpacing: '0.1em', flexShrink: 0 }}>
                or answer by voice
              </span>
            </div>
          )}
        </motion.div>
      )}
    </AnimatePresence>
  )
}
